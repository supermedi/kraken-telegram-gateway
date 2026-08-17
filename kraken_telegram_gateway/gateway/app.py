import asyncio
import logging
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from kraken_telegram_gateway.gateway.config import Settings, get_settings
from kraken_telegram_gateway.gateway.db import engine, get_session, init_db
from kraken_telegram_gateway.gateway.kraken import AccountBalance, KrakenAccountError, KrakenLiveTradingDisabledError
from kraken_telegram_gateway.gateway.parser import CommandParseError
from kraken_telegram_gateway.gateway.risk import RiskValidationError
from kraken_telegram_gateway.gateway.models import OrderRole, OrderStatus, TradeOrder, TradeStatus
from kraken_telegram_gateway.gateway.schemas import (
    AccountBalanceResponse,
    AuditEventList,
    AuditEventTypeList,
    ConfirmResult,
    ScalpSchedulerResult,
    ScalpSessionDetail,
    ScalpSessionResult,
    ScalpStartRequest,
    TradeDetail,
    TradeList,
    TradePreview,
)
from kraken_telegram_gateway.gateway.service import (
    cancel_trade,
    confirm_trade,
    create_trade_preview,
    get_account_balances,
    get_scalp_session_detail,
    get_trade_detail,
    get_trade_orders,
    list_audit_event_types,
    list_audit_events,
    list_trades,
    mark_entry_filled,
    run_active_scalp_paper_sessions_from_kraken,
    run_active_scalp_paper_sessions,
    start_scalp_session,
    stop_scalp_session,
    submit_ready_targets,
)
from kraken_telegram_gateway.gateway.telegram import (
    TelegramUpdateError,
    extract_chat_id,
    handle_telegram_update,
    send_telegram_message,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings = get_settings()
    scalp_scheduler_task = None
    if settings.scalp_kraken_scheduler_enabled:
        scalp_scheduler_task = asyncio.create_task(run_scalp_kraken_scheduler_loop(settings))
    try:
        yield
    finally:
        if scalp_scheduler_task is not None:
            scalp_scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scalp_scheduler_task


async def run_scalp_kraken_scheduler_loop(settings: Settings, *, max_ticks: int | None = None) -> None:
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        await asyncio.sleep(settings.scalp_kraken_scheduler_interval_seconds)
        try:
            await asyncio.to_thread(_run_scalp_kraken_scheduler_once, settings)
        except Exception:
            logger.exception("Scalp Kraken paper scheduler tick failed.")
        ticks += 1


def _run_scalp_kraken_scheduler_once(settings: Settings) -> ScalpSchedulerResult:
    with Session(engine) as session:
        return run_active_scalp_paper_sessions_from_kraken(
            session,
            snapshots_per_session=settings.scalp_kraken_scheduler_snapshots_per_session,
            timeout_seconds=settings.scalp_kraken_scheduler_timeout_seconds,
        )


app = FastAPI(title="Kraken Telegram Gateway", version="0.1.0", lifespan=lifespan)


class CommandRequest(BaseModel):
    text: str


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, bool | str]:
    return {"ok": True, "dry_run": settings.dry_run, "live_ready": settings.can_live_trade}


@app.get("/balance", response_model=list[AccountBalanceResponse])
def get_balance(
    account: str | None = None,
    currency: str | None = None,
    settings: Settings = Depends(get_settings),
) -> list[AccountBalance]:
    try:
        return get_account_balances(settings, account=account, currency=currency)
    except (KrakenAccountError, KrakenLiveTradingDisabledError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/commands/scalp-start", response_model=ScalpSessionResult)
def scalp_start_command(
    request: ScalpStartRequest,
    session: Session = Depends(get_session),
) -> ScalpSessionResult:
    try:
        return start_scalp_session(request.text, session)
    except (CommandParseError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/commands/scalp-stop/{session_id}", response_model=ScalpSessionResult)
def scalp_stop_command(session_id: str, session: Session = Depends(get_session)) -> ScalpSessionResult:
    return stop_scalp_session(session_id, session)


@app.get("/scalp/{session_id}", response_model=ScalpSessionDetail)
def get_scalp_session(session_id: str, session: Session = Depends(get_session)) -> ScalpSessionDetail:
    detail = get_scalp_session_detail(session_id, session)
    if detail is None:
        raise HTTPException(status_code=404, detail="Scalp session not found")
    return detail


@app.post("/scalp/scheduler/tick", response_model=ScalpSchedulerResult)
def scalp_scheduler_tick(session: Session = Depends(get_session)) -> ScalpSchedulerResult:
    return run_active_scalp_paper_sessions(session, lambda _: [])


@app.post("/scalp/scheduler/tick-kraken", response_model=ScalpSchedulerResult)
def scalp_scheduler_kraken_tick(
    snapshots_per_session: int = Query(default=1, ge=1, le=10),
    timeout_seconds: float = Query(default=10, ge=1, le=60),
    session: Session = Depends(get_session),
) -> ScalpSchedulerResult:
    return run_active_scalp_paper_sessions_from_kraken(
        session,
        snapshots_per_session=snapshots_per_session,
        timeout_seconds=timeout_seconds,
    )


@app.post("/commands/trade", response_model=TradePreview)
def trade_command(
    request: CommandRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TradePreview:
    try:
        return create_trade_preview(request.text, session, settings)
    except (CommandParseError, RiskValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/commands/confirm/{trade_id}", response_model=ConfirmResult)
def confirm_command(
    trade_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ConfirmResult:
    return confirm_trade(trade_id, session, settings)


@app.post("/commands/cancel/{trade_id}", response_model=ConfirmResult)
def cancel_command(
    trade_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ConfirmResult:
    return cancel_trade(trade_id, session, settings)


@app.post("/commands/entry-filled/{trade_id}", response_model=ConfirmResult)
def entry_filled_command(trade_id: str, session: Session = Depends(get_session)) -> ConfirmResult:
    return mark_entry_filled(trade_id, session)


@app.post("/commands/submit-targets/{trade_id}", response_model=ConfirmResult)
def submit_targets_command(
    trade_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ConfirmResult:
    return submit_ready_targets(trade_id, session, settings)


@app.get("/trades", response_model=TradeList)
def get_trade_list(
    status: TradeStatus | None = None,
    pair: str | None = None,
    side: str | None = Query(default=None, pattern="^(?i:buy|sell)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> TradeList:
    return list_trades(session, status=status, pair=pair, side=side, limit=limit, offset=offset)


@app.get("/audit", response_model=AuditEventList)
def get_audit_event_list(
    trade_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> AuditEventList:
    return list_audit_events(
        session,
        trade_id=trade_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )


@app.get("/audit/event-types", response_model=AuditEventTypeList)
def get_audit_event_type_list(session: Session = Depends(get_session)) -> AuditEventTypeList:
    return list_audit_event_types(session)


@app.get("/trades/{trade_id}", response_model=TradeDetail)
def get_trade(trade_id: str, session: Session = Depends(get_session)) -> TradeDetail:
    detail = get_trade_detail(trade_id, session)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return detail


@app.get("/trades/{trade_id}/orders", response_model=list[TradeOrder])
def get_trade_order_list(
    trade_id: str,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
    session: Session = Depends(get_session),
) -> list[TradeOrder]:
    orders = get_trade_orders(trade_id, session, status=status, role=role)
    if orders is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return orders


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    if (
        settings.telegram_webhook_secret
        and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret.")

    update = await request.json()
    try:
        reply = handle_telegram_update(update, session, settings)
        chat_id = extract_chat_id(update)
        if reply and chat_id is not None:
            replies = reply if isinstance(reply, list) else [reply]
            for message in replies:
                await send_telegram_message(chat_id, message, settings)
    except TelegramUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True}
