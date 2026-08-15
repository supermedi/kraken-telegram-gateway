from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import (
    AuditEvent,
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


def test_confirm_rejects_missing_stop_when_required():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        trade = session.get(Trade, trade_id)
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "trade_rejected")
        ).one()

    assert "stop loss requis" in confirm_reply
    assert "Statut: rejected" in confirm_reply
    assert trade is not None
    assert trade.status == "rejected"
    assert all(order.status == OrderStatus.PLANNED for order in orders)
    assert "stop loss requis" in audit.message


def test_confirm_accepts_stop_when_stop_policy_required():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:100% stop=63000",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

    assert "Dry-run: no Kraken order was submitted." in confirm_reply
    assert "Statut: dry_run_executed" in confirm_reply


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


def test_orders_command_lists_attached_orders_only():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 "
            "t1=63000:25% t2=61000:75%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        orders_reply = dispatch_telegram_text(f"/orders {trade_id}", session, Settings(max_amount_usdc=100))

    assert orders_reply.startswith(f"Ordres du trade {trade_id}\nSELL PF_XBTUSD | statut=pending_confirmation")
    assert "Trade ID:" not in orders_reply
    assert "- entry: sell limit 65000 | 100 USDC | reduce-only=non | statut=planned" in orders_reply
    assert "- target_exit: buy limit 63000 | 25 USDC | target=25% | reduce-only=oui | statut=planned" in orders_reply
    assert "- target_exit: buy limit 61000 | 75 USDC | target=75% | reduce-only=oui | statut=planned" in orders_reply


def test_orders_command_filters_status_and_role():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=planned role=target_exit",
            session,
            settings,
        )

    assert "- entry:" not in orders_reply
    assert "statut=dry_run_submitted" not in orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=planned" in orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=planned" in orders_reply


def test_entry_filled_command_marks_targets_ready_for_mobile_visibility():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        filled_reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        ready_orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=ready_to_submit role=target_exit",
            session,
            settings,
        )

    assert "aucun ordre Kraken envoye" in filled_reply
    assert "Statut: entry_filled" in filled_reply
    assert "- entry:" not in ready_orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=ready_to_submit" in ready_orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=ready_to_submit" in ready_orders_reply


def test_entry_filled_command_is_idempotent_for_mobile_retries():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)

        retry_reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        audits = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "entry_filled")
        ).all()

    assert "Aucun changement applique" in retry_reply
    assert "Statut: entry_filled" in retry_reply
    assert len(audits) == 1


def test_submit_targets_command_marks_ready_targets_dry_run_submitted():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)

        submit_reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        submitted_orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=dry_run_submitted role=target_exit",
            session,
            settings,
        )
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "targets_submitted")
        ).one()

    assert "2 target(s) reduce-only" in submit_reply
    assert "aucun ordre Kraken envoye" in submit_reply
    assert "Statut: entry_filled" in submit_reply
    assert "- entry:" not in submitted_orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=dry_run_submitted" in submitted_orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=dry_run_submitted" in submitted_orders_reply
    assert "Dry-run: 2 target(s)" in audit.message


def test_submit_targets_command_retry_is_idempotent_for_mobile_operators():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        first_orders = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.TARGET_EXIT)
        ).all()

        retry_reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        retry_orders = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.TARGET_EXIT)
        ).all()
        audits = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "targets_submitted")
        ).all()

    assert "Targets deja soumises: 2 target(s)" in retry_reply
    assert "Aucun changement applique" in retry_reply
    assert "Statut: entry_filled" in retry_reply
    assert {order.status for order in retry_orders} == {OrderStatus.DRY_RUN_SUBMITTED}
    assert [order.external_order_id for order in retry_orders] == [order.external_order_id for order in first_orders]
    assert len(audits) == 1


def test_submit_targets_command_rejects_before_entry_fill():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)

    assert "l'entree doit etre marquee filled" in reply
    assert "Statut: dry_run_executed" in reply


def test_entry_filled_command_rejects_unconfirmed_trade():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    assert "doit d'abord etre confirme" in reply
    assert "Statut: pending_confirmation" in reply
    assert {order.status for order in orders} == {OrderStatus.PLANNED}


def test_orders_command_rejects_invalid_filter():
    with make_session() as session:
        reply = dispatch_telegram_text("/orders trade-1 status=bad", session, Settings(max_amount_usdc=100))

    assert "'bad' is not a valid OrderStatus" in reply


def test_orders_command_handles_missing_trade():
    with make_session() as session:
        reply = dispatch_telegram_text("/orders missing", session, Settings(max_amount_usdc=100))

    assert reply == "Trade introuvable."


def test_trades_command_lists_recent_trades_for_mobile_visibility():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        first_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        second_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100%",
            session,
            settings,
        )
        first_trade_id = next(
            line.split(": ", 1)[1] for line in first_preview.splitlines() if line.startswith("Trade ID:")
        )
        second_trade_id = next(
            line.split(": ", 1)[1] for line in second_preview.splitlines() if line.startswith("Trade ID:")
        )

        reply = dispatch_telegram_text("/trades limit=2", session, settings)

    assert reply.startswith("Trades recents 1-2/2:")
    assert f"- {second_trade_id} | pending_confirmation | SELL PF_ETHUSD | 50 USDC | entry=3500" in reply
    assert f"- {first_trade_id} | pending_confirmation | BUY PF_XBTUSD | 100 USDC | entry=65000" in reply
    assert reply.index(second_trade_id) < reply.index(first_trade_id)


def test_trades_command_filters_status_and_pair():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        xbt_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        eth_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100%",
            session,
            settings,
        )
        xbt_trade_id = next(
            line.split(": ", 1)[1] for line in xbt_preview.splitlines() if line.startswith("Trade ID:")
        )
        eth_trade_id = next(
            line.split(": ", 1)[1] for line in eth_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/cancel {xbt_trade_id}", session, settings)

        reply = dispatch_telegram_text("/trades status=cancelled pair=pf_xbtusd", session, settings)

    assert reply.startswith("Trades recents 1-1/1:")
    assert xbt_trade_id in reply
    assert eth_trade_id not in reply
    assert "cancelled | BUY PF_XBTUSD" in reply


def test_trades_command_rejects_invalid_limit():
    with make_session() as session:
        reply = dispatch_telegram_text("/trades limit=25", session, Settings(max_amount_usdc=100))

    assert reply == "Commande refusee: /trades limit must be between 1 and 10"


def test_audit_command_lists_and_filters_safety_events():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/audit {trade_id} event_type=trade_rejected", session, settings)

    assert reply.startswith("Audit 1-1/1:")
    assert f"- trade_rejected | trade={trade_id}" in reply
    assert "stop loss requis" in reply
    assert "trade_preview" not in reply


def test_audit_command_rejects_invalid_limit():
    with make_session() as session:
        reply = dispatch_telegram_text("/audit limit=25", session, Settings(max_amount_usdc=100))

    assert reply == "Commande refusee: /audit limit must be between 1 and 10"


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
