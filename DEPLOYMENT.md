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
   - `ghcr.io/<owner>/<repo>:main`
   - `ghcr.io/<owner>/<repo>:latest`
   - `ghcr.io/<owner>/<repo>:sha-<commit>`
   - `ghcr.io/<owner>/<repo>:vX.Y.Z` when a version tag is pushed
4. In GitHub, set the package visibility to public if GHCR creates it as private.

Manual publish from your machine or VPS:

```bash
docker build -t ghcr.io/<owner>/<repo>:latest .
docker login ghcr.io
docker push ghcr.io/<owner>/<repo>:latest
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
HOST_PORT=8010
IMAGE_NAME=ghcr.io/<owner>/<repo>:latest
```

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
