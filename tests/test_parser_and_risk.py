import pytest

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.parser import parse_trade_command
from kraken_telegram_gateway.gateway.risk import RiskValidationError, validate_risk


def test_parse_trade_command_with_optional_stop():
    intent = parse_trade_command(
        "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
        "t1=67000:40% t2=69000:40% t3=72000:20% stop=63000 leverage=2"
    )

    assert intent.pair == "PF_XBTUSD"
    assert intent.amount_usdc == 100
    assert intent.stop_price == 63000
    assert len(intent.targets) == 3


def test_stop_loss_is_optional_but_warns():
    intent = parse_trade_command(
        "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
        "t1=67000:50% t2=69000:50%"
    )

    warning = validate_risk(intent, Settings(max_amount_usdc=100, max_leverage=2))

    assert "Aucun stop loss" in warning


def test_targets_must_total_100_percent():
    with pytest.raises(ValueError, match="target percentages"):
        parse_trade_command(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:50% t2=69000:20%"
        )


def test_amount_cap_is_enforced():
    intent = parse_trade_command(
        "/trade pair=PF_XBTUSD side=buy amount_usdc=101 entry=limit:65000 "
        "t1=67000:100%"
    )

    with pytest.raises(RiskValidationError, match="exceeds max"):
        validate_risk(intent, Settings(max_amount_usdc=100))
