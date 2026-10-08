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
    http_status: int | None = None
    ret_code: int | None = None
    as_of: str = ""


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
    http_status: int | None = None,
    ret_code: int | None = None,
    as_of: str = "",
) -> InfowayStatus:
    return InfowayStatus(
        ok=ok,
        status=status,
        latency_s=latency_s,
        symbol=symbol,
        price=price,
        candles=candles,
        detail=detail[:160],
        http_status=http_status,
        ret_code=ret_code,
        as_of=as_of,
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
    return _status(symbol, "ok", ok=True, price=match.p, as_of=str(match.t))


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
        return _status(
            symbol,
            "degraded",
            detail=envelope.msg or "ret rejected",
            ret_code=envelope.ret,
        )
    data = envelope.data or []
    try:
        parsed = _parse_trades(data, symbol) if kind == "trade" else _parse_candles(data, symbol)
    except ValidationError:
        return _status(symbol, "degraded", detail="response did not match the schema", ret_code=envelope.ret)
    return InfowayStatus(
        ok=parsed.ok,
        status=parsed.status,
        latency_s=parsed.latency_s,
        symbol=parsed.symbol,
        price=parsed.price,
        candles=parsed.candles,
        detail=parsed.detail,
        ret_code=envelope.ret,
        as_of=parsed.as_of,
    )


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
        result = _status(
            symbol,
            "degraded",
            detail="http error",
            latency_s=latency,
            http_status=response.status,
        )
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
            http_status=response.status,
            ret_code=result.ret_code,
            as_of=result.as_of,
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


def budget_snapshot(
    path: Path | None = None,
    now: float | None = None,
) -> tuple[int | None, int]:
    """Calls counted today and the cap. None means the budget file is unreadable."""
    settings = load_config_ext().infoway
    moment = time.time() if now is None else now
    today = datetime.fromtimestamp(moment, timezone.utc).date().isoformat()
    try:
        count = _budget_count(path or BUDGET_PATH, today)
    except ValueError:
        return None, settings.daily_cap
    return count, settings.daily_cap


# Official REST table:
# https://docs.infoway.io/en-docs/getting-started/error-codes/rest-api-error-codes.md
# HTTP status and business ret are different number spaces. ret 500 is both
# "server error" and "Apikey not exists", so it is not mapped to auth.
# 510/511/512 are WebSocket codes and are not mapped here.
_INFOWAY_RET = {
    501: "rate or plan limit",
    502: "rate or plan limit",
    503: "rate or plan limit",
    504: "rate or plan limit",
    505: "rate or plan limit",
    509: "auth rejected",
    513: "rate or plan limit",
    514: "auth rejected",
}
_INFOWAY_HTTP = {
    401: "auth rejected",
    404: "wrong endpoint",
    429: "rate or plan limit",
    524: "network",
}


def infoway_failure_class(
    *,
    configured: bool,
    http_status: int | None,
    ret: int | None,
    transport_failed: bool,
    ok: bool,
) -> str:
    """One of the documented classes, or unclassified. Success is none."""
    if not configured:
        return "missing key"
    if ok:
        return "none"
    if ret is not None and int(ret) != 200:
        return _INFOWAY_RET.get(int(ret), "unclassified")
    if http_status is not None and not 200 <= int(http_status) < 300:
        return _INFOWAY_HTTP.get(int(http_status), "unclassified")
    if transport_failed:
        return "network"
    return "unclassified"


def infoway_error_code(*, http_status: int | None, ret: int | None) -> str:
    """Short code only. Never a message body."""
    if ret is not None and int(ret) != 200:
        return str(int(ret))
    if http_status is not None and not 200 <= int(http_status) < 300:
        return str(int(http_status))
    return "none"


def smoke_quote(symbol: str = "EURUSD") -> tuple[str, float | None]:
    """One live read. Returns status and latency only."""
    clear_cache()
    result = fetch_quote(symbol, refresh=True)
    return result.status, result.latency_s


def smoke_report(symbol: str = "EURUSD") -> dict[str, object]:
    """One live read. The dict has no headers and no credential."""
    clear_cache()
    result = fetch_quote(symbol, refresh=True)
    latency_ms = None if result.latency_s is None else round(result.latency_s * 1000.0, 1)
    configured = result.status != "not_configured"
    transport_failed = (
        result.http_status is None
        and result.ret_code is None
        and result.status == "degraded"
        and result.detail in {"timeout", "request failed"}
    )
    return {
        "key_present": configured,
        "http_status": result.http_status,
        "ret": result.ret_code,
        "error_code": infoway_error_code(http_status=result.http_status, ret=result.ret_code),
        "failure_class": infoway_failure_class(
            configured=configured,
            http_status=result.http_status,
            ret=result.ret_code,
            transport_failed=transport_failed,
            ok=result.ok,
        ),
        "latency_ms": latency_ms,
        "result": "PASS" if result.ok else "FAIL",
    }
