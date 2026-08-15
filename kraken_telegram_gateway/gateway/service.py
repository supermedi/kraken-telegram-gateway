import json

from sqlmodel import Session, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import KrakenClient
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
from kraken_telegram_gateway.gateway.schemas import ConfirmResult, TradeDetail, TradePreview


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

    result = KrakenClient(settings).submit_entry_order(trade)
    trade.status = TradeStatus.DRY_RUN_EXECUTED if result["mode"] == "dry_run" else TradeStatus.LIVE_SUBMITTED
    trade.updated_at = utc_now()
    session.add(trade)
    mark_entry_orders_submitted(trade.id, result, session)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_confirmed", message=result["message"]))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=result["message"])


def cancel_trade(trade_id: str, session: Session) -> ConfirmResult:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    trade.status = TradeStatus.CANCELLED
    trade.updated_at = utc_now()
    session.add(trade)
    cancel_planned_orders(trade.id, session)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_cancelled", message="Trade cancelled by command."))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message="Trade annule.")


def get_trade_detail(trade_id: str, session: Session) -> TradeDetail | None:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return None
    return TradeDetail(trade=trade, orders=list_trade_orders(trade.id, session))


def format_trade_summary(trade: Trade) -> str:
    targets = ", ".join(
        f"{target['price']:g}:{target['percent']:g}%"
        for target in json.loads(trade.targets_json)
    )
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


def format_trade_order(order: TradeOrder) -> str:
    reduce_only = "oui" if order.reduce_only else "non"
    target = f" | target={order.target_percent:g}%" if order.target_percent is not None else ""
    external_id = f" | external_id={order.external_order_id}" if order.external_order_id else ""
    return (
        f"- {order.role}: {order.side} {order.order_type} {order.price:g} | "
        f"{order.amount_usdc:g} USDC{target} | reduce-only={reduce_only} | "
        f"statut={order.status}{external_id}"
    )


def list_trade_orders(trade_id: str, session: Session) -> list[TradeOrder]:
    orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()
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
    planned_orders = session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade_id,
            TradeOrder.status == OrderStatus.PLANNED,
        )
    ).all()
    for order in planned_orders:
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
