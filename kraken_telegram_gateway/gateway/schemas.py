from decimal import Decimal
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from kraken_telegram_gateway.gateway.models import AuditEvent, Trade, TradeOrder
from kraken_telegram_gateway.gateway.models import ScalpSession, ScalpSignal, ScalpTrade


class Target(BaseModel):
    price: float = Field(gt=0)
    percent: float = Field(gt=0, le=100)


class TradeIntent(BaseModel):
    pair: str
    side: str
    amount_usdc: float = Field(gt=0)
    entry_type: str
    entry_price: float = Field(gt=0)
    targets: list[Target] = Field(default_factory=list, max_length=3)
    stop_price: float | None = Field(default=None, gt=0)
    leverage: int = Field(default=1, ge=1)

    @field_validator("pair")
    @classmethod
    def normalize_pair(cls, value: str) -> str:
        return value.upper()

    @field_validator("side")
    @classmethod
    def validate_side(cls, value: str) -> str:
        side = value.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return side

    @field_validator("entry_type")
    @classmethod
    def validate_entry_type(cls, value: str) -> str:
        entry_type = value.lower()
        if entry_type != "limit":
            raise ValueError("only limit entries are supported in V1")
        return entry_type

    @model_validator(mode="after")
    def validate_target_percentages(self) -> "TradeIntent":
        if not self.targets:
            return self
        total = sum(target.percent for target in self.targets)
        if abs(total - 100) > 0.0001:
            raise ValueError("target percentages must total 100%")
        return self


class ScalpIntent(BaseModel):
    pair: str
    side_mode: str = "both"
    amount_usdc: float = Field(gt=0)
    leverage: int = Field(default=1, ge=1)
    duration_seconds: int = Field(default=3600, ge=60)
    max_hold_seconds: int = Field(default=300, ge=5)
    max_losses: int = Field(default=3, ge=1)
    min_net_pnl: float = Field(default=5, gt=0)
    mode: str = "paper"

    @field_validator("pair")
    @classmethod
    def normalize_pair(cls, value: str) -> str:
        return value.upper()

    @field_validator("side_mode")
    @classmethod
    def validate_side_mode(cls, value: str) -> str:
        side_mode = value.lower()
        if side_mode not in {"buy", "sell", "both"}:
            raise ValueError("side must be buy, sell, or both")
        return side_mode

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = value.lower()
        if mode not in {"paper", "live"}:
            raise ValueError("scalping mode must be paper or live")
        return mode


class TradePreview(BaseModel):
    trade_id: str
    summary: str
    warning: str | None = None
    dry_run: bool


class ConfirmResult(BaseModel):
    trade_id: str
    status: str
    message: str


class TradeDetail(BaseModel):
    trade: Trade
    orders: list[TradeOrder]


class TradeList(BaseModel):
    items: list[Trade]
    total: int
    limit: int
    offset: int


class AuditEventList(BaseModel):
    items: list[AuditEvent]
    total: int
    limit: int
    offset: int


class AuditEventTypeSummary(BaseModel):
    event_type: str
    count: int
    latest_at: datetime


class AuditEventTypeList(BaseModel):
    items: list[AuditEventTypeSummary]
    total: int


class AccountBalanceResponse(BaseModel):
    account: str
    currency: str
    balance: Decimal | None = None
    equity: Decimal | None = None
    available: Decimal | None = None
    margin: Decimal | None = None


class ScalpStartRequest(BaseModel):
    text: str


class ScalpSessionDetail(BaseModel):
    session: ScalpSession
    trades: list[ScalpTrade]
    signals: list[ScalpSignal]


class ScalpSessionResult(BaseModel):
    session_id: str
    status: str
    message: str


class ScalpSessionReport(BaseModel):
    session_id: str
    pair: str
    status: str
    closed_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    estimated_fees: float
    net_pnl: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    rejected_signals: int
    close_reasons: dict[str, int]
    stop_reason: str | None = None


class ScalpSchedulerResult(BaseModel):
    scanned: int
    processed: int
    skipped: int
    messages: list[str]
