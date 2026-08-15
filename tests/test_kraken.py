from decimal import Decimal

import pytest

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import (
    InstrumentMetadata,
    KrakenClient,
    KrakenFuturesSigner,
    KrakenLiveTradingDisabledError,
    KrakenOrderPayloadError,
)
from kraken_telegram_gateway.gateway.models import Trade


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

    headers = signer.build_headers(post_data, "/derivatives/api/v3/sendorder")

    assert headers == {
        "APIKey": "public-key",
        "Nonce": "1415957147987",
        "Authent": "RAM57StAIJCucaleKwNVMp+oy33wAt0eVE3OcsIs44NLZQVqZvSEpcT2VafIUGJptGLOQcOgDgKpBgzxd2jv6Q==",
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
            "/derivatives/api/v3/sendorder",
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
        "/derivatives/api/v3/sendorder",
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
    assert request.endpoint_path == "/derivatives/api/v3/sendorder"
    assert request.post_data == "symbol=PF_XBTUSD&orderType=lmt&side=buy&size=1&limitPrice=65000&reduceOnly=false"
    assert request.headers["APIKey"] == "public-key"
    assert "Nonce" in request.headers
    assert "Authent" in request.headers
    assert "test-secret" not in str(request.headers)


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
