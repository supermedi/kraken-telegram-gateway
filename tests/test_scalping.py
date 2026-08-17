from datetime import timedelta
from decimal import Decimal
import json

from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import KrakenFill
from kraken_telegram_gateway.gateway.models import ScalpSession, ScalpSessionStatus, ScalpSignal, ScalpTrade, ScalpTradeStatus
from kraken_telegram_gateway.gateway.models import AuditEvent
from kraken_telegram_gateway.gateway.scalping import MarketSnapshot
from kraken_telegram_gateway.gateway.scalp_replay import (
    load_market_snapshots,
    run_scalp_replay,
    run_scalp_replay_batch,
    snapshots_from_rows,
)
from kraken_telegram_gateway.gateway.service import (
    run_active_scalp_paper_sessions,
    run_scalp_live_snapshots,
    run_scalp_paper_snapshots,
    start_scalp_session,
    sync_scalp_entry_fills,
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


def test_scalp_live_runner_submits_entry_order_when_signal_passes(monkeypatch):
    calls = []

    def fake_submit_scalp_entry_order(self, scalp_session, side, price):
        calls.append((scalp_session.id, side, price))
        return {
            "mode": "live",
            "external_order_id": "OID-SCALP-1",
            "message": "Live Kraken scalp entry order submitted.",
        }

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.submit_scalp_entry_order",
        fake_submit_scalp_entry_order,
    )
    settings = Settings(
        scalp_live_enabled=True,
        scalp_live_max_amount_usdc=25,
        dry_run=False,
        live_trading_enabled=True,
        kraken_api_key="public-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
        allowed_pairs="PF_LINKUSD",
    )
    with make_session() as session:
        result = start_scalp_session(
            "/scalp_start pair=PF_LINKUSD amount_usdc=10 leverage=1 mode=live side=buy duration=60m max_hold=5m",
            session,
            settings,
        )
        scalp_session = session.get(ScalpSession, result.session_id)
        assert scalp_session is not None
        start = scalp_session.started_at

        run_result = run_scalp_live_snapshots(
            result.session_id,
            [
                MarketSnapshot(
                    start + timedelta(seconds=1),
                    bid=10,
                    ask=10.01,
                    bid_size=700,
                    ask_size=300,
                    volume_ratio=1.6,
                )
            ],
            session,
            settings,
        )
        trades = session.exec(select(ScalpTrade)).all()
        signals = session.exec(select(ScalpSignal)).all()

    assert run_result.status == ScalpSessionStatus.LIVE_ACTIVE
    assert run_result.message == "Live runner: 1 ordre(s) envoyes, 0 ordre(s) bloques."
    assert calls == [(result.session_id, "buy", 10.01)]
    assert len(trades) == 1
    assert trades[0].status == ScalpTradeStatus.LIVE_SUBMITTED
    assert trades[0].external_order_id == "OID-SCALP-1"
    assert signals[0].scalp_trade_id == trades[0].id


def test_sync_scalp_entry_fills_marks_live_submitted_trade_filled(monkeypatch):
    def fake_fetch_recent_fills(self):
        return [
            KrakenFill(
                order_id="OID-SCALP-1",
                symbol="PF_LINKUSD",
                side="buy",
                price=Decimal("10.02"),
                size=Decimal("1.5"),
                fill_id="fill-1",
                fill_time="2026-08-17T21:50:00.000Z",
            )
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_recent_fills",
        fake_fetch_recent_fills,
    )
    settings = Settings(
        kraken_api_key="public-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
    )
    with make_session() as session:
        scalp_session = ScalpSession(
            id="scalp-live-1",
            pair="PF_LINKUSD",
            side_mode="buy",
            amount_usdc=10,
            leverage=1,
            duration_seconds=3600,
            max_hold_seconds=300,
            max_losses=1,
            min_net_pnl=1,
            mode="live",
            status=ScalpSessionStatus.LIVE_ACTIVE,
        )
        trade = ScalpTrade(
            id="scalp-trade-1",
            session_id=scalp_session.id,
            pair="PF_LINKUSD",
            side="buy",
            amount_usdc=10,
            leverage=1,
            entry_price=10.01,
            status=ScalpTradeStatus.LIVE_SUBMITTED,
            external_order_id="OID-SCALP-1",
        )
        session.add(scalp_session)
        session.add(trade)
        session.commit()

        result = sync_scalp_entry_fills(scalp_session.id, session, settings)
        refreshed_trade = session.get(ScalpTrade, trade.id)
        audit = session.exec(select(AuditEvent).where(AuditEvent.event_type == "scalp_live_entry_filled")).one()

    assert result.scanned == 1
    assert result.filled == 1
    assert result.skipped == 0
    assert refreshed_trade is not None
    assert refreshed_trade.status == ScalpTradeStatus.LIVE_ENTRY_FILLED
    assert refreshed_trade.entry_price == 10.02
    assert refreshed_trade.opened_at.isoformat() == "2026-08-17T21:50:00"
    assert "OID-SCALP-1" in audit.message


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


def test_scalp_replay_loads_raw_kraken_futures_ws_messages():
    snapshots = snapshots_from_rows(
        [
            {
                "feed": "book_snapshot",
                "product_id": "PF_LINKUSD",
                "timestamp": 1786988700000,
                "bids": [{"price": "10", "qty": "700"}],
                "asks": [{"price": "10.01", "qty": "300"}],
            },
            {
                "feed": "book",
                "product_id": "PF_LINKUSD",
                "timestamp": 1786988701000,
                "side": "buy",
                "price": "10.02",
                "qty": "900",
            },
            {
                "feed": "ticker_lite",
                "product_id": "PF_LINKUSD",
                "timestamp": 1786988702000,
                "bid": "10.02",
                "ask": "10.04",
                "volume": "100",
            },
            {
                "feed": "ticker_lite",
                "product_id": "PF_LINKUSD",
                "timestamp": 1786988703000,
                "bid": "10.03",
                "ask": "10.04",
                "volume": "160",
            },
        ]
    )

    assert len(snapshots) == 4
    assert snapshots[0].bid == 10
    assert snapshots[0].ask == 10.01
    assert snapshots[1].bid == 10.02
    assert snapshots[-1].timestamp.isoformat() == "2026-08-17T17:45:03+00:00"
    assert snapshots[-1].volume_ratio == 1.6


def test_scalp_replay_loads_nested_order_book_exports():
    snapshots = snapshots_from_rows(
        [
            {
                "timestamp": "2026-08-17T13:45:00Z",
                "bids": [["9.99", "100"], ["10.00", "700"]],
                "asks": [["10.02", "400"], ["10.01", "300"]],
                "volumeRatio": "1.6",
            },
            {
                "E": 1786988701000,
                "b": [["10.02", "900"], ["10.01", "200"]],
                "a": [["10.04", "350"], ["10.05", "500"]],
            },
        ]
    )

    assert len(snapshots) == 2
    assert snapshots[0].bid == 10
    assert snapshots[0].ask == 10.01
    assert snapshots[0].bid_size == 700
    assert snapshots[0].ask_size == 300
    assert snapshots[0].volume_ratio == 1.6
    assert snapshots[1].timestamp.isoformat() == "2026-08-17T17:45:01+00:00"
    assert snapshots[1].bid == 10.02
    assert snapshots[1].ask == 10.04


def test_scalp_replay_loads_ccxt_ohlcv_array_exports():
    snapshots = snapshots_from_rows(
        [
            [1786988700000, "10.00", "10.20", "9.95", "10.15", "1000"],
        ]
    )

    assert len(snapshots) == 1
    assert snapshots[0].timestamp.isoformat() == "2026-08-17T17:45:00+00:00"
    assert snapshots[0].bid < 10.15
    assert snapshots[0].ask > 10.15
    assert snapshots[0].bid_size > snapshots[0].ask_size
    assert snapshots[0].volume_ratio == 1


def test_scalp_replay_loads_kline_mapping_with_taker_buy_volume():
    snapshots = snapshots_from_rows(
        [
            {
                "k": {
                    "t": 1786988700000,
                    "o": "10.00",
                    "h": "10.20",
                    "l": "9.95",
                    "c": "10.15",
                    "v": "1000",
                    "V": "750",
                    "volumeRatio": "1.4",
                    "spreadBps": "2",
                }
            }
        ]
    )

    assert len(snapshots) == 1
    assert round(snapshots[0].bid, 6) == 10.148985
    assert round(snapshots[0].ask, 6) == 10.151015
    assert snapshots[0].bid_size == 750
    assert snapshots[0].ask_size == 250
    assert snapshots[0].volume_ratio == 1.4


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
