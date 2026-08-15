# Kraken Telegram Trading Gateway

Date: 2026-08-14
Status: MVP SKELETON STARTED

## Objective

Create a Telegram-controlled gateway that receives structured futures trade instructions, validates them, and uses Kraken Derivatives APIs to place and manage orders.

The first production goal is not full autonomy. It is controlled execution with strong validation, dry-run support, clear confirmations, audit logs, and emergency stop controls.

## Initial User Flow

User sends a Telegram message:

```text
/trade pair=PF_XBTUSD side=buy amount_usdc=1000 entry=limit:65000 t1=67000:40% t2=69000:40% t3=72000:20% stop=63000
```

Stop loss is optional in V1. If omitted, the confirmation preview must clearly mark the trade as higher risk.

Gateway flow:

1. Parse the message into a strict trade intent.
2. Validate futures symbol, side, USDC amount, entry type, targets, percentages, optional stop, and leverage cap.
3. Estimate order size and fees.
4. Reply with a confirmation summary.
5. Execute only after explicit confirmation.
6. Place the entry order on Kraken.
7. Track order status.
8. When entry fills, place or manage exit targets.
9. Notify Telegram on each state change.
10. Log every intent, decision, API call result, and error.

## Core Components

- Telegram bot webhook or polling listener.
- Command parser with strict schema validation.
- Risk engine:
  - max order size
  - allowed pairs
  - futures only
  - leverage cap
  - optional stop loss risk warning
  - target percentages must total 100%
- Kraken adapter:
  - derivatives/futures trading client
  - WebSocket or polling-based order status tracking
- Execution engine:
  - dry-run mode
  - live mode
  - idempotency keys per instruction
  - retry policy
  - partial-fill handling
- Persistence:
  - trades
  - orders
  - target exits
  - audit events
  - configuration
- Admin controls:
  - pause trading
  - cancel open strategy
  - cancel all bot-managed orders
  - switch dry-run/live mode

## Safety Rules

- Default mode must be dry-run.
- Live trading requires explicit configuration.
- No API keys in source code or chat logs.
- Kraken API key permissions must be minimal.
- Every live order requires confirmation unless the user later enables trusted auto-execution.
- Futures require a leverage cap.
- Stop loss is optional, but trades without stop loss must be flagged in the confirmation summary.
- The bot must reject malformed or ambiguous commands.
- The bot must never infer missing price, side, amount, or pair.
- Amount is expressed in USDC quote value for V1.
- Idempotency must prevent duplicate execution if Telegram retries a message.

## Kraken Notes

- Kraken derivatives/futures order entry is handled through Derivatives REST.
- Kraken derivatives WebSocket is for streaming market/account updates, not order entry.

## First MVP

Build a private Telegram bot that supports:

- `/trade` dry-run parsing and validation
- `/confirm <trade_id>` execution gate
- `/status`
- `/cancel <trade_id>`
- `/pause`
- `/resume`

MVP storage can start with SQLite.

Recommended stack:

- Python 3.12
- FastAPI for webhook API
- python-telegram-bot or direct Telegram Bot API calls
- Pydantic for strict command schemas
- SQLite with SQLModel or SQLAlchemy
- Kraken official API format implemented in a small adapter layer
- Docker Compose for deployment

## Implementation Started

Created a Python/FastAPI skeleton with:

- strict `/trade` command parser
- Pydantic validation for futures pair, side, USDC amount, limit entry, targets, optional stop, and leverage
- risk guardrails for allowed pairs, max USDC amount, max leverage, and no-stop warning
- SQLite persistence for trades and audit events via SQLModel
- `/commands/trade`, `/commands/confirm/{trade_id}`, `/commands/cancel/{trade_id}`, `/trades/{trade_id}`, and `/health` endpoints
- Kraken adapter stub that only returns dry-run results unless live trading is explicitly enabled and keys are configured
- tests for parser and risk validation
- Telegram webhook endpoint at `/telegram/webhook`
- Telegram command dispatcher for `/trade`, `/confirm <trade_id>`, `/cancel <trade_id>`, `/status <trade_id>`, and `/help`
- Telegram allowlist via `TELEGRAM_ALLOWED_USER_IDS`
- Telegram webhook secret validation via `TELEGRAM_WEBHOOK_SECRET`
- Global trading pause/resume state via `/pause` and `/resume`
- Telegram retry idempotency via persisted `update_id`
- Planned entry and target-exit order rows via `TradeOrder`
- Target exits are modeled as opposite-side, reduce-only limit orders and remain `planned` after entry dry-run confirmation

Verification on 2026-08-14:

- `python3 -m pytest -q` -> 12 passed
- local Uvicorn smoke test succeeded for preview + confirm dry-run
- local `/health` smoke test on port 8010 succeeded

## Open Questions

1. Should target exits be limit reduce-only orders placed immediately after entry fill, or managed dynamically by the bot?
2. Should live mode require a second Telegram confirmation for every trade?
3. What maximum USDC amount per trade should be enforced by default?
4. What default leverage cap should be enforced?
5. Should trades without stop loss require an extra confirmation phrase?

## Next Implementation Steps

1. Add an execution lifecycle for planned target exits after simulated entry fill events.
2. Add Kraken Futures REST signer behind the existing live-trading gate.
3. Add Docker Compose deployment configuration.
