# Kraken Telegram Gateway

MVP local pour parser et valider des intentions de trades Kraken Futures envoyees depuis Telegram.

La V1 est verrouillee en dry-run par defaut. Aucun ordre reel ne part vers Kraken sans configuration explicite de `LIVE_TRADING_ENABLED=false -> true`, `DRY_RUN=true -> false`, et des cles API.

## Installation

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## Lancer l'API

```bash
uvicorn kraken_telegram_gateway.gateway.app:app --reload --host 0.0.0.0 --port 8000
```

## Docker

L'image Docker est prevue pour etre publique : elle contient le code, mais jamais le fichier `.env`, la base SQLite, ni les secrets Telegram/Kraken.

Build local :

```bash
docker build -t kraken-telegram-gateway:local .
```

Lancement VPS avec variables separees :

```bash
cp .env.example .env
nano .env
docker compose up -d
```

Publication publique recommandee : GitHub Container Registry (`ghcr.io`). Voir [DEPLOYMENT.md](DEPLOYMENT.md).

## Connecter Telegram

1. Creer un bot avec BotFather et renseigner `TELEGRAM_BOT_TOKEN` dans `.env`.
2. Mettre ton identifiant Telegram dans `TELEGRAM_ALLOWED_USER_IDS`.
3. Definir un secret long dans `TELEGRAM_WEBHOOK_SECRET`.
4. Exposer l'API en HTTPS, puis enregistrer le webhook :

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://ton-domaine.example/telegram/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Commandes Telegram supportees :

```text
/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:40% t2=69000:40% t3=72000:20%
/confirm <trade_id>
/cancel <trade_id>
/status [trade_id]
/pause
/resume
```

`/status <trade_id>` affiche le statut du trade et les ordres attaches : entree, targets reduce-only, prix, montant, statut et identifiant externe dry-run si disponible.

## Exemple

```bash
curl -X POST http://localhost:8000/commands/trade \
  -H "content-type: application/json" \
  -d '{"text":"/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:40% t2=69000:40% t3=72000:20%"}'
```

Puis confirmer en dry-run :

```bash
curl -X POST http://localhost:8000/commands/confirm/<trade_id>
```
