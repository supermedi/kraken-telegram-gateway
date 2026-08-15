from pydantic import BaseModel, Field, field_validator, model_validator

from kraken_telegram_gateway.gateway.models import AuditEvent, Trade, TradeOrder


class Target(BaseModel):
    price: float = Field(gt=0)
    percent: float = Field(gt=0, le=100)


class TradeIntent(BaseModel):
    pair: str
    side: str
    amount_usdc: float = Field(gt=0)
    entry_type: str
    entry_price: float = Field(gt=0)
    targets: list[Target] = Field(min_length=1, max_length=3)
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
        total = sum(target.percent for target in self.targets)
        if abs(total - 100) > 0.0001:
            raise ValueError("target percentages must total 100%")
        return self


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
