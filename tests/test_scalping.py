from datetime import timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.models import ScalpSession, ScalpSessionStatus, ScalpSignal, ScalpTrade, ScalpTradeStatus
from kraken_telegram_gateway.gateway.scalping import MarketSnapshot
from kraken_telegram_gateway.gateway.service import run_scalp_paper_snapshots, start_scalp_session


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
