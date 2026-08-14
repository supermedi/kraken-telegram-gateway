from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.models import Trade


class KrakenClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def submit_entry_order(self, trade: Trade) -> dict[str, str]:
        if not self.settings.can_live_trade:
            return {
                "mode": "dry_run",
                "external_order_id": f"dryrun-{trade.id}",
                "message": "Dry-run: no Kraken order was submitted.",
            }

        raise NotImplementedError("Live Kraken Futures submission is intentionally gated for V1.")
