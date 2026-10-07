"""One shadow pass over closed bars. It records agent results and does not trade."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import yaml

from config_ext_loader import load_config_ext
from engine.research import AGENT_MODULES
from learning.bars import load_bars
from learning.ledger import connect, record_prediction, resolve_open
from ui.services.dexter_client import research

ROOT = Path(__file__).resolve().parents[1]
NEWS_CACHE = ROOT / "data" / "news_cache.json"
SENTIMENT_CACHE = ROOT / "data" / "sentiment_cache.json"
PRICE_AGENTS = ("01", "02", "03", "07", "08")
DETERMINISTIC = ("09", "10")


def _plain(value: object) -> object:
    if hasattr(value, "item") and not isinstance(value, (str, bytes, dict, list, tuple)):
        try:
            return _plain(value.item())  # type: ignore[attr-defined]
        except Exception:
            return None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in list(value)[:40]]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)[:160]


def direction_of(label: str) -> str:
    text = label.upper()
    if "BEAR" in text or text in {"SELL", "SHORT"}:
        return "short"
    if "BULL" in text or text in {"BUY", "LONG"}:
        return "long"
    return "flat"


def whitelist(path: Path | None = None) -> list[tuple[str, str]]:
    target = path or (ROOT / "config.yaml")
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    groups = payload.get("symbols", {}).get("whitelist", {})
    rows: list[tuple[str, str]] = []
    if not isinstance(groups, dict):
        return rows
    for asset_class, symbols in groups.items():
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            rows.append((str(symbol), str(asset_class)))
    return rows


def _events() -> list[dict]:
    if not NEWS_CACHE.is_file():
        return []
    try:
        payload = json.loads(NEWS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not events:
        return []
    return [item for item in events if isinstance(item, dict)]


def _sentiment(symbol: str) -> dict:
    if not SENTIMENT_CACHE.is_file():
        return {}
    try:
        payload = json.loads(SENTIMENT_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found = payload.get(symbol)
    return found if isinstance(found, dict) and found else {}


def _record(conn, *, symbol: str, timeframe: str, bar_time: str, agent_id: str, result: object, config_hash: str) -> None:
    label = str(getattr(result, "label", "ERROR"))
    features = {
        "ok": bool(getattr(result, "ok", False)),
        "error": getattr(result, "error", None),
        "label": label,
        "features": _plain(getattr(result, "features", {})),
    }
    score = getattr(result, "score", None)
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)):
        stored_score: float | None = float(score)
    else:
        stored_score = None
    record_prediction(
        conn,
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_time": bar_time,
            "agent_id": agent_id,
            "agent_version": "1",
            "direction": direction_of(label),
            "score": stored_score,
            "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
            "features_json": json.dumps(features, default=str)[:8000],
            "config_hash": config_hash,
        },
    )


def run_cycle(
    *,
    enabled: bool = True,
    bar_root: Path | None = None,
    db_path: Path | None = None,
    config_path: Path | None = None,
    symbols: list[tuple[str, str]] | None = None,
) -> int:
    """Record one closed bar per symbol. Disabled calls record nothing."""
    if not enabled:
        return 0
    settings = load_config_ext()
    timeframe = settings.learning.timeframe
    digest = hashlib.sha256(
        json.dumps(
            {
                "learning": settings.learning.model_dump(),
                "atr_k": settings.chart.atr_k,
                "rr": settings.chart.rr,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    chosen = symbols if symbols is not None else whitelist(config_path)
    database = connect(db_path or (ROOT / "data" / "learning.db"))
    recorded = 0
    try:
        events = _events()
        for symbol, asset_class in chosen:
            bars = load_bars(symbol, timeframe, bar_root)
            if len(bars) < 2:
                continue
            closes = np.array([bar.close for bar in bars], dtype=float)
            returns = np.diff(closes) / closes[:-1]
            prices_by_tf = {timeframe: closes}
            for other in ("M15", "H1", "H4", "D1"):
                if other == timeframe:
                    continue
                extra = load_bars(symbol, other, bar_root)
                if len(extra) >= 20:
                    prices_by_tf[other] = np.array([bar.close for bar in extra], dtype=float)
            context: dict = {"prices": closes, "returns": returns, "prices_by_tf": prices_by_tf}
            upstream: dict[str, dict] = {}
            bar_time = bars[-1].time
            for agent_id in PRICE_AGENTS:
                if agent_id == "08":
                    context = {**context, "confluence": {"rule_statuses": upstream}}
                result = AGENT_MODULES[agent_id].run(context)
                _record(
                    database,
                    symbol=symbol,
                    timeframe=timeframe,
                    bar_time=bar_time,
                    agent_id=agent_id,
                    result=result,
                    config_hash=digest,
                )
                recorded += 1
                upstream[agent_id] = {
                    "triggered": direction_of(str(result.label)) != "flat",
                    "conflicting": False,
                }
            if events:
                news = AGENT_MODULES["04"].run({"events": events})
                _record(
                    database,
                    symbol=symbol,
                    timeframe=timeframe,
                    bar_time=bar_time,
                    agent_id="04",
                    result=news,
                    config_hash=digest,
                )
                recorded += 1
            sentiment = _sentiment(symbol)
            if sentiment:
                mood = AGENT_MODULES["05"].run({"sentiment_data": sentiment})
                _record(
                    database,
                    symbol=symbol,
                    timeframe=timeframe,
                    bar_time=bar_time,
                    agent_id="05",
                    result=mood,
                    config_hash=digest,
                )
                recorded += 1
            report = research(symbol, asset_class)
            fundamentals = {
                key: value
                for key, value in report.metrics.items()
                if isinstance(value, (int, float)) and report.model != "fixture" and report.status == "ok"
            }
            if fundamentals:
                fundamental = AGENT_MODULES["06"].run({"fundamental_data": fundamentals})
                _record(
                    database,
                    symbol=symbol,
                    timeframe=timeframe,
                    bar_time=bar_time,
                    agent_id="06",
                    result=fundamental,
                    config_hash=digest,
                )
                recorded += 1
            record_prediction(
                database,
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bar_time": bar_time,
                    "agent_id": "dexter",
                    "agent_version": "1",
                    "direction": "flat",
                    "score": None,
                    "confidence": 0.0,
                    "features_json": report.model_dump_json()[:8000],
                    "config_hash": digest,
                },
            )
            recorded += 1
            resolve_open(
                database,
                bars,
                symbol=symbol,
                timeframe=timeframe,
                horizons=list(settings.learning.horizons),
                atr_k=settings.chart.atr_k,
                rr=settings.chart.rr,
                atr_period=settings.learning.atr_period,
            )
    finally:
        database.close()
    return recorded
