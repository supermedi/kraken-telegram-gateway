import base64
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import Trade


class KrakenLiveTradingDisabledError(RuntimeError):
    """Raised when authenticated Kraken request preparation is blocked by safety settings."""


class KrakenOrderPayloadError(ValueError):
    """Raised when a Kraken order payload cannot be built safely."""


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

    def __init__(self, settings: Settings):
        self.settings = settings

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
            payload = self.build_entry_order_payload(trade)
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken submission blocked: {exc}",
            }
        self.build_private_request("POST", self.SEND_ORDER_PATH, payload)
        raise NotImplementedError("Live Kraken Futures submission is intentionally gated for V1.")

    def build_entry_order_payload(
        self,
        trade: Trade,
        instrument: InstrumentMetadata | None = None,
    ) -> dict[str, str | bool]:
        if instrument is None:
            raise KrakenOrderPayloadError(
                "instrument metadata is required before converting amount_usdc to contract size"
            )
        if instrument.symbol.upper() != trade.pair.upper():
            raise KrakenOrderPayloadError(
                f"instrument metadata mismatch: {instrument.symbol} cannot be used for {trade.pair}"
            )

        size = calculate_contract_size(
            amount_usdc=Decimal(str(trade.amount_usdc)),
            leverage=Decimal(str(trade.leverage)),
            instrument=instrument,
        )
        return {
            "symbol": trade.pair,
            "orderType": "lmt",
            "side": trade.side,
            "size": format_decimal(size),
            "limitPrice": format_decimal(Decimal(str(trade.entry_price))),
            "reduceOnly": False,
        }

    def build_private_request(
        self,
        method: str,
        endpoint_path: str,
        params: Mapping[str, str | int | float | bool | None],
    ) -> KrakenAuthenticatedRequest:
        if not self.settings.can_live_trade:
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


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")
