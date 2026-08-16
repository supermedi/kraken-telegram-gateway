import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

import httpx

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import Trade, TradeOrder


class KrakenLiveTradingDisabledError(RuntimeError):
    """Raised when authenticated Kraken request preparation is blocked by safety settings."""


class KrakenOrderPayloadError(ValueError):
    """Raised when a Kraken order payload cannot be built safely."""


class KrakenAccountError(RuntimeError):
    """Raised when Kraken account data cannot be fetched or parsed."""


@dataclass(frozen=True)
class KrakenAuthenticatedRequest:
    method: str
    url: str
    endpoint_path: str
    post_data: str
    headers: dict[str, str]


@dataclass(frozen=True)
class InstrumentMetadata:
    symbol: str
    contract_value_usdc: Decimal
    size_step: Decimal
    min_size: Decimal


@dataclass(frozen=True)
class AccountBalance:
    account: str
    currency: str
    balance: Decimal | None = None
    equity: Decimal | None = None
    available: Decimal | None = None
    margin: Decimal | None = None


class LocalInstrumentMetadataProvider:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._cache: object | None = None

    def get(self, symbol: str) -> InstrumentMetadata | None:
        if self.path is None:
            return None
        data = self._load()
        raw = find_instrument_metadata_payload(data, symbol)
        if raw is None:
            return None
        return parse_instrument_metadata(raw, symbol)

    def _load(self) -> object:
        if self._cache is None:
            try:
                self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise KrakenOrderPayloadError(f"instrument metadata file cannot be read: {self.path}") from exc
            except json.JSONDecodeError as exc:
                raise KrakenOrderPayloadError(f"instrument metadata file is not valid JSON: {self.path}") from exc
        return self._cache


class KrakenFuturesSigner:
    def __init__(self, api_key: str, api_secret: str, nonce_factory: Callable[[], str] | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.nonce_factory = nonce_factory or self._default_nonce

    def build_headers(self, post_data: str, endpoint_path: str) -> dict[str, str]:
        nonce = self.nonce_factory()
        return {
            "APIKey": self.api_key,
            "Nonce": nonce,
            "Authent": self.sign(post_data, nonce, endpoint_path),
        }

    def sign(self, post_data: str, nonce: str, endpoint_path: str) -> str:
        digest = hashlib.sha256(f"{post_data}{nonce}{endpoint_path}".encode("utf-8")).digest()
        secret = base64.b64decode(self.api_secret)
        signature = hmac.new(secret, digest, hashlib.sha512).digest()
        return base64.b64encode(signature).decode("utf-8")

    @staticmethod
    def _default_nonce() -> str:
        return str(int(time.time() * 1000))


class KrakenClient:
    SEND_ORDER_PATH = "/derivatives/api/v3/sendorder"
    ACCOUNTS_PATH = "/derivatives/api/v3/accounts"

    def __init__(
        self,
        settings: Settings,
        instrument_provider: LocalInstrumentMetadataProvider | None = None,
    ):
        self.settings = settings
        self.instrument_provider = instrument_provider or LocalInstrumentMetadataProvider(
            settings.kraken_instrument_metadata_path
        )

    def submit_entry_order(self, trade: Trade) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "external_order_id": f"dryrun-{trade.id}",
                "message": "Dry-run: no Kraken order was submitted.",
            }

        # The signer/request preparation exists for review and tests, but V1 still blocks
        # network submission until live trading is explicitly approved and implemented.
        try:
            payload = self.build_entry_order_payload(trade, self.instrument_provider.get(trade.pair))
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken submission blocked: {exc}",
            }
        self.build_private_request("POST", self.SEND_ORDER_PATH, payload)
        return {
            "mode": "blocked",
            "message": "Live Kraken submission blocked: network submission is intentionally disabled for V1.",
        }

    def submit_target_order(self, trade: Trade, order: TradeOrder) -> dict[str, str]:
        if not order.reduce_only:
            return {
                "mode": "blocked",
                "message": "Target submission blocked: target exit order must be reduce-only.",
            }
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "external_order_id": f"dryrun-target-{order.id}",
                "message": "Dry-run: no Kraken target order was submitted.",
            }

        # As with entry orders, V1 prepares the boundary for review but never
        # performs a Kraken network submission.
        try:
            payload = self.build_target_order_payload(trade, order, self.instrument_provider.get(order.pair))
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken target submission blocked: {exc}",
            }
        self.build_private_request("POST", self.SEND_ORDER_PATH, payload)
        return {
            "mode": "blocked",
            "message": "Live Kraken target submission blocked: network submission is intentionally disabled for V1.",
        }

    def fetch_account_balances(self) -> list[AccountBalance]:
        request = self.build_account_request()
        try:
            response = httpx.get(request.url, headers=request.headers, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenAccountError(f"Kraken balance request failed: {exc}") from exc
        except ValueError as exc:
            raise KrakenAccountError("Kraken balance response is not valid JSON.") from exc
        return parse_account_balances(payload)

    def build_account_request(self) -> KrakenAuthenticatedRequest:
        return self.build_private_request(
            "GET",
            self.ACCOUNTS_PATH,
            {},
            require_live_trading=False,
        )

    def build_entry_order_payload(
        self,
        trade: Trade,
        instrument: InstrumentMetadata | None = None,
    ) -> dict[str, str | bool]:
        return self._build_limit_order_payload(
            symbol=trade.pair,
            side=trade.side,
            price=trade.entry_price,
            amount_usdc=trade.amount_usdc,
            leverage=trade.leverage,
            reduce_only=False,
            instrument=instrument,
        )

    def build_target_order_payload(
        self,
        trade: Trade,
        order: TradeOrder,
        instrument: InstrumentMetadata | None = None,
    ) -> dict[str, str | bool]:
        if order.trade_id != trade.id:
            raise KrakenOrderPayloadError("target order does not belong to trade")
        if not order.reduce_only:
            raise KrakenOrderPayloadError("target exit order must be reduce-only")
        return self._build_limit_order_payload(
            symbol=order.pair,
            side=order.side,
            price=order.price,
            amount_usdc=order.amount_usdc,
            leverage=trade.leverage,
            reduce_only=True,
            instrument=instrument,
        )

    def _build_limit_order_payload(
        self,
        *,
        symbol: str,
        side: str,
        price: float,
        amount_usdc: float,
        leverage: int,
        reduce_only: bool,
        instrument: InstrumentMetadata | None,
    ) -> dict[str, str | bool]:
        if instrument is None:
            raise KrakenOrderPayloadError(
                "instrument metadata is required before converting amount_usdc to contract size"
            )
        if instrument.symbol.upper() != symbol.upper():
            raise KrakenOrderPayloadError(
                f"instrument metadata mismatch: {instrument.symbol} cannot be used for {symbol}"
            )

        size = calculate_contract_size(
            amount_usdc=Decimal(str(amount_usdc)),
            leverage=Decimal(str(leverage)),
            instrument=instrument,
        )
        return {
            "symbol": symbol,
            "orderType": "lmt",
            "side": side,
            "size": format_decimal(size),
            "limitPrice": format_decimal(Decimal(str(price))),
            "reduceOnly": reduce_only,
        }

    def build_private_request(
        self,
        method: str,
        endpoint_path: str,
        params: Mapping[str, str | int | float | bool | None],
        *,
        require_live_trading: bool = True,
    ) -> KrakenAuthenticatedRequest:
        if require_live_trading and not self.settings.can_live_trade:
            raise KrakenLiveTradingDisabledError("Kraken live request preparation is disabled by dry-run settings.")
        if not self.settings.kraken_api_key or not self.settings.kraken_api_secret:
            raise KrakenLiveTradingDisabledError("Kraken API credentials are required for signed requests.")

        post_data = urlencode({key: encode_param_value(value) for key, value in params.items() if value is not None})
        signer = KrakenFuturesSigner(self.settings.kraken_api_key, self.settings.kraken_api_secret)
        return KrakenAuthenticatedRequest(
            method=method.upper(),
            url=f"{self.settings.kraken_futures_base_url.rstrip('/')}{endpoint_path}",
            endpoint_path=endpoint_path,
            post_data=post_data,
            headers=signer.build_headers(post_data, endpoint_path),
        )


def encode_param_value(value: str | int | float | bool) -> str | int | float:
    if isinstance(value, bool):
        return str(value).lower()
    return value


def calculate_contract_size(amount_usdc: Decimal, leverage: Decimal, instrument: InstrumentMetadata) -> Decimal:
    if amount_usdc <= 0:
        raise KrakenOrderPayloadError("amount_usdc must be positive")
    if leverage <= 0:
        raise KrakenOrderPayloadError("leverage must be positive")
    if instrument.contract_value_usdc <= 0:
        raise KrakenOrderPayloadError("instrument contract_value_usdc must be positive")
    if instrument.size_step <= 0:
        raise KrakenOrderPayloadError("instrument size_step must be positive")
    if instrument.min_size <= 0:
        raise KrakenOrderPayloadError("instrument min_size must be positive")

    raw_size = (amount_usdc * leverage) / instrument.contract_value_usdc
    steps = (raw_size / instrument.size_step).to_integral_value(rounding=ROUND_DOWN)
    size = steps * instrument.size_step
    if size < instrument.min_size:
        raise KrakenOrderPayloadError(
            f"calculated size {format_decimal(size)} is below minimum {format_decimal(instrument.min_size)}"
        )
    return size


def find_instrument_metadata_payload(data: object, symbol: str) -> Mapping[str, object] | None:
    wanted = symbol.upper()
    if isinstance(data, Mapping):
        instruments = data.get("instruments", data)
        if isinstance(instruments, Mapping):
            for key, value in instruments.items():
                if str(key).upper() == wanted and isinstance(value, Mapping):
                    return value
        if isinstance(instruments, list):
            for value in instruments:
                if isinstance(value, Mapping) and str(value.get("symbol", "")).upper() == wanted:
                    return value
    return None


def parse_instrument_metadata(raw: Mapping[str, object], symbol: str) -> InstrumentMetadata:
    try:
        return InstrumentMetadata(
            symbol=str(raw.get("symbol") or symbol).upper(),
            contract_value_usdc=Decimal(str(raw["contract_value_usdc"])),
            size_step=Decimal(str(raw["size_step"])),
            min_size=Decimal(str(raw["min_size"])),
        )
    except KeyError as exc:
        raise KrakenOrderPayloadError(f"instrument metadata is missing required field: {exc.args[0]}") from exc
    except Exception as exc:
        raise KrakenOrderPayloadError("instrument metadata contains invalid decimal values") from exc


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def parse_account_balances(payload: object) -> list[AccountBalance]:
    if not isinstance(payload, Mapping):
        raise KrakenAccountError("Kraken balance response has an unexpected shape.")
    if str(payload.get("result", "success")).lower() != "success":
        raise KrakenAccountError(f"Kraken balance request was not successful: {payload.get('error') or payload}")

    accounts = payload.get("accounts")
    if not isinstance(accounts, Mapping):
        raise KrakenAccountError("Kraken balance response does not contain accounts.")

    balances: list[AccountBalance] = []
    for account_name, raw_account in accounts.items():
        if not isinstance(raw_account, Mapping):
            continue
        currencies = raw_account.get("currencies")
        if isinstance(currencies, Mapping):
            for currency, raw_currency in currencies.items():
                if isinstance(raw_currency, Mapping):
                    balances.append(_parse_balance_row(str(account_name), str(currency), raw_currency))
            continue

        currency = raw_account.get("currency")
        if currency is not None:
            balances.append(_parse_balance_row(str(account_name), str(currency), raw_account))

    if not balances:
        raise KrakenAccountError("Kraken balance response did not include currency balances.")
    return balances


def _parse_balance_row(account: str, currency: str, raw: Mapping[str, object]) -> AccountBalance:
    return AccountBalance(
        account=account,
        currency=currency.upper(),
        balance=_optional_decimal(raw, "balance", "quantity", "walletBalance"),
        equity=_optional_decimal(raw, "equity", "totalEquity"),
        available=_optional_decimal(raw, "available", "availableBalance", "free"),
        margin=_optional_decimal(raw, "margin", "initialMargin", "marginBalance"),
    )


def _optional_decimal(raw: Mapping[str, object], *keys: str) -> Decimal | None:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return Decimal(str(value))
    return None
