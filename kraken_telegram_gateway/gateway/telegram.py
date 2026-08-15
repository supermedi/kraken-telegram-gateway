from __future__ import annotations

import httpx
from sqlmodel import Session

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import ProcessedTelegramUpdate
from kraken_telegram_gateway.gateway.parser import CommandParseError
from kraken_telegram_gateway.gateway.risk import RiskValidationError
from kraken_telegram_gateway.gateway.service import (
    cancel_trade,
    confirm_trade,
    create_trade_preview,
    format_trade_status,
    get_trade_detail,
    is_trading_paused,
    pause_trading,
    resume_trading,
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
        if command == "/trade":
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

        if command == "/status":
            if not argument:
                return "Trading: pause" if is_trading_paused(session) else "Trading: actif"
            trade_id = _require_trade_id(argument, "/status")
            detail = get_trade_detail(trade_id, session)
            if detail is None:
                return "Trade introuvable."
            return format_trade_status(detail.trade, detail.orders)

        if command == "/pause":
            return pause_trading(session)

        if command == "/resume":
            return resume_trading(session)

        if command in {"/start", "/help"}:
            return (
                "Commandes: /trade, /confirm <trade_id>, /cancel <trade_id>, "
                "/status [trade_id], /pause, /resume.\n"
                "Exemple: /trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
                "t1=67000:40% t2=69000:40% t3=72000:20%"
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
