from datetime import timedelta
import json

from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.models import ScalpSession, ScalpSessionStatus, ScalpSignal, ScalpTrade, ScalpTradeStatus
from kraken_telegram_gateway.gateway.scalping import MarketSnapshot
from kraken_telegram_gateway.gateway.scalp_replay import load_market_snapshots, run_scalp_replay, run_scalp_replay_batch
from kraken_telegram_gateway.gateway.service import (
    run_active_scalp_paper_sessions,
    run_scalp_paper_snapshots,
    start_scalp_session,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_scalp_paper_runner_opens_and_closes_profitable_trade_from_synthetic_book():
    with make_session() as session:
        result = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m min_pnl=1",
            session,
        )
        scalp_session = session.get(ScalpSession, result.session_id)
        assert scalp_session is not None
        start = scalp_session.started_at

        run_result = run_scalp_paper_snapshots(
            result.session_id,
            [
                MarketSnapshot(start + timedelta(seconds=1), bid=10, ask=10.01, bid_size=700, ask_size=300, volume_ratio=1.6),
                MarketSnapshot(start + timedelta(seconds=90), bid=10.08, ask=10.09, bid_size=500, ask_size=500, volume_ratio=1.0),
            ],
            session,
        )
        trades = session.exec(select(ScalpTrade)).all()
        signals = session.exec(select(ScalpSignal)).all()

    assert run_result.status == ScalpSessionStatus.PAPER_ACTIVE
    assert run_result.message == "Paper runner: 1 trade(s) ouverts, 1 trade(s) fermes."
    assert len(signals) == 1
    assert signals[0].signal_kind == "book_volume_v1"
    assert signals[0].scalp_trade_id == trades[0].id
    assert len(trades) == 1
    assert trades[0].side == "buy"
    assert trades[0].status == ScalpTradeStatus.PAPER_CLOSED
    assert trades[0].close_reason == "min_net_pnl"
    assert trades[0].net_pnl is not None
    assert trades[0].net_pnl > 1


def test_scalp_paper_runner_stops_after_max_losses():
    with make_session() as session:
        result = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m max_losses=1 min_pnl=1",
            session,
        )
        scalp_session = session.get(ScalpSession, result.session_id)
        assert scalp_session is not None
        start = scalp_session.started_at

        run_result = run_scalp_paper_snapshots(
            result.session_id,
            [
                MarketSnapshot(start + timedelta(seconds=1), bid=10, ask=10.01, bid_size=700, ask_size=300, volume_ratio=1.6),
                MarketSnapshot(start + timedelta(seconds=90), bid=9.92, ask=9.93, bid_size=500, ask_size=500, volume_ratio=1.0),
                MarketSnapshot(start + timedelta(seconds=120), bid=10, ask=10.01, bid_size=700, ask_size=300, volume_ratio=1.6),
            ],
            session,
        )
        scalp_session = session.get(ScalpSession, result.session_id)
        trades = session.exec(select(ScalpTrade)).all()

    assert run_result.status == ScalpSessionStatus.COMPLETED
    assert scalp_session is not None
    assert scalp_session.status == ScalpSessionStatus.COMPLETED
    assert scalp_session.stop_reason == "max_losses"
    assert len(trades) == 1
    assert trades[0].status == ScalpTradeStatus.PAPER_CLOSED
    assert trades[0].close_reason == "max_loss_per_trade"
    assert trades[0].net_pnl is not None
    assert trades[0].net_pnl < 0


def test_scalp_paper_runner_closes_open_trade_on_max_hold():
    with make_session() as session:
        result = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 duration=60m max_hold=2m min_pnl=5",
            session,
        )
        scalp_session = session.get(ScalpSession, result.session_id)
        assert scalp_session is not None
        start = scalp_session.started_at

        run_scalp_paper_snapshots(
            result.session_id,
            [
                MarketSnapshot(start + timedelta(seconds=1), bid=10, ask=10.01, bid_size=700, ask_size=300, volume_ratio=1.6),
                MarketSnapshot(start + timedelta(seconds=121), bid=10.02, ask=10.03, bid_size=500, ask_size=500, volume_ratio=1.0),
            ],
            session,
        )
        trade = session.exec(select(ScalpTrade)).one()

    assert trade.status == ScalpTradeStatus.PAPER_CLOSED
    assert trade.close_reason == "max_hold"


def test_scalp_scheduler_runs_active_sessions_with_snapshot_provider():
    with make_session() as session:
        first = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m min_pnl=1",
            session,
        )
        stopped = start_scalp_session(
            "/scalp_start pair=PF_ETHUSD amount_usdc=100 duration=60m max_hold=5m min_pnl=1",
            session,
        )
        stopped_session = session.get(ScalpSession, stopped.session_id)
        assert stopped_session is not None
        stopped_session.status = ScalpSessionStatus.STOPPED
        session.add(stopped_session)
        session.commit()

        def snapshot_provider(scalp_session: ScalpSession) -> list[MarketSnapshot]:
            start = scalp_session.started_at
            return [
                MarketSnapshot(start + timedelta(seconds=1), bid=10, ask=10.01, bid_size=700, ask_size=300, volume_ratio=1.6),
                MarketSnapshot(start + timedelta(seconds=90), bid=10.08, ask=10.09, bid_size=500, ask_size=500, volume_ratio=1.0),
            ]

        result = run_active_scalp_paper_sessions(session, snapshot_provider)
        trades = session.exec(select(ScalpTrade)).all()

    assert result.scanned == 1
    assert result.processed == 1
    assert result.skipped == 0
    assert first.session_id in result.messages[0]
    assert len(trades) == 1
    assert trades[0].session_id == first.session_id
    assert trades[0].status == ScalpTradeStatus.PAPER_CLOSED


def test_scalp_scheduler_skips_active_session_without_snapshots():
    with make_session() as session:
        result = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 duration=60m max_hold=5m",
            session,
        )

        scheduler_result = run_active_scalp_paper_sessions(session, lambda _: [])
        trades = session.exec(select(ScalpTrade)).all()

    assert scheduler_result.scanned == 1
    assert scheduler_result.processed == 0
    assert scheduler_result.skipped == 1
    assert result.session_id in scheduler_result.messages[0]
    assert trades == []


def test_scalp_replay_runs_offline_report_from_json_snapshots(tmp_path):
    snapshots_path = tmp_path / "snapshots.json"
    snapshots_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-08-17T13:45:00Z",
                    "bid": 10,
                    "ask": 10.01,
                    "bid_size": 700,
                    "ask_size": 300,
                    "volume_ratio": 1.6,
                },
                {
                    "timestamp": "2026-08-17T13:46:30Z",
                    "bid": 10.08,
                    "ask": 10.09,
                    "bid_size": 500,
                    "ask_size": 500,
                    "volume_ratio": 1.0,
                },
            ]
        ),
        encoding="utf-8",
    )

    snapshots = load_market_snapshots(snapshots_path)
    result = run_scalp_replay(
        "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m min_pnl=1",
        snapshots,
    )

    assert result["runner"]["status"] == ScalpSessionStatus.PAPER_ACTIVE
    assert result["report"]["closed_trades"] == 1
    assert result["report"]["wins"] == 1
    assert result["report"]["net_pnl"] > 1


def test_scalp_replay_loads_csv_snapshots(tmp_path):
    snapshots_path = tmp_path / "snapshots.csv"
    snapshots_path.write_text(
        "\n".join(
            [
                "timestamp,bid,ask,bid_size,ask_size,volume_ratio",
                "2026-08-17T13:45:00Z,10,10.01,700,300,1.6",
            ]
        ),
        encoding="utf-8",
    )

    snapshots = load_market_snapshots(snapshots_path)

    assert len(snapshots) == 1
    assert snapshots[0].bid == 10
    assert snapshots[0].volume_ratio == 1.6


def test_scalp_replay_loads_common_historical_book_aliases(tmp_path):
    snapshots_path = tmp_path / "historical_book.csv"
    snapshots_path.write_text(
        "\n".join(
            [
                "datetime,best_bid,best_ask,bid_qty,ask_qty,volumeRatio",
                "2026-08-17T13:45:00Z,10,10.01,700,300,1.6",
            ]
        ),
        encoding="utf-8",
    )

    snapshots = load_market_snapshots(snapshots_path)

    assert len(snapshots) == 1
    assert snapshots[0].bid == 10
    assert snapshots[0].ask == 10.01
    assert snapshots[0].bid_size == 700
    assert snapshots[0].ask_size == 300
    assert snapshots[0].volume_ratio == 1.6


def test_scalp_replay_batch_summarizes_multiple_snapshot_files(tmp_path):
    profitable_path = tmp_path / "profitable.jsonl"
    profitable_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-17T13:45:00Z",
                        "bid": 10,
                        "ask": 10.01,
                        "bid_size": 700,
                        "ask_size": 300,
                        "volume_ratio": 1.6,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-17T13:46:30Z",
                        "bid": 10.08,
                        "ask": 10.09,
                        "bid_size": 500,
                        "ask_size": 500,
                        "volume_ratio": 1.0,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    losing_path = tmp_path / "losing.csv"
    losing_path.write_text(
        "\n".join(
            [
                "timestamp,bid,ask,bid_size,ask_size,volume_ratio",
                "2026-08-17T13:45:00Z,10,10.01,700,300,1.6",
                "2026-08-17T13:46:30Z,9.92,9.93,500,500,1.0",
            ]
        ),
        encoding="utf-8",
    )

    result = run_scalp_replay_batch(
        "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m max_losses=1 min_pnl=1",
        [profitable_path, losing_path],
    )

    assert [run["source"] for run in result["runs"]] == [str(profitable_path), str(losing_path)]
    assert result["summary"]["replays"] == 2
    assert result["summary"]["closed_trades"] == 2
    assert result["summary"]["wins"] == 1
    assert result["summary"]["losses"] == 1
    assert result["summary"]["win_rate"] == 50
    assert result["summary"]["close_reasons"] == {"min_net_pnl": 1, "max_loss_per_trade": 1}
    assert result["summary"]["stop_reasons"] == {"max_losses": 1}
    assert result["summary"]["avg_net_pnl_per_replay"] == result["summary"]["net_pnl"] / 2
