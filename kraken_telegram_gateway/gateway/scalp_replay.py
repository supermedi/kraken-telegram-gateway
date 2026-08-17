import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, SQLModel, create_engine

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
    return [snapshot_from_mapping(row) for row in rows]


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
    timestamp = _first_value(row, ["timestamp", "time", "created_at", "datetime", "date"])
    if timestamp is None:
        raise ValueError("snapshot is missing timestamp")
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


def _load_json_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("JSON snapshot file must contain a list")
        return parsed
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(float(text), timezone.utc)
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
