from decimal import Decimal

import pytest

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import (
    AccountBalance,
    InstrumentMetadata,
    KrakenAccountError,
    KrakenClient,
    KrakenFuturesSigner,
    KrakenLiveTradingDisabledError,
    KrakenOrderPayloadError,
    LocalInstrumentMetadataProvider,
    parse_account_balances,
)
from kraken_telegram_gateway.gateway.models import Trade
from kraken_telegram_gateway.gateway.models import TradeOrder


def make_trade() -> Trade:
    return Trade(
        id="trade-1",
        pair="PF_XBTUSD",
        side="buy",
        amount_usdc=100,
        entry_type="limit",
        entry_price=65000,
        targets_json="[]",
        leverage=2,
    )


def test_futures_signer_matches_derivatives_auth_algorithm():
    signer = KrakenFuturesSigner(
        api_key="public-key",
        api_secret="dGVzdC1zZWNyZXQ=",
        nonce_factory=lambda: "1415957147987",
    )
    post_data = "symbol=PF_XBTUSD&orderType=lmt&side=buy&size=1&limitPrice=65000"

    headers = signer.build_headers(post_data, "/api/v3/sendorder")

    assert headers == {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Accept": "application/json",
        "APIKey": "public-key",
        "Nonce": "1415957147987",
        "Authent": "LV1J80qMEQ6knibZT3MaXYm3nR7UP7GkFR5xPXWsg1KwgVdzBqsu5RekVw43zMKa06Aw37RHwXH65bQKo0SEnQ==",
    }


def test_private_request_preparation_is_blocked_in_dry_run():
    client = KrakenClient(
        Settings(
            dry_run=True,
            live_trading_enabled=False,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
        )
    )

    with pytest.raises(KrakenLiveTradingDisabledError, match="dry-run"):
        client.build_private_request(
            "POST",
            "/api/v3/sendorder",
            {"symbol": "PF_XBTUSD"},
        )


def test_private_request_preparation_signs_only_when_live_gate_is_open():
    client = KrakenClient(
        Settings(
            dry_run=False,
            live_trading_enabled=True,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_futures_base_url="https://example.test/",
        )
    )

    request = client.build_private_request(
        "post",
        "/api/v3/sendorder",
        {
            "symbol": "PF_XBTUSD",
            "orderType": "lmt",
            "side": "buy",
            "size": 1,
            "limitPrice": 65000,
            "reduceOnly": False,
            "optional": None,
        },
    )

    assert request.method == "POST"
    assert request.url == "https://example.test/derivatives/api/v3/sendorder"
    assert request.request_path == "/derivatives/api/v3/sendorder"
    assert request.endpoint_path == "/api/v3/sendorder"
    assert request.post_data == "symbol=PF_XBTUSD&orderType=lmt&side=buy&size=1&limitPrice=65000&reduceOnly=false"
    assert request.headers["APIKey"] == "public-key"
    assert "Nonce" in request.headers
    assert "Authent" in request.headers
    assert "test-secret" not in str(request.headers)


def test_account_request_can_be_signed_in_dry_run_with_credentials():
    client = KrakenClient(
        Settings(
            dry_run=True,
            live_trading_enabled=False,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_futures_base_url="https://example.test",
        )
    )

    request = client.build_account_request()

    assert request.method == "GET"
    assert request.url == "https://example.test/derivatives/api/v3/accounts"
    assert request.request_path == "/derivatives/api/v3/accounts"
    assert request.endpoint_path == "/api/v3/accounts"
    assert request.post_data == ""
    assert request.headers["APIKey"] == "public-key"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded; charset=utf-8"
    assert request.headers["Accept"] == "application/json"
    assert "Authent" in request.headers


def test_account_request_can_be_signed_without_nonce_for_retry():
    client = KrakenClient(
        Settings(
            dry_run=True,
            live_trading_enabled=False,
            kraken_api_key=" public-key ",
            kraken_api_secret=" dGVzdC1zZWNyZXQ= ",
            kraken_futures_base_url="https://example.test",
        )
    )

    request = client.build_account_request(include_nonce=False)

    assert request.headers["APIKey"] == "public-key"
    assert "Nonce" not in request.headers
    assert "Authent" in request.headers


def test_fetch_account_balances_retries_without_nonce_on_authentication_error(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_request(method, url, *, headers, timeout):
        calls.append(headers)
        if len(calls) == 1:
            return FakeResponse({"result": "error", "error": "authenticationError"})
        return FakeResponse(
            {
                "result": "success",
                "accounts": {
                    "flex": {
                        "currencies": {
                            "USDC": {
                                "quantity": "42",
                            }
                        }
                    }
                },
            }
        )

    monkeypatch.setattr("kraken_telegram_gateway.gateway.kraken.httpx.request", fake_request)
    client = KrakenClient(
        Settings(
            dry_run=True,
            live_trading_enabled=False,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_futures_base_url="https://example.test",
        )
    )

    balances = client.fetch_account_balances()

    assert calls[0]["Nonce"]
    assert "Nonce" not in calls[1]
    assert balances == [AccountBalance(account="flex", currency="USDC", balance=Decimal("42"))]


def test_fetch_account_balances_exposes_safe_debug_detail_on_error(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "error", "error": "authenticationError"}

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.httpx.request",
        lambda method, url, *, headers, timeout: FakeResponse(),
    )
    client = KrakenClient(
        Settings(
            dry_run=True,
            live_trading_enabled=False,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_futures_base_url="https://example.test",
        )
    )

    with pytest.raises(KrakenAccountError) as exc_info:
        client.fetch_account_balances()

    debug_detail = exc_info.value.debug_detail
    assert debug_detail is not None
    assert "kraken_error=authenticationError" in debug_detail
    assert "url=https://example.test/derivatives/api/v3/accounts" in debug_detail
    assert "signed_endpoint_path=/api/v3/accounts" in debug_detail
    assert "retried_without_nonce=oui" in debug_detail
    assert "APIKey" not in debug_detail
    assert "Authent" not in debug_detail


def test_parse_account_balances_reads_currency_rows():
    balances = parse_account_balances(
        {
            "result": "success",
            "accounts": {
                "flex": {
                    "currencies": {
                        "USDC": {
                            "quantity": "125.50",
                            "equity": "130",
                            "availableBalance": "120.25",
                            "initialMargin": "9.75",
                        }
                    }
                }
            },
        }
    )

    assert balances == [
        AccountBalance(
            account="flex",
            currency="USDC",
            balance=Decimal("125.50"),
            equity=Decimal("130"),
            available=Decimal("120.25"),
            margin=Decimal("9.75"),
        )
    ]


def test_parse_account_balances_rejects_error_result():
    with pytest.raises(KrakenAccountError, match="not successful"):
        parse_account_balances({"result": "error", "error": "api key invalid"})


def test_dry_run_entry_submission_does_not_require_valid_kraken_secret():
    trade = make_trade()
    client = KrakenClient(Settings(kraken_api_key="public-key", kraken_api_secret="not-base64"))

    result = client.submit_entry_order(trade)

    assert result["mode"] == "dry_run"
    assert result["external_order_id"] == "dryrun-trade-1"


def test_entry_order_payload_refuses_missing_instrument_metadata():
    client = KrakenClient(Settings())

    with pytest.raises(KrakenOrderPayloadError, match="instrument metadata"):
        client.build_entry_order_payload(make_trade())


def test_entry_order_payload_converts_usdc_to_contract_size_with_metadata():
    client = KrakenClient(Settings())
    instrument = InstrumentMetadata(
        symbol="PF_XBTUSD",
        contract_value_usdc=Decimal("5"),
        size_step=Decimal("0.5"),
        min_size=Decimal("1"),
    )

    payload = client.build_entry_order_payload(make_trade(), instrument)

    assert payload == {
        "symbol": "PF_XBTUSD",
        "orderType": "lmt",
        "side": "buy",
        "size": "40",
        "limitPrice": "65000",
        "reduceOnly": False,
    }


def test_target_order_payload_is_reduce_only_and_uses_target_amount():
    trade = make_trade()
    order = TradeOrder(
        id="target-1",
        trade_id=trade.id,
        role="target_exit",
        pair="PF_XBTUSD",
        side="sell",
        price=67000,
        amount_usdc=40,
        target_percent=40,
        reduce_only=True,
    )
    client = KrakenClient(Settings())
    instrument = InstrumentMetadata(
        symbol="PF_XBTUSD",
        contract_value_usdc=Decimal("5"),
        size_step=Decimal("0.5"),
        min_size=Decimal("1"),
    )

    payload = client.build_target_order_payload(trade, order, instrument)

    assert payload == {
        "symbol": "PF_XBTUSD",
        "orderType": "lmt",
        "side": "sell",
        "size": "16",
        "limitPrice": "67000",
        "reduceOnly": True,
    }


def test_dry_run_target_submission_returns_local_external_order_id():
    trade = make_trade()
    order = TradeOrder(
        id="target-1",
        trade_id=trade.id,
        role="target_exit",
        pair="PF_XBTUSD",
        side="sell",
        price=67000,
        amount_usdc=40,
        target_percent=40,
        reduce_only=True,
    )
    client = KrakenClient(Settings(kraken_api_key="public-key", kraken_api_secret="not-base64"))

    result = client.submit_target_order(trade, order)

    assert result["mode"] == "dry_run"
    assert result["external_order_id"] == "dryrun-target-target-1"


def test_local_instrument_metadata_provider_loads_cached_json(tmp_path):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": {
            "PF_XBTUSD": {
              "contract_value_usdc": "5",
              "size_step": "0.5",
              "min_size": "1"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    provider = LocalInstrumentMetadataProvider(str(metadata_file))

    instrument = provider.get("pf_xbtusd")
    metadata_file.write_text("not-json", encoding="utf-8")
    cached_instrument = provider.get("PF_XBTUSD")

    assert instrument == InstrumentMetadata(
        symbol="PF_XBTUSD",
        contract_value_usdc=Decimal("5"),
        size_step=Decimal("0.5"),
        min_size=Decimal("1"),
    )
    assert cached_instrument == instrument


def test_live_entry_submission_is_blocked_until_instrument_metadata_exists():
    client = KrakenClient(
        Settings(
            dry_run=False,
            live_trading_enabled=True,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
        )
    )

    result = client.submit_entry_order(make_trade())

    assert result["mode"] == "blocked"
    assert "instrument metadata" in result["message"]


def test_live_entry_submission_stays_blocked_after_metadata_payload_is_prepared(tmp_path):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": [
            {
              "symbol": "PF_XBTUSD",
              "contract_value_usdc": "5",
              "size_step": "0.5",
              "min_size": "1"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    client = KrakenClient(
        Settings(
            dry_run=False,
            live_trading_enabled=True,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_instrument_metadata_path=str(metadata_file),
        )
    )

    result = client.submit_entry_order(make_trade())

    assert result == {
        "mode": "blocked",
        "message": "Live Kraken submission blocked: network submission is intentionally disabled for V1.",
    }


def test_live_target_submission_stays_blocked_after_metadata_payload_is_prepared(tmp_path):
    trade = make_trade()
    order = TradeOrder(
        id="target-1",
        trade_id=trade.id,
        role="target_exit",
        pair="PF_XBTUSD",
        side="sell",
        price=67000,
        amount_usdc=40,
        target_percent=40,
        reduce_only=True,
    )
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": [
            {
              "symbol": "PF_XBTUSD",
              "contract_value_usdc": "5",
              "size_step": "0.5",
              "min_size": "1"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    client = KrakenClient(
        Settings(
            dry_run=False,
            live_trading_enabled=True,
            kraken_api_key="public-key",
            kraken_api_secret="dGVzdC1zZWNyZXQ=",
            kraken_instrument_metadata_path=str(metadata_file),
        )
    )

    result = client.submit_target_order(trade, order)

    assert result == {
        "mode": "blocked",
        "message": "Live Kraken target submission blocked: network submission is intentionally disabled for V1.",
    }
