from __future__ import annotations

import httpx
from sqlmodel import Session

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import OrderRole, OrderStatus, ProcessedTelegramUpdate, TradeOrder, TradeStatus
from kraken_telegram_gateway.gateway.parser import CommandParseError
from kraken_telegram_gateway.gateway.risk import RiskValidationError
from kraken_telegram_gateway.gateway.service import (
    cancel_trade,
    confirm_trade,
    create_trade_preview,
    format_audit_events,
    format_trade_list,
    format_trade_orders,
    format_trade_status,
    get_trade_detail,
    is_trading_paused,
    list_audit_events,
    list_trades,
    mark_entry_filled,
    pause_trading,
    resume_trading,
    submit_ready_targets,
)


class TelegramUpdateError(ValueError):
    pass


def handle_telegram_update(update: dict, session: Session, settings: Settings) -> str | None:
    update_id = update.get("update_id")
    if update_id is not None:
        processed = session.get(ProcessedTelegramUpdate, update_id)
        if processed is not None:
            return processed.reply_text

    message = update.get("message") or update.get("edited_message")
    if not message:
        return None

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = (message.get("text") or "").strip()

    if chat_id is None:
        raise TelegramUpdateError("Telegram update has no chat id.")
    if not _is_allowed_user(user_id, settings):
        return "Utilisateur non autorise."
    if not text:
        return "Commande vide."

    reply = dispatch_telegram_text(text, session, settings)
    if update_id is not None:
        session.add(
            ProcessedTelegramUpdate(
                update_id=update_id,
                chat_id=str(chat_id),
                reply_text=reply,
            )
        )
        session.commit()
    return reply


def dispatch_telegram_text(text: str, session: Session, settings: Settings) -> str:
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    argument = argument.strip()

    try:
        if command == "/trade" or not command.startswith("/"):
            preview = create_trade_preview(text, session, settings)
            lines = [
                "Preview creee.",
                preview.summary,
                f"Trade ID: {preview.trade_id}",
                "Dry-run: oui" if preview.dry_run else "Dry-run: non",
                f"Confirmer: /confirm {preview.trade_id}",
                f"Annuler: /cancel {preview.trade_id}",
            ]
            if preview.warning:
                lines.insert(2, f"Avertissement: {preview.warning}")
            return "\n".join(lines)

        if command == "/confirm":
            trade_id = _require_trade_id(argument, "/confirm")
            result = confirm_trade(trade_id, session, settings)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command == "/cancel":
            trade_id = _require_trade_id(argument, "/cancel")
            result = cancel_trade(trade_id, session)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command == "/entry_filled":
            trade_id = _require_trade_id(argument, "/entry_filled")
            result = mark_entry_filled(trade_id, session)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command == "/submit_targets":
            trade_id = _require_trade_id(argument, "/submit_targets")
            result = submit_ready_targets(trade_id, session, settings)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command == "/status":
            if not argument:
                return "Trading: pause" if is_trading_paused(session) else "Trading: actif"
            trade_id = _require_trade_id(argument, "/status")
            detail = get_trade_detail(trade_id, session)
            if detail is None:
                return "Trade introuvable."
            return format_trade_status(detail.trade, detail.orders)

        if command == "/orders":
            trade_id, filters = _parse_order_filters(argument)
            detail = get_trade_detail(trade_id, session)
            if detail is None:
                return "Trade introuvable."
            orders = _filter_orders(detail.orders, **filters)
            return format_trade_orders(detail.trade, orders)

        if command == "/trades":
            filters = _parse_trades_filters(argument)
            trades = list_trades(session, **filters)
            return format_trade_list(trades)

        if command == "/audit":
            filters = _parse_audit_filters(argument)
            events = list_audit_events(session, **filters)
            return format_audit_events(events)

        if command == "/pause":
            return pause_trading(session)

        if command == "/resume":
            return resume_trading(session)

        if command in {"/start", "/help"}:
            return (
                "Commandes: /trade, /confirm <trade_id>, /entry_filled <trade_id>, "
                "/submit_targets <trade_id>, "
                "/cancel <trade_id>, "
                "/status [trade_id], /orders <trade_id> [status=... role=...], "
                "/trades [limit=5 status=... pair=...], "
                "/audit [trade_id] [event_type=... limit=5], "
                "/pause, /resume.\n"
                "Exemple: /trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
                "t1=67000:40% t2=69000:40% t3=72000:20%\n"
                "Exemple court: LINK LONG 25USDC 2x Entry 9.356 Sl 9.298"
            )
    except (CommandParseError, RiskValidationError, ValueError) as exc:
        return f"Commande refusee: {exc}"

    return "Commande inconnue. Envoie /help."


async def send_telegram_message(chat_id: int | str, text: str, settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise TelegramUpdateError("TELEGRAM_BOT_TOKEN is not configured.")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()


def extract_chat_id(update: dict) -> int | str | None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    chat = message.get("chat") or {}
    return chat.get("id")


def _is_allowed_user(user_id: int | None, settings: Settings) -> bool:
    allowed_ids = settings.telegram_allowed_user_id_set
    return not allowed_ids or user_id in allowed_ids


def _require_trade_id(argument: str, command: str) -> str:
    if not argument:
        raise ValueError(f"{command} requires a trade_id")
    return argument.split()[0]


def _parse_order_filters(argument: str) -> tuple[str, dict]:
    trade_id = _require_trade_id(argument, "/orders")
    filters = {"status": None, "role": None}
    tokens = argument.split()[1:]
    allowed_keys = {"status", "role"}
    for token in tokens:
        if "=" not in token:
            raise ValueError("/orders filters must be key=value")
        key, value = token.split("=", 1)
        key = key.lower()
        if key not in allowed_keys:
            raise ValueError(f"unsupported /orders filter: {key}")
        if not value:
            raise ValueError(f"/orders {key} cannot be empty")
        filters[key] = OrderStatus(value) if key == "status" else OrderRole(value)
    return trade_id, filters


def _filter_orders(
    orders: list[TradeOrder],
    *,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
) -> list[TradeOrder]:
    filtered = orders
    if status is not None:
        filtered = [order for order in filtered if order.status == status]
    if role is not None:
        filtered = [order for order in filtered if order.role == role]
    return filtered


def _parse_trades_filters(argument: str) -> dict:
    filters = {"limit": 5, "offset": 0}
    if not argument:
        return filters

    allowed_keys = {"limit", "offset", "status", "pair"}
    for token in argument.split():
        if "=" not in token:
            raise ValueError("/trades arguments must be key=value")
        key, value = token.split("=", 1)
        key = key.lower()
        if key not in allowed_keys:
            raise ValueError(f"unsupported /trades argument: {key}")
        if not value:
            raise ValueError(f"/trades {key} cannot be empty")
        if key in {"limit", "offset"}:
            filters[key] = int(value)
        elif key == "status":
            filters[key] = TradeStatus(value)
        else:
            filters[key] = value

    if filters["limit"] < 1 or filters["limit"] > 10:
        raise ValueError("/trades limit must be between 1 and 10")
    if filters["offset"] < 0:
        raise ValueError("/trades offset must be >= 0")
    return filters


def _parse_audit_filters(argument: str) -> dict:
    filters = {"limit": 5, "offset": 0, "trade_id": None, "event_type": None}
    if not argument:
        return filters

    tokens = argument.split()
    if tokens and "=" not in tokens[0]:
        filters["trade_id"] = tokens.pop(0)

    allowed_keys = {"limit", "offset", "trade_id", "event_type"}
    for token in tokens:
        if "=" not in token:
            raise ValueError("/audit arguments must be key=value")
        key, value = token.split("=", 1)
        key = key.lower()
        if key not in allowed_keys:
            raise ValueError(f"unsupported /audit argument: {key}")
        if not value:
            raise ValueError(f"/audit {key} cannot be empty")
        if key in {"limit", "offset"}:
            filters[key] = int(value)
        else:
            filters[key] = value

    if filters["limit"] < 1 or filters["limit"] > 10:
        raise ValueError("/audit limit must be between 1 and 10")
    if filters["offset"] < 0:
        raise ValueError("/audit offset must be >= 0")
    return filters
