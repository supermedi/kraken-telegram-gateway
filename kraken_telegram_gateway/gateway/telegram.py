from __future__ import annotations

import html
import json
import re

import httpx
from sqlmodel import Session

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import KrakenAccountError, KrakenAccountEventError, KrakenLiveTradingDisabledError
from kraken_telegram_gateway.gateway.models import OrderRole, OrderStatus, ProcessedTelegramUpdate, TradeOrder, TradeStatus
from kraken_telegram_gateway.gateway.parser import CommandParseError
from kraken_telegram_gateway.gateway.risk import RiskValidationError
# Force update for pipeline
from kraken_telegram_gateway.gateway.service import (
    cancel_trade,
    confirm_trade,
    create_trade_preview,
    format_audit_events,
    format_account_balances,
    format_audit_event_types,
    format_scalp_report,
    format_scalp_status,
    format_trade_list,
    format_trade_orders,
    format_trade_status,
    get_account_balances,
    get_scalp_session_detail,
    get_scalp_audit,
    get_trade_detail,
    is_trading_paused,
    list_audit_events,
    list_audit_event_types,
    list_trades,
    mark_entry_filled,
    pause_trading,
    resume_trading,
    run_active_scalp_paper_sessions_from_kraken,
    start_scalp_session,
    stop_scalp_session,
    sync_scalp_entry_fills,
    submit_ready_targets,
)


class TelegramUpdateError(ValueError):
    pass


def handle_telegram_update(update: dict, session: Session, settings: Settings) -> str | list[str] | None:
    update_id = update.get("update_id")
    if update_id is not None:
        processed = session.get(ProcessedTelegramUpdate, update_id)
        if processed is not None:
            return deserialize_processed_reply(processed.reply_text)

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

    reply = dispatch_telegram_messages(text, session, settings)
    if update_id is not None:
        session.add(
            ProcessedTelegramUpdate(
                update_id=update_id,
                chat_id=str(chat_id),
                reply_text=serialize_processed_reply(reply),
            )
        )
        session.commit()
    return reply


def dispatch_telegram_messages(text: str, session: Session, settings: Settings) -> list[str]:
    reply = dispatch_telegram_text(text, session, settings)
    command = text.partition(" ")[0].split("@", 1)[0].lower()
    if command == "/trade" or not command.startswith("/"):
        trade_id = extract_trade_id_from_reply(reply)
        if trade_id:
            return [reply, trade_id]
    return [reply]


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
                format_trade_action_commands(preview.trade_id),
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
            result = cancel_trade(trade_id, session, settings)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command in {"/entry_filled", "/entry-filled"}:
            trade_id = _require_trade_id(argument, "/entry_filled")
            result = mark_entry_filled(trade_id, session)
            return f"{result.message}\nTrade ID: {result.trade_id}\nStatut: {result.status}"

        if command in {"/submit_targets", "/submit-targets"}:
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

        if command in {"/audit_types", "/audit-types"}:
            if argument:
                raise ValueError("/audit_types does not accept arguments")
            return format_audit_event_types(list_audit_event_types(session))

        if command in {"/balance", "/solde"}:
            filters = _parse_balance_filters(argument)
            balances = get_account_balances(settings, **filters)
            return format_account_balances(balances)

        if command == "/scalp_start":
            result = start_scalp_session(text, session, settings)
            if result.session_id:
                detail = get_scalp_session_detail(result.session_id, session)
                if detail is not None:
                    return f"{result.message}\n{format_scalp_status(detail)}"
            return f"{result.message}\nStatut: {result.status}"

        if command == "/scalp_audit":
            session_id = _require_scalp_session_id(argument, "/scalp_audit")
            return get_scalp_audit(session_id, session)

        if command == "/scalp_status":
            session_id = _require_scalp_session_id(argument, "/scalp_status")
            detail = get_scalp_session_detail(session_id, session)
            if detail is None:
                return "Session scalp introuvable."
            return format_scalp_status(detail)

        if command == "/scalp_report":
            session_id = _require_scalp_session_id(argument, "/scalp_report")
            detail = get_scalp_session_detail(session_id, session)
            if detail is None:
                return "Session scalp introuvable."
            return format_scalp_report(detail)

        if command == "/scalp_stop":
            session_id = _require_scalp_session_id(argument, "/scalp_stop")
            return stop_scalp_session(session_id, session).message

        if command in {"/scalp_sync_fills", "/scalp-sync-fills"}:
            session_id = _require_scalp_session_id(argument, "/scalp_sync_fills")
            return format_scalp_fill_sync_result(sync_scalp_entry_fills(session_id, session, settings))

        if command in {"/scalp_tick_kraken", "/scalp-tick-kraken"}:
            options = _parse_scalp_tick_kraken_options(argument)
            result = run_active_scalp_paper_sessions_from_kraken(session, **options, settings=settings)
            return format_scalp_scheduler_result(result)

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
                "/trades [limit=5 status=... pair=... side=buy|sell], "
                "/audit [trade_id] [event_type=...|type=... limit=5], /audit_types, "
                "/balance [account=... currency=...|asset=...], "
                "/scalp_start pair=PF_LINKUSD amount_usdc=100 duration=60m max_hold=5m max_losses=3, "
                "/scalp_status <session_id>, /scalp_stop <session_id>, /scalp_report <session_id>, "
                "/scalp_sync_fills <session_id>, "
                "/scalp_tick_kraken [snapshots=1 timeout=10], "
                "/pause, /resume.\n"
                "Exemple: /trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
                "t1=67000:40% t2=69000:40% t3=72000:20%\n"
                "Exemple court: LINK LONG 25USDC 2x Entry 9.356 Sl 9.298"
            )
    except (
        CommandParseError,
        RiskValidationError,
        KrakenAccountError,
        KrakenAccountEventError,
        KrakenLiveTradingDisabledError,
        ValueError,
    ) as exc:
        if (
            isinstance(exc, KrakenAccountError)
            and settings.kraken_balance_debug_errors
            and exc.debug_detail
        ):
            return f"Commande refusee: {exc}\nDebug Kraken balance:\n{exc.debug_detail}"
        return f"Commande refusee: {exc}"

    return "Commande inconnue. Envoie /help."


def format_trade_action_commands(trade_id: str) -> str:
    return f"```bash\n/confirm {trade_id}\n```\n\n```bash\n/cancel {trade_id}\n```"


def format_scalp_scheduler_result(result) -> str:
    lines = [
        "Tick Kraken scalp termine.",
        f"Sessions scannees: {result.scanned}",
        f"Traitees: {result.processed}",
        f"Ignorees: {result.skipped}",
    ]
    if result.messages:
        lines.append("Messages:")
        lines.extend(f"- {message}" for message in result.messages)
    return "\n".join(lines)


def format_scalp_fill_sync_result(result) -> str:
    return "\n".join(
        [
            result.message,
            f"Session ID: {result.session_id}",
            f"Statut: {result.status}",
            f"Scannes: {result.scanned} | Filled: {result.filled} | Ignores: {result.skipped}",
        ]
    )


def extract_trade_id_from_reply(reply: str) -> str | None:
    for line in reply.splitlines():
        if line.startswith("Trade ID:"):
            return line.split(":", 1)[1].strip()
    return None


def serialize_processed_reply(reply: list[str]) -> str:
    return json.dumps(reply)


def deserialize_processed_reply(reply_text: str | None) -> str | list[str] | None:
    if reply_text is None:
        return None
    try:
        decoded = json.loads(reply_text)
    except json.JSONDecodeError:
        return reply_text
    if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
        return decoded
    return reply_text


def render_telegram_html(text: str) -> str:
    parts = []
    cursor = 0
    for match in re.finditer(r"```([A-Za-z0-9_-]+)?\n(.*?)```", text, flags=re.DOTALL):
        parts.append(html.escape(text[cursor : match.start()]))
        language = match.group(1)
        code = html.escape(match.group(2).rstrip())
        if language:
            escaped_language = html.escape(language)
            parts.append(f'<pre><code class="language-{escaped_language}">{code}</code></pre>')
        else:
            parts.append(f"<pre>{code}</pre>")
        cursor = match.end()
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


async def send_telegram_message(chat_id: int | str, text: str, settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise TelegramUpdateError("TELEGRAM_BOT_TOKEN is not configured.")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            url,
            json={"chat_id": chat_id, "text": render_telegram_html(text), "parse_mode": "HTML"},
        )
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


def _require_scalp_session_id(argument: str, command: str) -> str:
    if not argument:
        raise ValueError(f"{command} requires a session_id")
    return argument.split()[0]


def _parse_scalp_tick_kraken_options(argument: str) -> dict[str, int | float]:
    options: dict[str, int | float] = {"snapshots_per_session": 1, "timeout_seconds": 10}
    allowed_keys = {"snapshots", "snapshots_per_session", "timeout", "timeout_seconds"}
    for token in argument.split():
        if "=" not in token:
            raise ValueError("/scalp_tick_kraken options must be key=value")
        key, value = token.split("=", 1)
        key = key.lower()
        if key not in allowed_keys:
            raise ValueError(f"unsupported /scalp_tick_kraken option: {key}")
        if not value:
            raise ValueError(f"/scalp_tick_kraken {key} cannot be empty")
        if key in {"snapshots", "snapshots_per_session"}:
            snapshots = int(value)
            if snapshots < 1 or snapshots > 10:
                raise ValueError("/scalp_tick_kraken snapshots must be between 1 and 10")
            options["snapshots_per_session"] = snapshots
        else:
            timeout = float(value)
            if timeout < 1 or timeout > 60:
                raise ValueError("/scalp_tick_kraken timeout must be between 1 and 60 seconds")
            options["timeout_seconds"] = timeout
    return options


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
        filters[key] = OrderStatus(value.lower()) if key == "status" else OrderRole(value.lower())
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

    allowed_keys = {"limit", "offset", "status", "pair", "side"}
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
            filters[key] = TradeStatus(value.lower())
        elif key == "side":
            normalized_side = value.lower()
            if normalized_side not in {"buy", "sell"}:
                raise ValueError("/trades side must be buy or sell")
            filters[key] = normalized_side
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

    allowed_keys = {"limit", "offset", "trade_id", "event_type", "type", "event"}
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
        elif key in {"type", "event"}:
            filters["event_type"] = value.lower()
        elif key == "event_type":
            filters[key] = value.lower()
        else:
            filters[key] = value

    if filters["limit"] < 1 or filters["limit"] > 10:
        raise ValueError("/audit limit must be between 1 and 10")
    if filters["offset"] < 0:
        raise ValueError("/audit offset must be >= 0")
    return filters


def _parse_balance_filters(argument: str) -> dict:
    filters = {"account": None, "currency": None}
    if not argument:
        return filters

    allowed_keys = {"account", "currency", "asset", "devise"}
    for token in argument.split():
        if "=" not in token:
            raise ValueError("/balance arguments must be key=value")
        key, value = token.split("=", 1)
        key = key.lower()
        if key not in allowed_keys:
            raise ValueError(f"unsupported /balance argument: {key}")
        if not value:
            raise ValueError(f"/balance {key} cannot be empty")
        if key in {"asset", "devise"}:
            filters["currency"] = value
        else:
            filters[key] = value
    return filters
