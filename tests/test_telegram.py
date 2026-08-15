from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import (
    OrderRole,
    OrderStatus,
    ProcessedTelegramUpdate,
    Trade,
    TradeOrder,
)
from kraken_telegram_gateway.gateway.telegram import dispatch_telegram_text, handle_telegram_update


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_trade_message_creates_preview_and_confirm_hint():
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:40% t3=72000:20%",
            session,
            Settings(max_amount_usdc=100),
        )

    assert "Preview creee" in reply
    assert "Avertissement: Aucun stop loss" in reply
    assert "Confirmer: /confirm" in reply


def test_confirm_executes_dry_run_after_preview():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, Settings(max_amount_usdc=100))

    assert "Dry-run: no Kraken order was submitted." in confirm_reply
    assert "Statut: dry_run_executed" in confirm_reply


def test_trade_preview_creates_entry_and_reduce_only_target_orders():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:40% t3=72000:20%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    entry_orders = [order for order in orders if order.role == OrderRole.ENTRY]
    target_orders = [order for order in orders if order.role == OrderRole.TARGET_EXIT]

    assert len(entry_orders) == 1
    assert entry_orders[0].side == "buy"
    assert entry_orders[0].reduce_only is False
    assert len(target_orders) == 3
    assert {order.side for order in target_orders} == {"sell"}
    assert all(order.reduce_only for order in target_orders)
    assert sum(order.amount_usdc for order in target_orders) == 100
    assert all(order.status == OrderStatus.PLANNED for order in orders)


def test_confirm_marks_only_entry_order_as_dry_run_submitted():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, Settings(max_amount_usdc=100))
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    entry_order = next(order for order in orders if order.role == OrderRole.ENTRY)
    target_order = next(order for order in orders if order.role == OrderRole.TARGET_EXIT)

    assert entry_order.status == OrderStatus.DRY_RUN_SUBMITTED
    assert entry_order.external_order_id == f"dryrun-{trade_id}"
    assert target_order.side == "buy"
    assert target_order.reduce_only is True
    assert target_order.status == OrderStatus.PLANNED


def test_confirm_with_live_gate_open_rejects_until_instrument_metadata_exists():
    settings = Settings(
        max_amount_usdc=100,
        dry_run=False,
        live_trading_enabled=True,
        kraken_api_key="public-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
    )
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        trade = session.get(Trade, trade_id)
        entry_order = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.ENTRY)
        ).one()

    assert "Live Kraken submission blocked" in confirm_reply
    assert "Statut: rejected" in confirm_reply
    assert trade is not None
    assert trade.status == "rejected"
    assert entry_order.status == OrderStatus.PLANNED


def test_status_includes_planned_order_visibility():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        status_reply = dispatch_telegram_text(f"/status {trade_id}", session, Settings(max_amount_usdc=100))

    assert f"Trade ID: {trade_id}" in status_reply
    assert "Ordres planifies:" in status_reply
    assert "- entry: buy limit 65000 | 100 USDC | reduce-only=non | statut=planned" in status_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=planned" in status_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=planned" in status_reply


def test_allowed_user_ids_are_enforced():
    update = {
        "message": {
            "chat": {"id": 456},
            "from": {"id": 123},
            "text": "/help",
        }
    }

    with make_session() as session:
        reply = handle_telegram_update(
            update,
            session,
            Settings(telegram_allowed_user_ids="999"),
        )

    assert reply == "Utilisateur non autorise."


def test_pause_blocks_new_trades_and_resume_allows_them():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        pause_reply = dispatch_telegram_text("/pause", session, settings)
        blocked_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        status_reply = dispatch_telegram_text("/status", session, settings)
        resume_reply = dispatch_telegram_text("/resume", session, settings)
        allowed_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )

    assert "pause" in pause_reply
    assert "trading is paused" in blocked_reply
    assert status_reply == "Trading: pause"
    assert "relance" in resume_reply
    assert "Preview creee" in allowed_reply


def test_pause_blocks_confirm_but_allows_cancel():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        dispatch_telegram_text("/pause", session, settings)
        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        cancel_reply = dispatch_telegram_text(f"/cancel {trade_id}", session, settings)

    assert "Trading en pause" in confirm_reply
    assert "Trade annule" in cancel_reply


def test_telegram_update_id_is_stored_and_reused():
    update = {
        "update_id": 42,
        "message": {
            "chat": {"id": 456},
            "from": {"id": 123},
            "text": "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
        },
    }

    with make_session() as session:
        first_reply = handle_telegram_update(update, session, Settings(max_amount_usdc=100))
        second_reply = handle_telegram_update(update, session, Settings(max_amount_usdc=100))
        trades = session.exec(select(Trade)).all()
        processed = session.get(ProcessedTelegramUpdate, 42)

    assert first_reply == second_reply
    assert len(trades) == 1
    assert processed is not None
