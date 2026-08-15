from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: str = "development"
    database_url: str = "sqlite:///./gateway.db"
    dry_run: bool = True
    live_trading_enabled: bool = False
    max_amount_usdc: float = 100
    max_leverage: int = 2
    allowed_pairs: str = "PF_XBTUSD,PF_ETHUSD"
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_allowed_user_ids: str = ""
    kraken_api_key: str | None = None
    kraken_api_secret: str | None = None

    @property
    def allowed_pair_set(self) -> set[str]:
        return {pair.strip().upper() for pair in self.allowed_pairs.split(",") if pair.strip()}

    @property
    def allows_all_pairs(self) -> bool:
        return self.allowed_pairs.strip() == "*"

    @property
    def telegram_allowed_user_id_set(self) -> set[int]:
        return {
            int(user_id.strip())
            for user_id in self.telegram_allowed_user_ids.split(",")
            if user_id.strip()
        }

    @property
    def can_live_trade(self) -> bool:
        return (
            self.live_trading_enabled
            and not self.dry_run
            and bool(self.kraken_api_key)
            and bool(self.kraken_api_secret)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
