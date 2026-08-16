import json

from sqlalchemy import func
from sqlmodel import Session, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import AccountBalance, KrakenClient
from kraken_telegram_gateway.gateway.models import (
    AuditEvent,
    BotState,
    OrderRole,
    OrderStatus,
    Trade,
    TradeOrder,
    TradeStatus,
    utc_now,
)
from kraken_telegram_gateway.gateway.parser import parse_trade_command
from kraken_telegram_gateway.gateway.risk import validate_risk
from kraken_telegram_gateway.gateway.schemas import AuditEventList, ConfirmResult, TradeDetail, TradeList, TradePreview


def create_trade_preview(text: str, session: Session, settings: Settings) -> TradePreview:
    if is_trading_paused(session):
        raise ValueError("trading is paused")
    intent = parse_trade_command(text)
    warning = validate_risk(intent, settings)
    trade = Trade(
        pair=intent.pair,
        side=intent.side,
        amount_usdc=intent.amount_usdc,
        entry_type=intent.entry_type,
        entry_price=intent.entry_price,
        targets_json=json.dumps([target.model_dump() for target in intent.targets]),
        stop_price=intent.stop_price,
        leverage=intent.leverage,
        dry_run=settings.dry_run,
        warning=warning,
    )
    session.add(trade)
    for order in build_planned_orders(trade):
        session.add(order)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_preview", message="Trade parsed and validated."))
    session.commit()
    session.refresh(trade)
    return TradePreview(trade_id=trade.id, summary=format_trade_summary(trade), warning=warning, dry_run=trade.dry_run)


def confirm_trade(trade_id: str, session: Session, settings: Settings) -> ConfirmResult:
    if is_trading_paused(session):
        return ConfirmResult(trade_id=trade_id, status="paused", message="Trading en pause.")

    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status != TradeStatus.PENDING_CONFIRMATION:
        return ConfirmResult(trade_id=trade.id, status=trade.status, message="Trade deja traite.")
    if settings.require_stop_loss_for_confirmation and trade.stop_price is None:
        message = "Confirmation refusee: stop loss requis par la configuration de securite."
        trade.status = TradeStatus.REJECTED
        trade.updated_at = utc_now()
        session.add(trade)
        session.add(AuditEvent(trade_id=trade.id, event_type="trade_rejected", message=message))
        session.commit()
        return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)

    result = KrakenClient(settings).submit_entry_order(trade)
    if result["mode"] == "dry_run":
        trade.status = TradeStatus.DRY_RUN_EXECUTED
    elif result["mode"] == "blocked":
        trade.status = TradeStatus.REJECTED
    else:
        trade.status = TradeStatus.LIVE_SUBMITTED
    trade.updated_at = utc_now()
    session.add(trade)
    if result["mode"] != "blocked":
        mark_entry_orders_submitted(trade.id, result, session)
    event_type = "trade_rejected" if result["mode"] == "blocked" else "trade_confirmed"
    session.add(AuditEvent(trade_id=trade.id, event_type=event_type, message=result["message"]))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=result["message"])


def cancel_trade(trade_id: str, session: Session) -> ConfirmResult:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status == TradeStatus.CANCELLED:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Trade deja annule. Aucun changement applique.",
        )
    trade.status = TradeStatus.CANCELLED
    trade.updated_at = utc_now()
    session.add(trade)
    cancel_planned_orders(trade.id, session)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_cancelled", message="Trade cancelled by command."))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message="Trade annule.")


def mark_entry_filled(trade_id: str, session: Session) -> ConfirmResult:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status not in {
        TradeStatus.DRY_RUN_EXECUTED,
        TradeStatus.LIVE_SUBMITTED,
        TradeStatus.ENTRY_FILLED,
    }:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Entry fill refuse: le trade doit d'abord etre confirme.",
        )

    entry_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade.id, TradeOrder.role == OrderRole.ENTRY)
    ).all()
    target_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade.id, TradeOrder.role == OrderRole.TARGET_EXIT)
    ).all()

    entry_needs_update = any(order.status != OrderStatus.FILLED for order in entry_orders)
    planned_targets = [order for order in target_orders if order.status == OrderStatus.PLANNED]
    if trade.status == TradeStatus.ENTRY_FILLED and not entry_needs_update and not planned_targets:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Entry deja marquee filled. Aucun changement applique.",
        )

    for order in entry_orders:
        if order.status != OrderStatus.FILLED:
            order.status = OrderStatus.FILLED
            order.updated_at = utc_now()
            session.add(order)
    for order in planned_targets:
        order.status = OrderStatus.READY_TO_SUBMIT
        order.updated_at = utc_now()
        session.add(order)

    trade.status = TradeStatus.ENTRY_FILLED
    trade.updated_at = utc_now()
    session.add(trade)
    message = (
        "Entry marquee filled. Targets reduce-only pretes a soumettre; aucun ordre Kraken envoye."
        if planned_targets
        else "Entry marquee filled. Aucune target definie; aucun ordre Kraken envoye."
    )
    session.add(AuditEvent(trade_id=trade.id, event_type="entry_filled", message=message))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)


def submit_ready_targets(trade_id: str, session: Session, settings: Settings) -> ConfirmResult:
    if is_trading_paused(session):
        return ConfirmResult(trade_id=trade_id, status="paused", message="Trading en pause.")

    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status != TradeStatus.ENTRY_FILLED:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Soumission targets refusee: l'entree doit etre marquee filled.",
        )

    ready_targets = session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade.id,
            TradeOrder.role == OrderRole.TARGET_EXIT,
            TradeOrder.status == OrderStatus.READY_TO_SUBMIT,
        )
    ).all()
    if not ready_targets:
        submitted_targets = session.exec(
            select(TradeOrder).where(
                TradeOrder.trade_id == trade.id,
                TradeOrder.role == OrderRole.TARGET_EXIT,
                TradeOrder.status.in_([OrderStatus.DRY_RUN_SUBMITTED, OrderStatus.LIVE_SUBMITTED]),
            )
        ).all()
        if submitted_targets:
            return ConfirmResult(
                trade_id=trade.id,
                status=trade.status,
                message=(
                    f"Targets deja soumises: {len(submitted_targets)} target(s) reduce-only deja enregistrees. "
                    "Aucun changement applique."
                ),
            )
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Aucune target reduce-only prete a soumettre.",
        )

    client = KrakenClient(settings)
    submitted_count = 0
    blocked_messages = []
    for order in sorted(ready_targets, key=order_sort_key):
        result = client.submit_target_order(trade, order)
        if result["mode"] == "blocked":
            blocked_messages.append(result["message"])
            continue
        order.status = OrderStatus.DRY_RUN_SUBMITTED if result["mode"] == "dry_run" else OrderStatus.LIVE_SUBMITTED
        order.external_order_id = result.get("external_order_id")
        order.updated_at = utc_now()
        session.add(order)
        submitted_count += 1

    if submitted_count:
        message = f"Dry-run: {submitted_count} target(s) reduce-only marquees soumises; aucun ordre Kraken envoye."
        session.add(AuditEvent(trade_id=trade.id, event_type="targets_submitted", message=message))
    else:
        message = blocked_messages[0] if blocked_messages else "Aucune target reduce-only soumise."
        session.add(AuditEvent(trade_id=trade.id, event_type="targets_blocked", message=message))
    trade.updated_at = utc_now()
    session.add(trade)
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)


def get_trade_detail(trade_id: str, session: Session) -> TradeDetail | None:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return None
    return TradeDetail(trade=trade, orders=list_trade_orders(trade.id, session))


def get_trade_orders(
    trade_id: str,
    session: Session,
    *,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
) -> list[TradeOrder] | None:
    if session.get(Trade, trade_id) is None:
        return None
    return list_trade_orders(trade_id, session, status=status, role=role)


def list_trades(
    session: Session,
    *,
    status: TradeStatus | None = None,
    pair: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> TradeList:
    filters = []
    if status is not None:
        filters.append(Trade.status == status)
    if pair:
        filters.append(Trade.pair == pair.upper())

    count_statement = select(func.count()).select_from(Trade)
    if filters:
        count_statement = count_statement.where(*filters)
    total = session.exec(count_statement).one()

    trade_statement = (
        select(Trade)
        .where(*filters)
        .order_by(Trade.created_at.desc(), Trade.id.desc())
        .offset(offset)
        .limit(limit)
    )
    trades = session.exec(trade_statement).all()
    return TradeList(items=list(trades), total=total, limit=limit, offset=offset)


def list_audit_events(
    session: Session,
    *,
    trade_id: str | None = None,
    event_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AuditEventList:
    filters = []
    if trade_id:
        filters.append(AuditEvent.trade_id == trade_id)
    if event_type:
        filters.append(AuditEvent.event_type == event_type)

    count_statement = select(func.count()).select_from(AuditEvent)
    if filters:
        count_statement = count_statement.where(*filters)
    total = session.exec(count_statement).one()

    event_statement = (
        select(AuditEvent)
        .where(*filters)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    events = session.exec(event_statement).all()
    return AuditEventList(items=list(events), total=total, limit=limit, offset=offset)


def get_account_balances(
    settings: Settings,
    *,
    account: str | None = None,
    currency: str | None = None,
) -> list[AccountBalance]:
    balances = KrakenClient(settings).fetch_account_balances()
    if account:
        wanted_account = account.lower()
        balances = [balance for balance in balances if balance.account.lower() == wanted_account]
    if currency:
        wanted_currency = currency.upper()
        balances = [balance for balance in balances if balance.currency.upper() == wanted_currency]
    return balances


def format_trade_summary(trade: Trade) -> str:
    targets = ", ".join(
        f"{target['price']:g}:{target['percent']:g}%"
        for target in json.loads(trade.targets_json)
    ) or "aucune"
    stop = f"{trade.stop_price:g}" if trade.stop_price else "aucun"
    return (
        f"{trade.side.upper()} {trade.pair} | montant={trade.amount_usdc:g} USDC | "
        f"entry={trade.entry_type}:{trade.entry_price:g} | targets={targets} | "
        f"stop={stop} | leverage={trade.leverage}x"
    )


def format_trade_status(trade: Trade, orders: list[TradeOrder]) -> str:
    lines = [
        f"Trade ID: {trade.id}",
        f"Statut: {trade.status}",
        format_trade_summary(trade),
    ]
    if orders:
        lines.append("Ordres planifies:")
        lines.extend(format_trade_order(order) for order in orders)
    return "\n".join(lines)


def format_trade_orders(trade: Trade, orders: list[TradeOrder]) -> str:
    lines = [
        f"Ordres du trade {trade.id}",
        f"{trade.side.upper()} {trade.pair} | statut={trade.status}",
    ]
    if not orders:
        lines.append("Aucun ordre attache.")
    else:
        lines.extend(format_trade_order(order) for order in orders)
    return "\n".join(lines)


def format_trade_list(trades: TradeList) -> str:
    if not trades.items:
        return "Aucun trade trouve."

    start = trades.offset + 1
    end = trades.offset + len(trades.items)
    lines = [f"Trades recents {start}-{end}/{trades.total}:"]
    for trade in trades.items:
        lines.append(
            f"- {trade.id} | {trade.status} | {trade.side.upper()} {trade.pair} | "
            f"{trade.amount_usdc:g} USDC | entry={trade.entry_price:g}"
        )
    return "\n".join(lines)


def format_audit_events(events: AuditEventList) -> str:
    if not events.items:
        return "Aucun evenement d'audit trouve."

    start = events.offset + 1
    end = events.offset + len(events.items)
    lines = [f"Audit {start}-{end}/{events.total}:"]
    for event in events.items:
        trade = f" | trade={event.trade_id}" if event.trade_id else ""
        lines.append(f"- {event.event_type}{trade} | {event.message}")
    return "\n".join(lines)


def format_account_balances(balances: list[AccountBalance]) -> str:
    if not balances:
        return "Aucun solde trouve."

    lines = ["Solde Kraken Futures:"]
    for balance in sorted(balances, key=lambda item: (item.account, item.currency)):
        parts = [f"- {balance.account} {balance.currency}"]
        parts.extend(
            f"{label}={format_optional_decimal(value)}"
            for label, value in (
                ("balance", balance.balance),
                ("equity", balance.equity),
                ("available", balance.available),
                ("margin", balance.margin),
            )
            if value is not None
        )
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def format_optional_decimal(value) -> str:
    return f"{value:g}"


def format_trade_order(order: TradeOrder) -> str:
    reduce_only = "oui" if order.reduce_only else "non"
    target = f" | target={order.target_percent:g}%" if order.target_percent is not None else ""
    external_id = f" | external_id={order.external_order_id}" if order.external_order_id else ""
    return (
        f"- {order.role}: {order.side} {order.order_type} {order.price:g} | "
        f"{order.amount_usdc:g} USDC{target} | reduce-only={reduce_only} | "
        f"statut={order.status}{external_id}"
    )


def list_trade_orders(
    trade_id: str,
    session: Session,
    *,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
) -> list[TradeOrder]:
    filters = [TradeOrder.trade_id == trade_id]
    if status is not None:
        filters.append(TradeOrder.status == status)
    if role is not None:
        filters.append(TradeOrder.role == role)
    orders = session.exec(select(TradeOrder).where(*filters)).all()
    return sorted(orders, key=order_sort_key)


def build_planned_orders(trade: Trade) -> list[TradeOrder]:
    targets = json.loads(trade.targets_json)
    orders = [
        TradeOrder(
            trade_id=trade.id,
            role=OrderRole.ENTRY,
            pair=trade.pair,
            side=trade.side,
            price=trade.entry_price,
            amount_usdc=trade.amount_usdc,
            reduce_only=False,
        )
    ]
    exit_side = opposite_side(trade.side)
    for target in targets:
        orders.append(
            TradeOrder(
                trade_id=trade.id,
                role=OrderRole.TARGET_EXIT,
                pair=trade.pair,
                side=exit_side,
                price=target["price"],
                amount_usdc=trade.amount_usdc * target["percent"] / 100,
                target_percent=target["percent"],
                reduce_only=True,
            )
        )
    return orders


def order_sort_key(order: TradeOrder) -> tuple[int, float, str]:
    role_rank = 0 if order.role == OrderRole.ENTRY else 1
    target_rank = order.target_percent if order.target_percent is not None else 0
    return (role_rank, target_rank, order.id)


def mark_entry_orders_submitted(trade_id: str, result: dict[str, str], session: Session) -> None:
    status = OrderStatus.DRY_RUN_SUBMITTED if result["mode"] == "dry_run" else OrderStatus.LIVE_SUBMITTED
    entry_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.ENTRY)
    ).all()
    for order in entry_orders:
        order.status = status
        order.external_order_id = result.get("external_order_id")
        order.updated_at = utc_now()
        session.add(order)


def cancel_planned_orders(trade_id: str, session: Session) -> None:
    cancellable_orders = session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade_id,
            TradeOrder.status.in_([OrderStatus.PLANNED, OrderStatus.READY_TO_SUBMIT]),
        )
    ).all()
    for order in cancellable_orders:
        order.status = OrderStatus.CANCELLED
        order.updated_at = utc_now()
        session.add(order)


def opposite_side(side: str) -> str:
    if side == "buy":
        return "sell"
    if side == "sell":
        return "buy"
    raise ValueError(f"unsupported side: {side}")


def pause_trading(session: Session) -> str:
    set_bot_state(session, "trading_paused", "true")
    session.add(AuditEvent(event_type="trading_paused", message="Trading paused by command."))
    session.commit()
    return "Trading mis en pause. Les nouveaux trades et confirmations sont bloques."


def resume_trading(session: Session) -> str:
    set_bot_state(session, "trading_paused", "false")
    session.add(AuditEvent(event_type="trading_resumed", message="Trading resumed by command."))
    session.commit()
    return "Trading relance. Les previews et confirmations sont de nouveau autorisees."


def is_trading_paused(session: Session) -> bool:
    state = session.get(BotState, "trading_paused")
    return state is not None and state.value == "true"


def set_bot_state(session: Session, key: str, value: str) -> None:
    state = session.get(BotState, key)
    if state is None:
        state = BotState(key=key, value=value)
    else:
        state.value = value
        state.updated_at = utc_now()
    session.add(state)
