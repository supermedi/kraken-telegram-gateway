import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import ScalpSession, Trade, TradeOrder, ScalpTrade


class KrakenLiveTradingDisabledError(RuntimeError):
    """Raised when authenticated Kraken request preparation is blocked by safety settings."""


class KrakenOrderPayloadError(ValueError):
    """Raised when a Kraken order payload cannot be built safely."""


class KrakenAccountError(RuntimeError):
    """Raised when Kraken account data cannot be fetched or parsed."""

    def __init__(self, message: str, *, debug_detail: str | None = None):
        super().__init__(message)
        self.debug_detail = debug_detail


class KrakenOrderSubmissionError(RuntimeError):
    """Raised when Kraken rejects or fails an order submission."""


class KrakenOrderCancellationError(RuntimeError):
    """Raised when Kraken rejects or fails an order cancellation."""


class KrakenAccountEventError(RuntimeError):
    """Raised when Kraken account events cannot be fetched or parsed."""


@dataclass(frozen=True)
class KrakenAuthenticatedRequest:
    method: str
    url: str
    request_path: str
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


@dataclass(frozen=True)
class KrakenFill:
    order_id: str
    symbol: str
    side: str
    price: Decimal
    size: Decimal | None = None
    fill_id: str | None = None
    fill_time: str | None = None


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


class PublicKrakenInstrumentMetadataProvider:
    INSTRUMENTS_PATH = "/api/v3/instruments"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._cache: object | None = None

    def get(self, symbol: str) -> InstrumentMetadata | None:
        raw = find_instrument_metadata_payload(self._load(), symbol)
        if raw is None:
            return None
        return parse_public_instrument_metadata(raw, symbol)

    def _load(self) -> object:
        if self._cache is None:
            url = f"{self.base_url}{KrakenClient.API_PREFIX}{self.INSTRUMENTS_PATH}"
            try:
                response = httpx.get(url, timeout=10)
                response.raise_for_status()
                self._cache = response.json()
            except httpx.HTTPError as exc:
                raise KrakenOrderPayloadError(f"Kraken instrument metadata request failed: {exc}") from exc
            except ValueError as exc:
                raise KrakenOrderPayloadError("Kraken instrument metadata response is not valid JSON.") from exc
        return self._cache


class InstrumentMetadataProvider:
    def __init__(self, settings: Settings):
        self.local_provider = LocalInstrumentMetadataProvider(settings.kraken_instrument_metadata_path)
        self.public_provider = PublicKrakenInstrumentMetadataProvider(settings.kraken_futures_base_url)

    def get(self, symbol: str) -> InstrumentMetadata | None:
        local = self.local_provider.get(symbol)
        if local is not None:
            return local
        return self.public_provider.get(symbol)


class KrakenFuturesSigner:
    def __init__(self, api_key: str, api_secret: str, nonce_factory: Callable[[], str] | None = None):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.nonce_factory = nonce_factory or self._default_nonce

    def build_headers(self, post_data: str, endpoint_path: str, *, include_nonce: bool = True) -> dict[str, str]:
        nonce = self.nonce_factory() if include_nonce else ""
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "application/json",
            "APIKey": self.api_key,
            "Authent": self.sign(post_data, nonce, endpoint_path),
        }
        if include_nonce:
            headers["Nonce"] = nonce
        return headers

    def sign(self, post_data: str, nonce: str, endpoint_path: str) -> str:
        digest = hashlib.sha256(f"{post_data}{nonce}{endpoint_path}".encode("utf-8")).digest()
        secret = base64.b64decode(self.api_secret)
        signature = hmac.new(secret, digest, hashlib.sha512).digest()
        return base64.b64encode(signature).decode("utf-8")

    @staticmethod
    def _default_nonce() -> str:
        return str(int(time.time() * 1000))


class KrakenClient:
    API_PREFIX = "/derivatives"
    SEND_ORDER_PATH = "/api/v3/sendorder"
    CANCEL_ORDER_PATH = "/api/v3/cancelorder"
    ACCOUNTS_PATH = "/api/v3/accounts"
    FILLS_PATH = "/api/v3/fills"

    def __init__(
        self,
        settings: Settings,
        instrument_provider: InstrumentMetadataProvider | LocalInstrumentMetadataProvider | None = None,
    ):
        self.settings = settings
        self.instrument_provider = instrument_provider or InstrumentMetadataProvider(settings)

    def submit_entry_order(self, trade: Trade) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "external_order_id": f"dryrun-{trade.id}",
                "message": "Dry-run: no Kraken order was submitted.",
            }

        try:
            payload = self.build_entry_order_payload(trade, self.instrument_provider.get(trade.pair))
            external_order_id = self.submit_live_order(payload)
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken submission blocked: {exc}",
            }
        except KrakenOrderSubmissionError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken submission failed: {exc}",
            }
        return {
            "mode": "live",
            "external_order_id": external_order_id,
            "message": "Live Kraken entry order submitted.",
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

        try:
            payload = self.build_target_order_payload(trade, order, self.instrument_provider.get(order.pair))
            external_order_id = self.submit_live_order(payload)
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken target submission blocked: {exc}",
            }
        except KrakenOrderSubmissionError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken target submission failed: {exc}",
            }
        return {
            "mode": "live",
            "external_order_id": external_order_id,
            "message": "Live Kraken target order submitted.",
        }

    def submit_scalp_exit_order(self, scalp_session: ScalpSession, trade: ScalpTrade, price: float) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "blocked",
                "message": "Live scalp exit blocked: Kraken live gates are not open.",
            }
        
        # Le side d'exit est l'inverse du side d'entrée
        exit_side = "sell" if trade.side == "buy" else "buy"
        
        try:
            payload = self._build_limit_order_payload(
                symbol=scalp_session.pair,
                side=exit_side,
                price=price,
                amount_usdc=scalp_session.amount_usdc,
                leverage=scalp_session.leverage,
                reduce_only=True,
                instrument=self.instrument_provider.get(scalp_session.pair),
            )
            external_order_id = self.submit_live_order(payload)
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken scalp exit submission blocked: {exc}",
            }
        except KrakenOrderSubmissionError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken scalp exit submission failed: {exc}",
            }
        return {
            "mode": "live",
            "external_order_id": external_order_id,
            "message": f"Live Kraken scalp exit order submitted at {price}.",
        }

    def submit_scalp_entry_order(self, scalp_session: ScalpSession, side: str, price: float) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "blocked",
                "message": "Live scalp entry blocked: Kraken live gates are not open.",
            }
        
        try:
            payload = self._build_limit_order_payload(
                symbol=scalp_session.pair,
                side=side,
                price=price,
                amount_usdc=scalp_session.amount_usdc,
                leverage=scalp_session.leverage,
                reduce_only=False,
                instrument=self.instrument_provider.get(scalp_session.pair),
            )
            external_order_id = self.submit_live_order(payload)
        except KrakenOrderPayloadError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken scalp entry submission blocked: {exc}",
            }
        except KrakenOrderSubmissionError as exc:
            return {
                "mode": "blocked",
                "message": f"Live Kraken scalp entry submission failed: {exc}",
            }
        return {
            "mode": "live",
            "external_order_id": external_order_id,
            "message": f"Live Kraken scalp entry order submitted at {price}.",
        }

    def submit_live_order(self, payload: Mapping[str, str | int | float | bool | None]) -> str:
        request = self.build_private_request("POST", self.SEND_ORDER_PATH, payload)
        try:
            response = httpx.request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.post_data,
                timeout=10,
            )
            response.raise_for_status()
            response_payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenOrderSubmissionError(f"request failed: {exc}") from exc
        except ValueError as exc:
            raise KrakenOrderSubmissionError("response is not valid JSON") from exc

        if not isinstance(response_payload, Mapping):
            raise KrakenOrderSubmissionError("response payload is not an object")
        if str(response_payload.get("result", "success")).lower() != "success":
            raise KrakenOrderSubmissionError(str(response_payload.get("error") or response_payload))
        return extract_placed_order_id(response_payload)

    def fetch_recent_fills(self) -> list[KrakenFill]:
        request = self.build_fills_request()
        try:
            response = httpx.request(request.method, request.url, headers=request.headers, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenAccountEventError(f"Kraken fills request failed: {exc}") from exc
        except ValueError as exc:
            raise KrakenAccountEventError("Kraken fills response is not valid JSON.") from exc
        
        return parse_kraken_fills(payload)

    def cancel_order(self, order_id: str) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "message": "Dry-run: no Kraken order was cancelled.",
            }
        
        request = self.build_private_request("POST", self.CANCEL_ORDER_PATH, {"order_id": order_id})
        try:
            response = httpx.request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.post_data,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenOrderCancellationError(f"request failed: {exc}") from exc
        
        if str(payload.get("result", "success")).lower() != "success":
            raise KrakenOrderCancellationError(str(payload.get("error") or payload))
            
        validate_cancel_status(payload)
        return {
            "mode": "live",
            "message": "Live Kraken order cancelled.",
        }
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "message": "Dry-run: no Kraken order was cancelled.",
            }
        
        request = self.build_private_request("POST", self.CANCEL_ORDER_PATH, {"orderId": order_id})
        try:
            response = httpx.request(
                request.method,
                request.url,
                headers=request.headers,
                content=request.post_data,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenOrderCancellationError(f"request failed: {exc}") from exc
        
        if str(payload.get("result", "success")).lower() != "success":
            raise KrakenOrderCancellationError(str(payload.get("error") or payload))
            
        validate_cancel_status(payload)
        return {
            "mode": "live",
            "message": "Live Kraken order cancelled.",
        }

    def fetch_account_balances(self) -> list[AccountBalance]:
        request = self.build_account_request()
        retried_without_nonce = False
        try:
            response = httpx.request(request.method, request.url, headers=request.headers, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenAccountError(f"Kraken balance request failed: {exc}") from exc
        except ValueError as exc:
            raise KrakenAccountError("Kraken balance response is not valid JSON.") from exc

        if is_kraken_authentication_error(payload) and "Nonce" in request.headers:
            request = self.build_account_request(include_nonce=False)
            retried_without_nonce = True
            try:
                response = httpx.request(request.method, request.url, headers=request.headers, timeout=10)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise KrakenAccountError(f"Kraken balance request failed: {exc}") from exc
            except ValueError as exc:
                raise KrakenAccountError("Kraken balance response is not valid JSON.") from exc

        if str(payload.get("result", "success")).lower() != "success" if isinstance(payload, Mapping) else False:
            raise KrakenAccountError(
                f"Kraken balance request was not successful: {payload.get('error') or payload}",
                debug_detail=format_account_error_debug_detail(
                    request,
                    payload,
                    retried_without_nonce=retried_without_nonce,
                    base_url=self.settings.kraken_futures_base_url,
                ),
            )
        return parse_account_balances(payload)

    def fetch_ohlcv(self, symbol: str, interval: str, count: int = 100) -> list[dict[str, Any]]:
        # Mapping des intervalles numériques vers les formats de l'API charts (ex: 60 -> 1h, 30 -> 30m, 1 -> 1m)
        interval_map = {"60": "1h", "30": "30m", "15": "15m", "5": "5m", "1": "1m"}
        formatted_interval = interval_map.get(interval, interval)
        
        url = f"{self.settings.kraken_futures_base_url.rstrip('/')}/api/charts/v1/trade/{symbol}/{formatted_interval}"
        try:
            response = httpx.get(url, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise KrakenAccountError(f"Kraken OHLCV request failed: {exc}") from exc
        except ValueError as exc:
            raise KrakenAccountError("Kraken OHLCV response is not valid JSON.") from exc

        return payload if isinstance(payload, list) else payload.get("candles", [])

    def build_account_request(self, *, include_nonce: bool = True) -> KrakenAuthenticatedRequest:
        return self.build_private_request(
            "GET",
            self.ACCOUNTS_PATH,
            {},
            require_live_trading=False,
            include_nonce=include_nonce,
        )

    def build_fills_request(self, *, include_nonce: bool = True) -> KrakenAuthenticatedRequest:
        return self.build_private_request(
            "GET",
            self.FILLS_PATH,
            {},
            require_live_trading=False,
            include_nonce=include_nonce,
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
            price=Decimal(str(price)),
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
        include_nonce: bool = True,
    ) -> KrakenAuthenticatedRequest:
        if require_live_trading and not self.settings.can_live_trade:
            raise KrakenLiveTradingDisabledError("Kraken live request preparation is disabled by dry-run settings.")
        if not self.settings.kraken_api_key or not self.settings.kraken_api_secret:
            raise KrakenLiveTradingDisabledError("Kraken API credentials are required for signed requests.")

        post_data = urlencode({key: encode_param_value(value) for key, value in params.items() if value is not None})
        signer = KrakenFuturesSigner(self.settings.kraken_api_key, self.settings.kraken_api_secret)
        return KrakenAuthenticatedRequest(
            method=method.upper(),
            url=f"{self.settings.kraken_futures_base_url.rstrip('/')}{self.API_PREFIX}{endpoint_path}",
            request_path=f"{self.API_PREFIX}{endpoint_path}",
            endpoint_path=endpoint_path,
            post_data=post_data,
            headers=signer.build_headers(post_data, endpoint_path, include_nonce=include_nonce),
        )


def encode_param_value(value: str | int | float | bool) -> str | int | float:
    if isinstance(value, bool):
        return str(value).lower()
    return value


def is_kraken_authentication_error(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if isinstance(error, str):
        return error == "authenticationError"
    if isinstance(error, list):
        return "authenticationError" in error
    return False


def format_account_error_debug_detail(
    request: KrakenAuthenticatedRequest,
    payload: Mapping[str, object],
    *,
    retried_without_nonce: bool,
    base_url: str,
) -> str:
    error = payload.get("error")
    if isinstance(error, list):
        error_text = ", ".join(str(item) for item in error)
    else:
        error_text = str(error)
    return "\n".join(
        [
            f"kraken_result={payload.get('result')}",
            f"kraken_error={error_text}",
            f"method={request.method}",
            f"url={request.url}",
            f"signed_endpoint_path={request.endpoint_path}",
            f"request_path={request.request_path}",
            f"base_url={base_url.rstrip('/')}",
            f"nonce_sent={'oui' if 'Nonce' in request.headers else 'non'}",
            f"retried_without_nonce={'oui' if retried_without_nonce else 'non'}",
            f"response_keys={','.join(str(key) for key in payload.keys())}",
        ]
    )


def extract_placed_order_id(payload: Mapping[str, object]) -> str:
    send_status = payload.get("sendStatus")
    if isinstance(send_status, Mapping):
        status = send_status.get("status")
        if str(status).lower() != "placed":
            raise KrakenOrderSubmissionError(format_send_order_rejection(send_status))
        order_id = extract_order_id(send_status)
        if order_id:
            return order_id

    order_id = extract_order_id(payload)
    if order_id:
        return order_id

    server_time = payload.get("serverTime")
    if server_time:
        return f"kraken-live-{server_time}"
    return "kraken-live-submitted"


def extract_order_id(payload: Mapping[str, object]) -> str | None:
    for key in ("order_id", "orderId", "order_id_2", "cliOrdId"):
        value = payload.get(key)
        if value:
            return str(value)

    order_events = payload.get("orderEvents")
    if isinstance(order_events, list):
        for event in order_events:
            if isinstance(event, Mapping):
                order = event.get("order")
                if isinstance(order, Mapping):
                    value = order.get("orderId")
                    if value:
                        return str(value)
    return None


def format_send_order_rejection(send_status: Mapping[str, object]) -> str:
    status = send_status.get("status") or "unknown"
    reason = send_status.get("error") or send_status.get("reason") or send_status.get("message")
    details = [f"sendStatus.status={status}"]
    if reason:
        details.append(f"reason={reason}")
    order_events = send_status.get("orderEvents")
    if isinstance(order_events, list):
        event_types = [
            str(event.get("type"))
            for event in order_events
            if isinstance(event, Mapping) and event.get("type")
        ]
        if event_types:
            details.append(f"orderEvents={','.join(event_types)}")
    return "; ".join(details)


def validate_cancel_status(payload: Mapping[str, object]) -> None:
    cancel_status = payload.get("cancelStatus")
    if not isinstance(cancel_status, Mapping):
        return
    status = str(cancel_status.get("status") or "").lower()
    if status == "cancelled":
        return
    details = [f"cancelStatus.status={status or 'unknown'}"]
    reason = cancel_status.get("error") or cancel_status.get("reason") or cancel_status.get("message")
    if reason:
        details.append(f"reason={reason}")
    raise KrakenOrderCancellationError("; ".join(details))


def calculate_contract_size(
    amount_usdc: Decimal,
    leverage: Decimal,
    price: Decimal,
    instrument: InstrumentMetadata,
) -> Decimal:
    if amount_usdc <= 0:
        raise KrakenOrderPayloadError("amount_usdc must be positive")
    if leverage <= 0:
        raise KrakenOrderPayloadError("leverage must be positive")
    if price <= 0:
        raise KrakenOrderPayloadError("price must be positive")
    if instrument.contract_value_usdc <= 0:
        raise KrakenOrderPayloadError("instrument contract_value_usdc must be positive")
    if instrument.size_step <= 0:
        raise KrakenOrderPayloadError("instrument size_step must be positive")
    if instrument.min_size <= 0:
        raise KrakenOrderPayloadError("instrument min_size must be positive")

    raw_size = (amount_usdc * leverage) / (price * instrument.contract_value_usdc)
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


def parse_public_instrument_metadata(raw: Mapping[str, object], symbol: str) -> InstrumentMetadata:
    try:
        precision = int(str(raw["contractValueTradePrecision"]))
        size_step = Decimal("1").scaleb(-precision)
        return InstrumentMetadata(
            symbol=str(raw.get("symbol") or symbol).upper(),
            contract_value_usdc=Decimal(str(raw["contractSize"])),
            size_step=size_step,
            min_size=size_step,
        )
    except KeyError as exc:
        raise KrakenOrderPayloadError(f"Kraken instrument metadata is missing required field: {exc.args[0]}") from exc
    except Exception as exc:
        raise KrakenOrderPayloadError("Kraken instrument metadata contains invalid decimal values") from exc


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


def parse_kraken_fills(payload: object) -> list[KrakenFill]:
    if not isinstance(payload, Mapping):
        raise KrakenAccountEventError("Kraken fills response has an unexpected shape.")
    if str(payload.get("result", "success")).lower() != "success":
        raise KrakenAccountEventError(f"Kraken fills request was not successful: {payload.get('error') or payload}")

    raw_fills = payload.get("fills")
    if not isinstance(raw_fills, list):
        raise KrakenAccountEventError("Kraken fills response does not contain fills.")

    fills: list[KrakenFill] = []
    for raw_fill in raw_fills:
        if not isinstance(raw_fill, Mapping):
            continue
        order_id = raw_fill.get("order_id") or raw_fill.get("orderId") or raw_fill.get("cliOrdId")
        price = raw_fill.get("price")
        if order_id is None or price is None:
            continue
        size = raw_fill.get("size") or raw_fill.get("filledSize")
        try:
            fills.append(
                KrakenFill(
                    order_id=str(order_id),
                    symbol=str(raw_fill.get("symbol") or "").upper(),
                    side=str(raw_fill.get("side") or "").lower(),
                    price=Decimal(str(price)),
                    size=Decimal(str(size)) if size is not None else None,
                    fill_id=str(raw_fill["fill_id"]) if raw_fill.get("fill_id") is not None else None,
                    fill_time=str(raw_fill["fillTime"]) if raw_fill.get("fillTime") is not None else None,
                )
            )
        except Exception as exc:
            raise KrakenAccountEventError("Kraken fills response contains invalid decimal values.") from exc
    return fills


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
