# Kraken Telegram Gateway

MVP local pour parser et valider des intentions de trades Kraken Futures envoyees depuis Telegram.

La V1 est verrouillee en dry-run par defaut. Aucun ordre reel ne part vers Kraken sans configuration explicite de `LIVE_TRADING_ENABLED=false -> true`, `DRY_RUN=true -> false`, et des cles API Kraken Futures valides. Quand ces garde-fous existants sont ouverts, `/confirm` peut soumettre l'ordre d'entree live a Kraken Futures.

## Securite Kraken Futures

Le montant utilisateur reste exprime en USDC. Pour un ordre live Kraken Futures, le systeme doit convertir ce montant en `size` de contrat a partir de metadonnees d'instrument verifiees : valeur USDC par contrat, increment de taille et taille minimale. Si ces metadonnees ne sont pas disponibles, la confirmation est rejetee et aucun payload live n'est signe ni soumis.

Par defaut, un trade sans stop loss reste autorise mais affiche un avertissement. Pour imposer une politique plus stricte, definir `REQUIRE_STOP_LOSS_FOR_CONFIRMATION=true` : la preview reste possible, mais `/confirm` rejette le trade sans toucher aux ordres planifies ni a Kraken.

Par defaut, le bot recupere les metadonnees d'instrument depuis l'endpoint public Kraken Futures `/derivatives/api/v3/instruments` avant de preparer un payload live. Un cache local optionnel peut aussi etre fourni via `KRAKEN_INSTRUMENT_METADATA_PATH`; quand il est defini et contient le symbole demande, il est prioritaire sur l'endpoint public. Format accepte :

```json
{
  "instruments": {
    "PF_XBTUSD": {
      "contract_value_usdc": "5",
      "size_step": "0.5",
      "min_size": "1"
    }
  }
}
```

Ces valeurs doivent etre verifiees avant usage si un cache local est fourni. Avec des metadonnees valides et la gate live ouverte, la V1 soumet l'ordre live signe a Kraken Futures.

Validation locale du cache avant de le monter sur le VPS :

```bash
kraken-metadata-validate ./instruments.json --require PF_XBTUSD --require PF_ETHUSD
```

La commande verifie que le JSON est lisible, que chaque instrument expose `contract_value_usdc`, `size_step` et `min_size`, que ces valeurs sont positives, et que les symboles requis sont presents. Elle ne contacte pas Kraken.

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
/trade LINK LONG 25USDC 2x Entry 9.356 Sl 9.298
/confirm <trade_id>
/entry_filled <trade_id>
/entry-filled <trade_id>
/submit_targets <trade_id>
/submit-targets <trade_id>
/cancel <trade_id>
/status [trade_id]
/orders <trade_id> [status=planned role=target_exit]
/trades [limit=5 status=pending_confirmation pair=PF_XBTUSD side=buy]
/audit [trade_id] [event_type=trade_rejected type=trade_rejected limit=5]
/audit_types
/audit-types
/balance [account=flex currency=USDC asset=USDC]
/solde
/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m max_losses=3 min_pnl=5
/scalp_status <session_id>
/scalp_stop <session_id>
/scalp_report <session_id>
/scalp_tick_kraken [snapshots=2 timeout=10]
/pause
/resume
```

Les targets `t1`/`t2`/`t3` sont optionnelles. Si elles sont presentes, leurs pourcentages doivent totaliser 100%. Si elles sont absentes, le bot planifie seulement l'ordre d'entree et `/submit_targets` n'aura aucune target reduce-only a soumettre.

La syntaxe courte accepte les symboles sans suffixe, par exemple `LINK`, et les convertit par defaut en futures Kraken quote USD/USDC : `PF_LINKUSD`. Le montant reste obligatoire, par exemple `25USDC`, pour eviter qu'un ordre soit cree avec une taille implicite.

`/status <trade_id>` affiche le statut du trade et les ordres attaches : entree, targets reduce-only, prix, montant, statut et identifiant externe dry-run si disponible.
Les previews Telegram gardent le message complet avec les lignes `/confirm <trade_id>` et `/cancel <trade_id>`, puis le webhook envoie un second message contenant uniquement le `trade_id` pour faciliter la copie depuis mobile.
`/entry_filled <trade_id>` ou `/entry-filled <trade_id>` marque l'ordre d'entree comme rempli et passe les targets reduce-only en `ready_to_submit` sans envoyer d'ordre Kraken. La commande est idempotente : la relancer sur un trade deja marque filled ne cree pas de nouvel evenement d'audit.
`/submit_targets <trade_id>` ou `/submit-targets <trade_id>` marque les targets `ready_to_submit` comme soumises en dry-run, avec ids externes locaux, sans envoyer d'ordre Kraken. Si certaines targets seulement sont bloquees, le retour Telegram/API indique le nombre bloque, la premiere erreur, et ajoute un evenement `targets_blocked` en plus de `targets_submitted`. La commande est idempotente apres soumission : un retry indique les targets deja soumises, conserve les ids existants et ne cree pas de nouvel evenement d'audit.
`/cancel <trade_id>` annule un trade et ses ordres encore planifies/prets a soumettre. Si une entree live a deja ete soumise a Kraken, la commande appelle d'abord Kraken Futures `/cancelorder` pour chaque ordre live attache; si Kraken refuse l'annulation, le trade reste dans son statut courant et un evenement `trade_cancel_blocked` est enregistre. La commande est idempotente : un retry sur un trade deja annule ne cree pas de nouvel evenement d'audit.
`/orders <trade_id>` affiche seulement la liste des ordres attaches pour relire rapidement l'entree et les targets, avec filtres optionnels `status` et `role`.
`/trades` affiche les derniers trades depuis Telegram, avec filtres optionnels `limit`, `offset`, `status`, `pair` et `side`.
`/audit` affiche les derniers evenements d'audit, filtrables par `trade_id` et `event_type`, pour diagnostiquer les confirmations rejetees et les garde-fous. Les alias `type=...` et `event=...` sont acceptes pour filtrer plus vite depuis Telegram.
Les filtres Telegram `status`, `role`, `event_type`, `type` et `event` acceptent les majuscules/minuscules pour eviter les rejets inutiles depuis mobile.
`/audit_types` ou `/audit-types` affiche les types d'evenements d'audit disponibles avec leurs compteurs, pratique pour choisir un filtre `event_type`.
`/balance` ou `/solde` interroge le endpoint Kraken Futures `/derivatives/api/v3/accounts` en lecture seule et affiche les soldes par compte/devise. Les lignes sans solde numerique ou avec uniquement des zeros sont masquees, pour garder seulement les comptes utiles comme `flex USDC`. Les filtres optionnels `account` et `currency` permettent de limiter la reponse, par exemple `/balance account=flex currency=USDC`. Les alias `asset=...` et `devise=...` sont aussi acceptes pour filtrer la devise plus vite depuis Telegram. Cette commande exige des cles API Futures (`KRAKEN_API_KEY` et `KRAKEN_API_SECRET`), pas des cles Spot, et reste disponible en dry-run. Si Kraken renvoie `authenticationError`, verifier aussi que `KRAKEN_FUTURES_BASE_URL` correspond au compte des cles (`https://futures.kraken.com` pour live, `https://demo-futures.kraken.com` pour demo).

Un mode scalping experimental est initialise dans [SCALPING_MODE.md](SCALPING_MODE.md). Par defaut, `/scalp_start` cree une session paper durable sans envoyer d'ordre Kraken, accepte des trades qui pourront durer de quelques secondes a quelques minutes via `max_hold`, applique les parametres `duration`, `max_losses` et `min_pnl`, puis `/scalp_status`, `/scalp_stop` et `/scalp_report` permettent de piloter la session. Le rapport affiche winrate, PnL brut/net, frais estimes, drawdown max, signaux rejetes et raisons de cloture. `/scalp_tick_kraken` collecte des snapshots publics Kraken Futures WebSocket et fait avancer les sessions actives depuis Telegram; les options `snapshots=1..10` et `timeout=1..60` sont disponibles. Le meme scheduler est expose par `POST /scalp/scheduler/tick-kraken`. Une boucle background opt-in peut lancer ce tick automatiquement avec `SCALP_KRAKEN_SCHEDULER_ENABLED=true`; elle reste desactivee par defaut.

Scalping live experimental: `mode=live` est accepte uniquement si `SCALP_LIVE_ENABLED=true`, `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`, les cles Kraken Futures sont presentes, la paire est autorisee, et le montant respecte `SCALP_LIVE_MAX_AMOUNT_USDC`. La V1 live soumet seulement un ordre limit d'entree quand un signal passe. Elle ne place pas encore d'ordre de sortie automatique et ne suit pas encore les fills Kraken; surveiller la position directement sur Kraken pendant les tests petit montant.

Variables utiles pour la boucle scalp paper Kraken:

```dotenv
SCALP_LIVE_ENABLED=false
SCALP_LIVE_MAX_AMOUNT_USDC=25
SCALP_KRAKEN_SCHEDULER_ENABLED=false
SCALP_KRAKEN_SCHEDULER_INTERVAL_SECONDS=60
SCALP_KRAKEN_SCHEDULER_SNAPSHOTS_PER_SESSION=1
SCALP_KRAKEN_SCHEDULER_TIMEOUT_SECONDS=10
```

Replay offline d'une session scalp paper depuis des snapshots deterministes JSON, JSONL ou CSV:

```bash
kraken-scalp-replay \
  --command "/scalp_start pair=PF_LINKUSD amount_usdc=100 leverage=2 duration=60m max_hold=5m min_pnl=1" \
  --snapshots ./snapshots-day-1.json ./snapshots-day-2.csv
```

Colonnes/champs attendus: `timestamp`, `bid`, `ask`, `bid_size`, `ask_size`, et `volume_ratio` optionnel. Les alias historiques courants sont aussi acceptes: `time`/`created_at`/`datetime`/`date`, `best_bid`/`bid_price`/`bidPrice`, `best_ask`/`ask_price`/`askPrice`, `bid_qty`/`bidQty`/`bid_volume`/`bidVolume`/`bidsize`, `ask_qty`/`askQty`/`ask_volume`/`askVolume`/`asksize`, et `volumeRatio`/`volume_ratio_1m`. Les exports JSON/JSONL de messages WebSocket publics Kraken Futures `book_snapshot`, `book` et `ticker_lite` sont aussi acceptes; le replay reconstruit alors les meilleurs prix, tailles et ratio de volume local comme le scheduler paper Kraken. Avec un seul fichier, la commande emet le rapport de session comme avant; avec plusieurs fichiers, elle emet `runs` plus une synthese multi-replay: replays, trades fermes/ouverts, wins/losses, winrate, PnL brut/net, frais estimes, pire drawdown par replay, signaux rejetes, raisons de cloture et raisons d'arret. La commande utilise une base SQLite en memoire, ne contacte pas Kraken et n'envoie aucun ordre.

## Exemple

```bash
curl -X POST http://localhost:8000/commands/trade \
  -H "content-type: application/json" \
  -d '{"text":"/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:40% t2=69000:40% t3=72000:20%"}'
```

Puis confirmer en dry-run :

```bash
curl -X POST http://localhost:8000/commands/confirm/<trade_id>
curl -X POST http://localhost:8000/commands/entry-filled/<trade_id>
curl -X POST http://localhost:8000/commands/submit-targets/<trade_id>
```

Consulter les ordres attaches et filtrer une vue operateur :

```bash
curl "http://localhost:8000/trades?limit=20&offset=0"
curl "http://localhost:8000/trades?status=pending_confirmation&pair=PF_XBTUSD&side=buy"
curl http://localhost:8000/trades/<trade_id>/orders
curl "http://localhost:8000/trades/<trade_id>/orders?status=planned"
curl "http://localhost:8000/trades/<trade_id>/orders?status=dry_run_submitted&role=target_exit"
curl "http://localhost:8000/trades/<trade_id>/orders?role=target_exit&status=planned"
curl "http://localhost:8000/audit?trade_id=<trade_id>&event_type=trade_rejected"
curl http://localhost:8000/audit/event-types
curl http://localhost:8000/balance
curl "http://localhost:8000/balance?account=flex&currency=USDC"
curl -X POST http://localhost:8000/commands/scalp-start \
  -H "content-type: application/json" \
  -d '{"text":"/scalp_start pair=PF_LINKUSD amount_usdc=100 duration=60m max_hold=5m max_losses=3 min_pnl=5"}'
curl -X POST http://localhost:8000/commands/scalp-start \
  -H "content-type: application/json" \
  -d '{"text":"/scalp_start pair=PF_LINKUSD amount_usdc=10 mode=live side=buy duration=15m max_hold=2m max_losses=1 min_pnl=1"}'
curl http://localhost:8000/scalp/<session_id>
curl http://localhost:8000/scalp/<session_id>/report
curl -X POST http://localhost:8000/scalp/scheduler/tick
curl -X POST "http://localhost:8000/scalp/scheduler/tick-kraken?snapshots_per_session=2&timeout_seconds=10"
curl -X POST http://localhost:8000/commands/scalp-stop/<session_id>
```

`GET /trades` retourne les trades les plus recents sous forme `{items,total,limit,offset}` avec filtres optionnels `status`, `pair` et `side`.
`GET /audit` retourne les evenements d'audit les plus recents sous forme `{items,total,limit,offset}` avec filtres optionnels `trade_id` et `event_type`.
`GET /audit/event-types` retourne les compteurs par type d'evenement d'audit sous forme `{items,total}` pour aider les vues operateur.
`GET /balance` retourne les soldes Kraken Futures lus via `/derivatives/api/v3/accounts`, avec filtres optionnels `account` et `currency`; il exige les cles Kraken mais reste strictement read-only et disponible en dry-run.
`POST /commands/scalp-start`, `GET /scalp/{session_id}`, `GET /scalp/{session_id}/report`, `POST /scalp/scheduler/tick`, `POST /scalp/scheduler/tick-kraken` et `POST /commands/scalp-stop/{session_id}` exposent le socle scalping paper/live cote API. Le live reste bloque par defaut.
