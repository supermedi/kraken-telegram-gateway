import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine

from kraken_telegram_gateway.gateway.market_data import KrakenFuturesBook
from kraken_telegram_gateway.gateway.scalping import MarketSnapshot
from kraken_telegram_gateway.gateway.service import (
    get_scalp_session_report,
    run_scalp_paper_snapshots,
    start_scalp_session,
)


def load_market_snapshots(path: Path) -> list[MarketSnapshot]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = _load_json_rows(path)
    return snapshots_from_rows(rows)


def snapshots_from_rows(rows: list[Any]) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    kraken_books: dict[str, KrakenFuturesBook] = {}
    for row in rows:
        if isinstance(row, dict) and _is_kraken_futures_ws_message(row):
            product_id = str(row["product_id"])
            book = kraken_books.setdefault(product_id, KrakenFuturesBook(product_id))
            snapshot = book.apply(row)
            if snapshot is not None:
                snapshots.append(snapshot)
            else:
                print(f"DEBUG: snapshot is None for row: {row}")
            continue
        snapshots.append(snapshot_from_row(row))
    return snapshots


def snapshot_from_row(row: Any) -> MarketSnapshot:
    if isinstance(row, dict):
        if isinstance(row.get("k"), dict):
            return snapshot_from_mapping(row["k"])
        return snapshot_from_mapping(row)
    if isinstance(row, (list, tuple)):
        return snapshot_from_ohlcv_sequence(row)
    raise ValueError("snapshot row must be an object or OHLCV array")


def run_scalp_replay(command: str, snapshots: list[MarketSnapshot]) -> dict[str, Any]:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        started = start_scalp_session(command, session)
        if started.session_id == "":
            raise ValueError(started.message)
        run_result = run_scalp_paper_snapshots(started.session_id, snapshots, session)
        report = get_scalp_session_report(started.session_id, session)
        if report is None:
            raise ValueError("scalp replay report could not be built")
        return {
            "session_id": started.session_id,
            "runner": run_result.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
        }


def run_scalp_replay_batch(command: str, snapshot_paths: list[Path]) -> dict[str, Any]:
    runs = []
    for path in snapshot_paths:
        replay = run_scalp_replay(command, load_market_snapshots(path))
        replay["source"] = str(path)
        runs.append(replay)
    return {
        "runs": runs,
        "summary": summarize_scalp_replays(runs),
    }


def summarize_scalp_replays(runs: list[dict[str, Any]]) -> dict[str, Any]:
    close_reasons: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    totals = {
        "replays": len(runs),
        "closed_trades": 0,
        "open_trades": 0,
        "wins": 0,
        "losses": 0,
        "gross_pnl": 0.0,
        "estimated_fees": 0.0,
        "net_pnl": 0.0,
        "max_drawdown": 0.0,
        "rejected_signals": 0,
    }
    for run in runs:
        report = run["report"]
        totals["closed_trades"] += report["closed_trades"]
        totals["open_trades"] += report["open_trades"]
        totals["wins"] += report["wins"]
        totals["losses"] += report["losses"]
        totals["gross_pnl"] += report["gross_pnl"]
        totals["estimated_fees"] += report["estimated_fees"]
        totals["net_pnl"] += report["net_pnl"]
        totals["max_drawdown"] = max(totals["max_drawdown"], report["max_drawdown"])
        totals["rejected_signals"] += report["rejected_signals"]
        for reason, count in report["close_reasons"].items():
            close_reasons[reason] = close_reasons.get(reason, 0) + count
        if report.get("stop_reason"):
            stop_reason = report["stop_reason"]
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1

    totals["win_rate"] = (totals["wins"] / totals["closed_trades"] * 100) if totals["closed_trades"] else 0.0
    totals["avg_net_pnl_per_replay"] = totals["net_pnl"] / len(runs) if runs else 0.0
    totals["close_reasons"] = close_reasons
    totals["stop_reasons"] = stop_reasons
    return totals


def snapshot_from_mapping(row: dict[str, Any]) -> MarketSnapshot:
    timestamp = _first_value(
        row,
        ["timestamp", "time", "created_at", "datetime", "date", "event_time", "eventTime", "open_time", "openTime", "E", "t"],
    )
    if timestamp is None:
        raise ValueError("snapshot is missing timestamp")
    book_snapshot = _snapshot_from_order_book_mapping(row, timestamp)
    if book_snapshot is not None:
        return book_snapshot
    ohlcv_snapshot = _snapshot_from_ohlcv_mapping(row, timestamp)
    if ohlcv_snapshot is not None:
        return ohlcv_snapshot
    return MarketSnapshot(
        timestamp=_parse_timestamp(timestamp),
        bid=_required_float(row, "bid", aliases=["best_bid", "bid_price", "bidPrice"]),
        ask=_required_float(row, "ask", aliases=["best_ask", "ask_price", "askPrice"]),
        bid_size=_required_float(
            row,
            "bid_size",
            aliases=["bid_qty", "bidQty", "bid_volume", "bidVolume", "bidsize"],
        ),
        ask_size=_required_float(
            row,
            "ask_size",
            aliases=["ask_qty", "askQty", "ask_volume", "askVolume", "asksize"],
        ),
        volume_ratio=_optional_float(row, ["volume_ratio", "volumeRatio", "volume_ratio_1m"], default=1),
    )


def snapshot_from_ohlcv_sequence(row: list[Any] | tuple[Any, ...]) -> MarketSnapshot:
    if len(row) < 6:
        raise ValueError("OHLCV array snapshot must contain timestamp, open, high, low, close, and volume")
    return _snapshot_from_ohlcv_values(
        timestamp=row[0],
        open_price=row[1],
        high=row[2],
        low=row[3],
        close=row[4],
        volume=row[5],
    )


def _load_json_rows(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("JSON snapshot file must contain a list")
        return parsed
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _is_kraken_futures_ws_message(row: dict[str, Any]) -> bool:
    return row.get("feed") in {"book_snapshot", "book", "ticker_lite"} and bool(row.get("product_id"))


def _snapshot_from_order_book_mapping(row: dict[str, Any], timestamp: Any) -> MarketSnapshot | None:
    bids = _first_value(row, ["bids", "bid_levels", "bidLevels", "b"])
    asks = _first_value(row, ["asks", "ask_levels", "askLevels", "a"])
    if bids is None or asks is None:
        return None

    best_bid = _best_book_level(bids, side="bid")
    best_ask = _best_book_level(asks, side="ask")
    if best_bid is None or best_ask is None:
        raise ValueError("order book snapshot is missing usable bid/ask levels")

    return MarketSnapshot(
        timestamp=_parse_timestamp(timestamp),
        bid=best_bid[0],
        ask=best_ask[0],
        bid_size=best_bid[1],
        ask_size=best_ask[1],
        volume_ratio=_optional_float(row, ["volume_ratio", "volumeRatio", "volume_ratio_1m"], default=1),
    )


def _snapshot_from_ohlcv_mapping(row: dict[str, Any], timestamp: Any) -> MarketSnapshot | None:
    close = _first_value(row, ["close", "Close", "c"])
    volume = _first_value(row, ["volume", "Volume", "vol", "base_volume", "baseVolume", "v"])
    if close is None or volume is None:
        return None
    return _snapshot_from_ohlcv_values(
        timestamp=timestamp,
        open_price=_first_value(row, ["open", "Open", "o"]),
        high=_first_value(row, ["high", "High", "h"]),
        low=_first_value(row, ["low", "Low", "l"]),
        close=close,
        volume=volume,
        spread_bps=_first_value(row, ["spread_bps", "spreadBps"]),
        volume_ratio=_first_value(row, ["volume_ratio", "volumeRatio", "volume_ratio_1m"]),
        buy_volume=_first_value(
            row,
            [
                "buy_volume",
                "buyVolume",
                "taker_buy_volume",
                "takerBuyVolume",
                "taker_buy_base_asset_volume",
                "takerBuyBaseAssetVolume",
                "V",
            ],
        ),
    )


def _snapshot_from_ohlcv_values(
    *,
    timestamp: Any,
    open_price: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any,
    spread_bps: Any = None,
    volume_ratio: Any = None,
    buy_volume: Any = None,
) -> MarketSnapshot:
    close_price = float(close)
    if close_price <= 0:
        raise ValueError("OHLCV snapshot close must be positive")
    spread = close_price * (float(spread_bps) if spread_bps is not None else 5) / 10_000
    total_volume = max(float(volume), 1.0)
    bid_size, ask_size = _synthetic_ohlcv_sizes(
        open_price=open_price,
        high=high,
        low=low,
        close=close,
        volume=total_volume,
        buy_volume=buy_volume,
    )
    return MarketSnapshot(
        timestamp=_parse_timestamp(timestamp),
        bid=close_price - spread / 2,
        ask=close_price + spread / 2,
        bid_size=bid_size,
        ask_size=ask_size,
        volume_ratio=float(volume_ratio) if volume_ratio is not None else 1,
    )


def _synthetic_ohlcv_sizes(
    *,
    open_price: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: float,
    buy_volume: Any,
) -> tuple[float, float]:
    if buy_volume is not None:
        bid_size = max(min(float(buy_volume), volume), 0.0)
        ask_size = max(volume - bid_size, 0.0)
        return _ensure_positive_sizes(bid_size, ask_size)

    if open_price is None:
        return volume / 2, volume / 2

    open_value = float(open_price)
    close_value = float(close)
    if high is not None and low is not None and float(high) > float(low):
        direction = (close_value - open_value) / (float(high) - float(low))
    elif open_value > 0:
        direction = (close_value - open_value) / open_value
    else:
        direction = 0.0
    imbalance = max(min(direction, 0.8), -0.8)
    bid_size = volume * (1 + imbalance) / 2
    ask_size = volume - bid_size
    return _ensure_positive_sizes(bid_size, ask_size)


def _ensure_positive_sizes(bid_size: float, ask_size: float) -> tuple[float, float]:
    if bid_size <= 0 and ask_size <= 0:
        return 1.0, 1.0
    if bid_size <= 0:
        return 0.00000001, ask_size
    if ask_size <= 0:
        return bid_size, 0.00000001
    return bid_size, ask_size


def _best_book_level(raw_levels: Any, *, side: str) -> tuple[float, float] | None:
    levels = _coerce_book_levels(raw_levels)
    parsed_levels = [_parse_book_level(level) for level in levels]
    usable_levels = [level for level in parsed_levels if level is not None and level[0] > 0 and level[1] > 0]
    if not usable_levels:
        return None
    return max(usable_levels, key=lambda level: level[0]) if side == "bid" else min(usable_levels, key=lambda level: level[0])


def _coerce_book_levels(raw_levels: Any) -> list[Any]:
    if isinstance(raw_levels, str):
        text = raw_levels.strip()
        if not text:
            return []
        parsed = json.loads(text)
        return _coerce_book_levels(parsed)
    if isinstance(raw_levels, dict):
        return [{"price": price, "qty": qty} for price, qty in raw_levels.items()]
    if isinstance(raw_levels, list):
        return raw_levels
    return []


def _parse_book_level(level: Any) -> tuple[float, float] | None:
    if isinstance(level, dict):
        price = _first_value(level, ["price", "px", "rate", "0"])
        qty = _first_value(level, ["qty", "quantity", "size", "amount", "volume", "1"])
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price = level[0]
        qty = level[1]
    else:
        return None
    if price is None or qty is None:
        return None
    return float(price), float(qty)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) > 10_000_000_000:
            seconds = seconds / 1000
        return datetime.fromtimestamp(seconds, timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        seconds = float(text)
        if abs(seconds) > 10_000_000_000:
            seconds = seconds / 1000
        return datetime.fromtimestamp(seconds, timezone.utc)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _optional_float(row: dict[str, Any], keys: list[str], *, default: float) -> float:
    value = _first_value(row, keys)
    if value is None:
        return default
    return float(value)


def _required_float(row: dict[str, Any], key: str, *, aliases: list[str] | None = None) -> float:
    keys = [key, *(aliases or [])]
    value = _first_value(row, keys)
    if value is None or value == "":
        raise ValueError(f"snapshot is missing {key}")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline paper scalping replay from deterministic snapshots.")
    parser.add_argument("--command", required=True, help="Full /scalp_start command used to configure the paper session.")
    parser.add_argument(
        "--snapshots",
        required=True,
        nargs="+",
        type=Path,
        help="One or more CSV, JSON list, or JSONL snapshot files.",
    )
    args = parser.parse_args()

    if len(args.snapshots) == 1:
        result = run_scalp_replay(args.command, load_market_snapshots(args.snapshots[0]))
    else:
        result = run_scalp_replay_batch(args.command, args.snapshots)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
