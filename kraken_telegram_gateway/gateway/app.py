from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from kraken_telegram_gateway.gateway.config import Settings, get_settings
from kraken_telegram_gateway.gateway.db import get_session, init_db
from kraken_telegram_gateway.gateway.parser import CommandParseError
from kraken_telegram_gateway.gateway.risk import RiskValidationError
from kraken_telegram_gateway.gateway.schemas import ConfirmResult, TradeDetail, TradePreview
from kraken_telegram_gateway.gateway.service import cancel_trade, confirm_trade, create_trade_preview, get_trade_detail
from kraken_telegram_gateway.gateway.telegram import (
    TelegramUpdateError,
    extract_chat_id,
    handle_telegram_update,
    send_telegram_message,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Kraken Telegram Gateway", version="0.1.0", lifespan=lifespan)


class CommandRequest(BaseModel):
    text: str


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, bool | str]:
    return {"ok": True, "dry_run": settings.dry_run, "live_ready": settings.can_live_trade}


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
def cancel_command(trade_id: str, session: Session = Depends(get_session)) -> ConfirmResult:
    return cancel_trade(trade_id, session)


@app.get("/trades/{trade_id}", response_model=TradeDetail)
def get_trade(trade_id: str, session: Session = Depends(get_session)) -> TradeDetail:
    detail = get_trade_detail(trade_id, session)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return detail


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
            await send_telegram_message(chat_id, reply, settings)
    except TelegramUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True}
