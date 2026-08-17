# Deployment

This project is designed so the Docker image can be public while secrets stay only on the VPS.

## What Goes In The Image

- Application code.
- Python dependencies.
- No `.env`.
- No SQLite database.
- No Telegram token.
- No Kraken API key or secret.

Secrets and runtime settings are injected by Docker Compose with `env_file: .env`.

## Publish The Public Image

Recommended registry: GitHub Container Registry (`ghcr.io`).

1. Push this repository to GitHub.
2. Keep the repository public if you want the image to be publicly discoverable.
3. The workflow `.github/workflows/publish-docker.yml` publishes:
   - `ghcr.io/supermedi/kraken-telegram-gateway:main`
   - `ghcr.io/supermedi/kraken-telegram-gateway:latest`
   - `ghcr.io/supermedi/kraken-telegram-gateway:sha-<commit>`
   - `ghcr.io/supermedi/kraken-telegram-gateway:vX.Y.Z` when a version tag is pushed
4. In GitHub, set the package visibility to public if GHCR creates it as private.

Manual publish from your machine or VPS:

```bash
docker build -t ghcr.io/supermedi/kraken-telegram-gateway:latest .
docker login ghcr.io
docker push ghcr.io/supermedi/kraken-telegram-gateway:latest
```

## Configure The VPS

Create a local `.env` on the Hostinger VPS. Do not commit it.

```bash
cp .env.example .env
nano .env
```

Safe dry-run example:

```dotenv
APP_ENV=production
DATABASE_URL=sqlite:////data/gateway.db
DRY_RUN=true
LIVE_TRADING_ENABLED=false
MAX_AMOUNT_USDC=100
MAX_LEVERAGE=2
ALLOWED_PAIRS=PF_XBTUSD,PF_ETHUSD
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_WEBHOOK_SECRET=your_long_random_secret
TELEGRAM_ALLOWED_USER_IDS=1544791425
KRAKEN_API_KEY=
KRAKEN_API_SECRET=
SCALP_KRAKEN_SCHEDULER_ENABLED=false
SCALP_KRAKEN_SCHEDULER_INTERVAL_SECONDS=60
SCALP_KRAKEN_SCHEDULER_SNAPSHOTS_PER_SESSION=1
SCALP_KRAKEN_SCHEDULER_TIMEOUT_SECONDS=10
HOST_PORT=8010
IMAGE_NAME=ghcr.io/supermedi/kraken-telegram-gateway:latest
```

`SCALP_KRAKEN_SCHEDULER_ENABLED=true` lance uniquement la boucle automatique de scalping paper depuis les snapshots publics Kraken Futures. Garder `DRY_RUN=true` et `LIVE_TRADING_ENABLED=false`; cette boucle ne doit pas etre utilisee comme validation de trading live.

Start:

```bash
docker compose pull
docker compose up -d
docker compose logs -f gateway
```

Health check:

```bash
curl http://127.0.0.1:8010/health
```

## Telegram Webhook

After the app is reachable through HTTPS, register the webhook:

```bash
source .env
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://your-domain.example/telegram/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

## Update The VPS

When a new image is published:

```bash
docker compose pull
docker compose up -d
docker image prune -f
```

Keep live trading disabled until the live Kraken path has been explicitly reviewed and approved.
