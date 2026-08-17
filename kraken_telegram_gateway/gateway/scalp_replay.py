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


def snapshot_from_mapping(row: dict[str, Any]) -> MarketSnapshot:
    timestamp = row.get("timestamp") or row.get("time") or row.get("created_at")
    if timestamp is None:
        raise ValueError("snapshot is missing timestamp")
    return MarketSnapshot(
        timestamp=_parse_timestamp(timestamp),
        bid=_required_float(row, "bid"),
        ask=_required_float(row, "ask"),
        bid_size=_required_float(row, "bid_size"),
        ask_size=_required_float(row, "ask_size"),
        volume_ratio=float(row.get("volume_ratio") or 1),
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


def _required_float(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        raise ValueError(f"snapshot is missing {key}")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline paper scalping replay from deterministic snapshots.")
    parser.add_argument("--command", required=True, help="Full /scalp_start command used to configure the paper session.")
    parser.add_argument("--snapshots", required=True, type=Path, help="CSV, JSON list, or JSONL snapshot file.")
    args = parser.parse_args()

    snapshots = load_market_snapshots(args.snapshots)
    result = run_scalp_replay(args.command, snapshots)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
