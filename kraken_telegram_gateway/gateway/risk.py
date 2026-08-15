from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.schemas import TradeIntent


class RiskValidationError(ValueError):
    pass


def validate_risk(intent: TradeIntent, settings: Settings) -> str | None:
    if not settings.allows_all_pairs and intent.pair not in settings.allowed_pair_set:
        raise RiskValidationError(f"pair {intent.pair} is not allowed")
    if intent.amount_usdc > settings.max_amount_usdc:
        raise RiskValidationError(
            f"amount_usdc {intent.amount_usdc:g} exceeds max {settings.max_amount_usdc:g}"
        )
    if intent.leverage > settings.max_leverage:
        raise RiskValidationError(f"leverage {intent.leverage} exceeds max {settings.max_leverage}")
    if intent.stop_price is None:
        return "Aucun stop loss defini, risque de perte non borne."
    return None
