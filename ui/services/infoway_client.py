"""Read-only Infoway quotes and candles. Research display only.

Official HTTP host is https://data.infoway.io. The key travels in the
``apiKey`` header, never in the URL. Success is ``ret == 200``. An HTTP 200
with any other ``ret`` is a failure. Forex and metals use ``/common/``.

The free REST plan allows 1 request per second, 60 per minute, and 86,400
per day. This process uses the lower ``daily_cap`` in config_ext.yaml.
A timeout is not retried. MT5 remains the price used for orders.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config_ext_loader import InfowaySettings, load_config_ext
from engine.keys import Keyring, keyring

HOST = "https://data.infoway.io"
BUDGET_PATH = Path(__file__).resolve().parents[2] / "data" / "infoway_budget.json"
_CACHE: dict[tuple[str, str, str], tuple[float, "InfowayStatus"]] = {}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TradePrint(StrictModel):
    s: str
    t: int
    p: str
    v: str
    vw: str
    td: int = 0


class CandleBar(StrictModel):
    t: str
    h: str
    o: str
    l: str
    c: str
    v: str
    vw: str
    pc: str = ""
    pca: str = ""


class CandleSeries(StrictModel):
    s: str = ""
    respList: list[CandleBar] = Field(default_factory=list)


class Envelope(StrictModel):
    ret: int
    msg: str = ""
    traceId: str = ""
    data: list[dict] | None = None


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_s: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class InfowayStatus:
    ok: bool
    status: str
    latency_s: float | None
    symbol: str
    price: str | None = None
    candles: tuple[CandleBar, ...] = ()
    detail: str = ""


Transport = Callable[[HttpRequest], HttpResponse]


def clear_cache() -> None:
    _CACHE.clear()


def common_code(symbol: str) -> str:
    """Forex and metals codes on the official /common/ routes."""
    code = symbol.strip().upper().replace("/", "")
    if not code.isalnum() or not 3 <= len(code) <= 12:
        raise ValueError("symbol is not a common-market code")
    return code


def _status(
    symbol: str,
    status: str,
    *,
    ok: bool = False,
    latency_s: float | None = None,
    price: str | None = None,
    candles: tuple[CandleBar, ...] = (),
    detail: str = "",
) -> InfowayStatus:
    return InfowayStatus(
        ok=ok,
        status=status,
        latency_s=latency_s,
        symbol=symbol,
        price=price,
        candles=candles,
        detail=detail[:160],
    )


def _budget_count(path: Path, today: str) -> int:
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("budget file unreadable")
    if not isinstance(payload, dict) or set(payload) - {"date", "count"}:
        raise ValueError("budget file unreadable")
    if payload.get("date") != today:
        return 0
    count = payload.get("count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("budget file unreadable")
    return count


def _charge(path: Path, today: str, cap: int) -> None:
    count = _budget_count(path, today)
    if count >= cap:
        raise RuntimeError("daily cap reached")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"date": today, "count": count + 1}),
        encoding="utf-8",
    )


def _urllib_transport(request: HttpRequest) -> HttpResponse:
    outgoing = urllib.request.Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=request.timeout_s) as response:
            return HttpResponse(status=int(response.status), body=response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        raw = exc.read(2_000) if exc.fp else b""
        return HttpResponse(status=int(exc.code), body=raw)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("timeout") from exc
        raise


def _parse_trades(payload: list[dict], symbol: str) -> InfowayStatus:
    prints = [TradePrint.model_validate(item) for item in payload]
    match = next((item for item in prints if item.s.upper() == symbol), prints[0] if prints else None)
    if match is None:
        return _status(symbol, "degraded", detail="empty trade list")
    return _status(symbol, "ok", ok=True, price=match.p)


def _parse_candles(payload: list[dict], symbol: str) -> InfowayStatus:
    series = [CandleSeries.model_validate(item) for item in payload]
    bars: list[CandleBar] = []
    for item in series:
        if not item.s or item.s.upper() == symbol:
            bars.extend(item.respList)
    if not bars:
        return _status(symbol, "degraded", detail="empty candle list")
    return _status(symbol, "ok", ok=True, candles=tuple(bars), price=bars[0].c)


def _finish(body: bytes, symbol: str, kind: str) -> InfowayStatus:
    try:
        parsed = json.loads(body.decode("utf-8"))
        envelope = Envelope.model_validate(parsed)
    except (UnicodeError, json.JSONDecodeError, ValidationError):
        return _status(symbol, "degraded", detail="response did not match the schema")
    if envelope.ret != 200:
        return _status(symbol, "degraded", detail=envelope.msg or f"ret {envelope.ret}")
    data = envelope.data or []
    try:
        if kind == "trade":
            return _parse_trades(data, symbol)
        return _parse_candles(data, symbol)
    except ValidationError:
        return _status(symbol, "degraded", detail="response did not match the schema")


def _request(
    symbol: str,
    kind: str,
    request: HttpRequest,
    *,
    transport: Transport,
    settings: InfowaySettings,
    budget_path: Path,
    now: float,
    refresh: bool,
) -> InfowayStatus:
    cache_key = (kind, request.url, request.body.decode("utf-8") if request.body else "")
    cached = _CACHE.get(cache_key)
    if cached and not refresh and now - cached[0] <= settings.cache_ttl_s:
        return cached[1]
    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    try:
        _charge(budget_path, today, settings.daily_cap)
    except ValueError as exc:
        return _status(symbol, "degraded", detail=str(exc))
    except RuntimeError:
        return _status(symbol, "capped", detail="daily cap reached")
    started = time.perf_counter()
    try:
        response = transport(request)
    except TimeoutError:
        return _status(symbol, "degraded", detail="timeout", latency_s=time.perf_counter() - started)
    except Exception:
        return _status(symbol, "degraded", detail="request failed", latency_s=time.perf_counter() - started)
    latency = time.perf_counter() - started
    if response.status != 200:
        result = _status(symbol, "degraded", detail=f"http {response.status}", latency_s=latency)
    else:
        result = _finish(response.body, symbol, kind)
        result = InfowayStatus(
            ok=result.ok,
            status=result.status,
            latency_s=latency,
            symbol=result.symbol,
            price=result.price,
            candles=result.candles,
            detail=result.detail,
        )
    _CACHE[cache_key] = (now, result)
    return result


def _ring_or_default(ring: Keyring | None) -> Keyring:
    return ring if ring is not None else keyring()


def fetch_quote(
    symbol: str,
    *,
    transport: Transport | None = None,
    ring: Keyring | None = None,
    settings: InfowaySettings | None = None,
    budget_path: Path | None = None,
    now: float | None = None,
    refresh: bool = False,
) -> InfowayStatus:
    """Latest trade price for one common-market symbol. No order path."""
    return _fetch(
        symbol,
        kind="trade",
        transport=transport,
        ring=ring,
        settings=settings,
        budget_path=budget_path,
        now=now,
        refresh=refresh,
    )


def fetch_candles(
    symbol: str,
    *,
    kline_type: int = 8,
    kline_num: int = 2,
    transport: Transport | None = None,
    ring: Keyring | None = None,
    settings: InfowaySettings | None = None,
    budget_path: Path | None = None,
    now: float | None = None,
    refresh: bool = False,
) -> InfowayStatus:
    """Recent candles. kline_num stays at the vendor maximum of 500."""
    if kline_type < 1 or kline_type > 12 or kline_num < 1 or kline_num > 500:
        return _status(symbol, "degraded", detail="kline arguments out of range")
    return _fetch(
        symbol,
        kind="candle",
        kline_type=kline_type,
        kline_num=kline_num,
        transport=transport,
        ring=ring,
        settings=settings,
        budget_path=budget_path,
        now=now,
        refresh=refresh,
    )


def _fetch(
    symbol: str,
    *,
    kind: str,
    transport: Transport | None,
    ring: Keyring | None,
    settings: InfowaySettings | None,
    budget_path: Path | None,
    now: float | None,
    refresh: bool,
    kline_type: int = 8,
    kline_num: int = 2,
) -> InfowayStatus:
    active = _ring_or_default(ring)
    if not active.is_configured("infoway"):
        return _status(symbol or "", "not_configured", detail="INFOWAY_API_KEY is not configured")
    try:
        code = common_code(symbol)
    except ValueError as exc:
        return _status(symbol, "degraded", detail=str(exc))
    chosen = settings if settings is not None else load_config_ext().infoway
    key = active.require("infoway")
    headers = {"apiKey": key, "Accept": "application/json"}
    if kind == "trade":
        request = HttpRequest(
            "GET",
            f"{HOST}/common/batch_trade/{code}",
            headers,
            None,
            chosen.timeout_s,
        )
    else:
        headers = {**headers, "Content-Type": "application/json"}
        body = json.dumps(
            {"klineType": kline_type, "klineNum": kline_num, "codes": code}
        ).encode("utf-8")
        request = HttpRequest(
            "POST",
            f"{HOST}/common/v2/batch_kline",
            headers,
            body,
            chosen.timeout_s,
        )
    if key in request.url:
        return _status(code, "degraded", detail="refusing to put the key in the url")
    return _request(
        code,
        kind,
        request,
        transport=transport or _urllib_transport,
        settings=chosen,
        budget_path=budget_path or BUDGET_PATH,
        now=time.time() if now is None else now,
        refresh=refresh,
    )


def smoke_quote(symbol: str = "EURUSD") -> tuple[str, float | None]:
    """One live read. Returns status and latency only."""
    clear_cache()
    result = fetch_quote(symbol, refresh=True)
    return result.status, result.latency_s
