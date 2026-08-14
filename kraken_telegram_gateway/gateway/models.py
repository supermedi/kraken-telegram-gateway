from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlmodel import Field, SQLModel


class TradeStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    DRY_RUN_EXECUTED = "dry_run_executed"
    LIVE_SUBMITTED = "live_submitted"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderRole(StrEnum):
    ENTRY = "entry"
    TARGET_EXIT = "target_exit"


class OrderStatus(StrEnum):
    PLANNED = "planned"
    DRY_RUN_SUBMITTED = "dry_run_submitted"
    LIVE_SUBMITTED = "live_submitted"
    CANCELLED = "cancelled"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Trade(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    pair: str
    side: str
    amount_usdc: float
    entry_type: str
    entry_price: float
    targets_json: str
    stop_price: float | None = None
    leverage: int
    status: TradeStatus = TradeStatus.PENDING_CONFIRMATION
    dry_run: bool = True
    warning: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEvent(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    trade_id: str | None = Field(default=None, index=True)
    event_type: str
    message: str
    created_at: datetime = Field(default_factory=utc_now)


class TradeOrder(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    trade_id: str = Field(index=True)
    role: OrderRole
    pair: str
    side: str
    order_type: str = "limit"
    price: float
    amount_usdc: float
    target_percent: float | None = None
    reduce_only: bool = False
    status: OrderStatus = OrderStatus.PLANNED
    external_order_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BotState(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=utc_now)


class ProcessedTelegramUpdate(SQLModel, table=True):
    update_id: int = Field(primary_key=True)
    chat_id: str | None = None
    reply_text: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
