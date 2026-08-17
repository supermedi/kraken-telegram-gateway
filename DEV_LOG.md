# Kraken Telegram Gateway - Dev Log

Durable project tracker for autonomous development cycles.

The hourly isolated cron must use this file as the handoff point between runs:

1. Read this file before choosing work.
2. Pick one small, useful, non-destructive task from `Next Queue`.
3. Keep Kraken live trading disabled unless the user explicitly approves it.
4. Update `Current State`, `Cycle Log`, and `Next Queue` before reporting.
5. If the cycle makes a validated project evolution, create a local Git commit before reporting.
6. Include test results, commit hash/status, and important file changes in the Telegram report.

## Current State

- Project: Kraken Futures <-> Telegram trading gateway.
- Runtime: Python/FastAPI with SQLite persistence.
- Safety mode: dry-run only by default; live Kraken execution is approved only when the existing runtime gates are deliberately opened with `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`, and valid Kraken Futures credentials.
- Telegram: webhook endpoint, command dispatcher, user allowlist, webhook secret, pause/resume, retry idempotency, trade previews with copyable `bash` action blocks, `/balance`/`/solde` read-only Kraken Futures account balance lookup with `account`/`currency` plus `asset`/`devise` currency aliases, idempotent `/cancel <trade_id>` with live Kraken order cancellation when an attached live order exists, `/status <trade_id>` trade visibility, `/orders <trade_id>` compact order visibility with case-insensitive `status`/`role` filters, `/trades` recent-list visibility with `limit`/`offset`/case-insensitive `status`/`pair`/`side` filters, `/audit` safety-event diagnostics with case-insensitive `event_type`/`type`/`event` filters, `/audit_types` and `/audit-types` event-type counters, idempotent `/entry_filled <trade_id>`/`/entry-filled <trade_id>` local lifecycle tracking, and idempotent `/submit_targets <trade_id>`/`/submit-targets <trade_id>` dry-run target submission are implemented.
- Risk policy: stop loss is optional by default with a warning; `REQUIRE_STOP_LOSS_FOR_CONFIRMATION=true` rejects confirmation of no-stop trades without touching planned orders or Kraken.
- Trading model: trade previews are persisted with planned entry orders and reduce-only target exit orders; repeated cancellation retries are no-ops that avoid duplicate audit events; confirmed entries can be marked `filled`, which moves target exits to `ready_to_submit`; ready targets can then be marked `dry_run_submitted` with local external ids and no Kraken network submission; repeated target submission retries are no-ops that preserve existing ids and avoid duplicate audit events; `/trades` lists recent trades with `limit`, `offset`, `status`, `pair`, and `side` filters; `/trades/{trade_id}` returns the trade plus attached orders; `/trades/{trade_id}/orders` returns attached orders with optional `status` and `role` filters; `/audit` lists recent audit events with `trade_id` and `event_type` filters; `/audit/event-types` returns local audit event-type counters.
- Kraken Futures: authenticated REST signing, private request preparation, read-only `/derivatives/api/v3/accounts` balance lookup exposed through Telegram `/balance`/`/solde` and API `GET /balance` with optional filters, a local-first/public-fallback instrument metadata provider, a metadata cache validator CLI, safe entry/target limit-order payload boundaries, live order POST submission to `/derivatives/api/v3/sendorder`, and live order cancellation via `/derivatives/api/v3/cancelorder` are implemented behind the existing live gates.
- Deployment: Dockerfile, Docker Compose, GHCR publish workflow, final public image name `ghcr.io/supermedi/kraken-telegram-gateway:latest`, and deployment documentation are in place; runtime secrets stay in local `.env`.
- GitHub: dedicated public repository created and initial code pushed to `https://github.com/supermedi/kraken-telegram-gateway`.
- Verification baseline: `python3 -m pytest -q` was last reported passing with 93 tests on 2026-08-16.

## Guardrails

- Do not place real Kraken orders unless the user explicitly asks for live mode and the runtime gates are already configured for it.
- Do not flip `DRY_RUN=false` or `LIVE_TRADING_ENABLED=true` without explicit user approval.
- Do not log, print, or commit secrets.
- Keep changes small and testable.
- Preserve user changes and avoid destructive git/file operations.
- Prefer explicit confirmation gates for any future live-trading path.
- After every validated evolution, commit only project files with the local author identity `supermedi <108479582+supermedi@users.noreply.github.com>`.
- Never commit OpenClaw private workspace files such as `AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `TOOLS.md`, `HEARTBEAT.md`, `BOOT*.md`, `memory/`, or `media/`.
- Do not push from cron unless GitHub credentials are already available and safe for non-interactive use; otherwise report the local commit hash and that push is pending.

## Next Queue

1. Validate Docker build in the target VPS environment, then confirm GHCR pull/run health against `ghcr.io/supermedi/kraken-telegram-gateway:latest`.
2. Validate Docker/GHCR deployment on the VPS with the public Kraken instrument metadata fallback enabled.
3. Add Kraken account-event polling/webhook abstraction for real entry fill detection only after live integration is explicitly approved.
4. Harden target-submission retry diagnostics further only if mixed blocked/submitted live-integration states need clearer operator feedback.
5. Extend audit/balance/Telegram diagnostics only if operators need retention/export, balance freshness, richer webhook failure visibility, or additional mobile command ergonomics.

## Cycle Log

### 2026-08-17 01:42 UTC - Cancel Live Kraken Orders

- Added signed Kraken Futures `/cancelorder` support using the stored live `external_order_id`.
- Changed `/cancel <trade_id>` so attached live orders are cancelled on Kraken before the local trade is marked `cancelled`.
- If Kraken rejects cancellation, the trade remains in its current status and a `trade_cancel_blocked` audit event records the failure.
- Updated Telegram/API cancel routing and documented the live cancellation behavior.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `kraken_telegram_gateway/gateway/app.py`, `tests/test_kraken.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 104 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-17 00:33 UTC - Respect Kraken Send Status Rejections

- Fixed live order submission handling so Kraken responses with `result=success` but `sendStatus.status` other than `placed` are treated as rejected/blocked.
- Added rejection detail from `sendStatus.status`, `reason`/`error`/`message`, and `orderEvents` into the Telegram/API failure message.
- Updated live submission tests to require documented `sendStatus.status=placed` for successful order placement.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `tests/test_kraken.py`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_kraken.py -q` -> 21 passed. `python3 -m pytest -q` -> 98 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-17 00:28 UTC - Separate Telegram Action Blocks

- Split the Telegram preview copy helper into two separate `bash` blocks: one for `/confirm <trade_id>` and one for `/cancel <trade_id>`.
- Kept the existing plain confirm/cancel hint lines for readability and compatibility.
- Updated README documentation for the mobile copy behavior.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_trade_message_creates_preview_and_confirm_hint tests/test_telegram.py::test_render_telegram_html_preserves_code_block_without_markdown_underscores -q` -> 2 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 97 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 23:24 UTC - Copyable Trade Actions

- Added a `bash` code block to Telegram trade previews with ready-to-copy `/confirm <trade_id>` and `/cancel <trade_id>` commands.
- Kept the existing plain confirm/cancel hint lines for readability and compatibility.
- Documented the mobile copy helper in `README.md`.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_trade_message_creates_preview_and_confirm_hint -q` -> 1 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 94 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 23:18 UTC - Public Instrument Metadata Fallback

- Added a public Kraken Futures instrument metadata provider that reads `/derivatives/api/v3/instruments` when no local `KRAKEN_INSTRUMENT_METADATA_PATH` entry exists for the symbol.
- Mapped Kraken public fields `contractSize` and `contractValueTradePrecision` into the internal contract sizing metadata used before live request preparation.
- Kept the local metadata cache as the first priority when configured, and preserved the V1 network-submission block after payload preparation.
- Documented that the cache is now optional and only overrides the public metadata fallback.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `tests/test_kraken.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_kraken.py -q` -> 19 passed. `python3 -m pytest tests/test_metadata.py -q` -> 4 passed.

### 2026-08-16 23:05 UTC - Hide Empty Balance Rows

- Filtered Kraken Futures account balances before API/Telegram formatting so empty instrument rows with no numeric balance, or only zero values, are hidden.
- Kept non-zero rows such as `flex USDC | balance=96.8 | available=96.8` visible.
- Documented that `/balance` and `/solde` hide empty/zero-only rows to keep the mobile output readable.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_balance_command_hides_empty_instrument_accounts tests/test_telegram.py::test_balance_command_formats_kraken_futures_balances -q` -> 2 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 93 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 22:44 UTC - Telegram Filter Case Normalization

- Chose a small mobile ergonomics task from Next Queue item 5 because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Made Telegram `/orders` `status`/`role`, `/trades` `status`, and `/audit` `event_type`/`type`/`event` filter values case-insensitive, so mobile operators can send values like `CANCELLED`, `ENTRY`, or `TRADE_REJECTED` without command rejection or empty results.
- Documented the case-insensitive Telegram filter behavior in `README.md`.
- Kept Kraken safety guardrails unchanged: no live-trading flag, dry-run default, Kraken order path, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_orders_command_accepts_case_insensitive_filters tests/test_telegram.py::test_trades_command_filters_status_pair_and_side tests/test_telegram.py::test_audit_command_accepts_short_event_type_filter_aliases -q` -> 3 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 92 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 21:44 UTC - Trade Side Filters

- Chose a small operator visibility task from Next Queue item 5 because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Added `side=buy|sell` filtering to API `GET /trades` and Telegram `/trades`, including case-insensitive Telegram input and explicit rejection of invalid side values.
- Updated `README.md` and `/help` examples so operators can quickly list only buy-side or sell-side planned trades from mobile.
- Kept Kraken safety guardrails unchanged: no live-trading flag, dry-run default, Kraken order path, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_trades_command_filters_status_pair_and_side tests/test_telegram.py::test_trades_command_rejects_invalid_side -q` -> 2 passed. `python3 -m pytest tests/test_api.py::test_trade_list_api_returns_recent_trades_with_filters_and_pagination -q` -> 1 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 91 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 21:00 UTC - Telegram Balance Currency Aliases

- Chose a small read-only Telegram ergonomics task from Next Queue item 5 because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Added `/balance asset=...` and `/solde devise=...` as aliases for the existing `currency=...` filter, keeping the same Kraken Futures read-only balance lookup path.
- Updated `/help` and `README.md` so mobile balance examples include the alias behavior.
- Kept Kraken safety guardrails unchanged: no live-trading flag, dry-run default, Kraken order path, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_balance_command_filters_account_and_currency tests/test_telegram.py::test_balance_command_accepts_currency_aliases tests/test_telegram.py::test_balance_command_rejects_invalid_filter -q` -> 3 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 88 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 19:44 UTC - Telegram Audit Filter Aliases

- Chose a small Telegram diagnostics task from Next Queue item 5 because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Added `/audit type=...` and `/audit event=...` as mobile-friendly aliases for the existing `/audit event_type=...` filter.
- Documented the aliases in `README.md` and surfaced `type=...` in `/help`.
- Kept Kraken safety guardrails unchanged: no live-trading flag, dry-run default, Kraken order path, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_audit_command_accepts_short_event_type_filter_aliases -q` -> 1 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 87 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 18:44 UTC - Telegram Hyphen Command Aliases

- Chose a small Telegram robustness task because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Added Telegram hyphen aliases for multiword commands: `/entry-filled`, `/submit-targets`, and `/audit-types`, while preserving the existing underscore commands.
- Documented the aliases in `README.md` so Telegram usage can match the API route style more naturally.
- Kept Kraken safety guardrails unchanged: no live-trading flag, dry-run default, Kraken order path, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py::test_entry_filled_command_accepts_hyphen_alias tests/test_telegram.py::test_submit_targets_command_accepts_hyphen_alias tests/test_telegram.py::test_audit_types_command_accepts_hyphen_alias -q` -> 3 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 86 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 17:49 UTC - Audit Event-Type Counters

- Chose Next Queue item 5: event-type shortcuts for operator audit diagnostics, because Docker/VPS validation and operator-reviewed metadata still require external environment/input.
- Added API `GET /audit/event-types`, returning `{items,total}` with audit `event_type`, count, and latest timestamp, sorted by most common type then name.
- Added Telegram `/audit_types` to show compact event-type counters so mobile operators can choose valid `/audit event_type=...` filters.
- Documented the new API and Telegram command in `README.md`.
- Kept Kraken safety guardrails unchanged: no Kraken order path, live-trading flag, dry-run default, secrets, or network submission behavior was changed.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `kraken_telegram_gateway/gateway/schemas.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py::test_audit_event_types_api_returns_counts_for_operator_filters -q` -> 1 passed, 1 Starlette/TestClient deprecation warning. `python3 -m pytest tests/test_telegram.py::test_audit_types_command_lists_event_type_counts -q` -> 1 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 83 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 16:44 UTC - Final GHCR Image Name

- Chose the deployability part of Next Queue item 1 because Docker validation still requires an environment with Docker installed.
- Set the Compose default image to the final public GHCR image: `ghcr.io/supermedi/kraken-telegram-gateway:latest`.
- Updated `DEPLOYMENT.md` so publish, push, tag, and VPS `.env` examples use the real `supermedi/kraken-telegram-gateway` image path instead of placeholders.
- Verified no old image placeholder remains in deployment docs/config.
- Kept Kraken safety guardrails unchanged: no live trading settings, order submission code, secrets, or dry-run defaults were changed.

Files changed: `compose.yml`, `DEPLOYMENT.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 81 passed, 1 Starlette/TestClient deprecation warning. `docker --version` -> unavailable in this container (`docker: command not found`), so build validation remains queued for the VPS/host environment.

### 2026-08-16 15:55 UTC - Kraken Balance Auth Retry

- Investigated a persistent Kraken Futures `authenticationError` on `/balance` after the endpoint-path signing fix.
- Cross-checked Kraken's current Derivatives REST auth docs and a Futures SDK implementation: the code already signs the v3 endpoint path without the `/derivatives` prefix.
- Hardened the balance request by matching known client headers, trimming configured API key/secret whitespace before signing, using the prepared request method, and retrying the read-only account lookup without `Nonce` only when Kraken specifically returns `authenticationError`.
- Documented that `/balance` requires Futures API keys, not Spot keys, and that the base URL must match live vs demo Futures credentials.
- Kept Kraken safety guardrails unchanged: this only touches read-only account lookup; no order submission path or live trading gate was enabled.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `tests/test_kraken.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_kraken.py -q` -> 17 passed. `python3 -m pytest -q` -> 81 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 15:44 UTC - Balance Filters

- Chose Next Queue item 5: small read-only balance diagnostics improvement for operator visibility.
- Added optional `account` and `currency` filters to API `GET /balance`, applied locally after the signed read-only Kraken balance lookup.
- Added matching Telegram filters for `/balance account=... currency=...` and `/solde`, with clean rejection of unsupported arguments.
- Documented filtered balance examples in `README.md`.
- Kept Kraken safety guardrails unchanged: no order path was touched, dry-run defaults remain enabled, and Kraken order network submission remains blocked.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py::test_balance_api_returns_read_only_kraken_balances tests/test_api.py::test_balance_api_filters_by_account_and_currency tests/test_api.py::test_balance_api_rejects_missing_kraken_credentials_cleanly -q` -> 3 passed, 1 Starlette/TestClient deprecation warning. `python3 -m pytest tests/test_telegram.py::test_balance_command_formats_kraken_futures_balances tests/test_telegram.py::test_balance_command_filters_account_and_currency tests/test_telegram.py::test_balance_command_rejects_invalid_filter tests/test_telegram.py::test_solde_alias_reports_missing_kraken_credentials -q` -> 4 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 79 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 15:27 UTC - API Balance Endpoint

- Chose a local read-only operator visibility task because VPS Docker validation and operator-reviewed Kraken metadata still require external environment/input.
- Added API `GET /balance`, reusing the existing Kraken Futures read-only account balance lookup used by Telegram `/balance` and `/solde`.
- Added an `AccountBalanceResponse` schema so API clients receive stable `account`, `currency`, `balance`, `equity`, `available`, and `margin` fields.
- Added clean HTTP 400 handling when Kraken credentials are missing or balance lookup preparation is refused, avoiding an internal server error.
- Documented the new `curl http://localhost:8000/balance` operator check in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken order submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/schemas.py`, `tests/test_api.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py -q` -> 15 passed, 1 Starlette/TestClient deprecation warning. `python3 -m pytest -q` -> 76 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. Follow-up targeted check: `python3 -m pytest tests/test_api.py::test_balance_api_returns_read_only_kraken_balances tests/test_api.py::test_balance_api_rejects_missing_kraken_credentials_cleanly -q` -> 2 passed, 1 warning.

### 2026-08-16 15:16 UTC - Telegram Balance Command

- Added `/balance` and `/solde` Telegram commands for read-only Kraken Futures account balance lookup.
- Added signed GET request preparation for `/derivatives/api/v3/accounts` that works in dry-run when Kraken API credentials are configured.
- Added flexible account-balance parsing and Telegram formatting for balance, equity, available, and margin fields.
- Documented the new commands in `README.md`.
- Follow-up fix: sign Kraken Futures requests with endpoint paths like `/api/v3/accounts` while still calling URLs under `/derivatives/api/v3/...`; this matches Kraken's REST auth docs and fixes `authenticationError` on `/balance`.

Tests: `python3 -m pytest -q` -> 74 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-14 19:50 UTC - Tracker Initialized

- Created this durable dev tracker for isolated autonomous cron cycles.
- Reflected current implemented state from project docs and source inspection.
- Next recommended work: expose and test planned order visibility before adding real Kraken signing.

Tests: not run for this tracker-only change.

### 2026-08-14 20:08 UTC - Docker Registry Prep

- Added `Dockerfile` for a public, secret-free Python 3.12 runtime image.
- Added `compose.yml` using `env_file: .env`, a persistent `/data` volume, healthcheck, and configurable `IMAGE_NAME`.
- Added `.dockerignore` to exclude `.env`, local DB files, git metadata, and private memory/dev files from build context.
- Added GHCR publish workflow at `.github/workflows/publish-docker.yml`.
- Added `DEPLOYMENT.md` with VPS, webhook, and image update steps.

Tests: `python3 -m pytest -q` -> 12 passed. Docker build not run because Docker is not installed in the current OpenClaw container.

### 2026-08-14 20:18 UTC - Dedicated GitHub Repository

- Created public GitHub repository: `https://github.com/supermedi/kraken-telegram-gateway`.
- Published a clean initial commit from a temporary release folder, excluding OpenClaw private workspace files and local secrets.
- Configured commit author as `supermedi <108479582+supermedi@users.noreply.github.com>`.
- Pushed branch `main`; GHCR publish workflow is included in the repository.

Tests before publish: `python3 -m pytest -q` -> 12 passed.

### 2026-08-15 00:05 UTC - Planned Order Visibility

- Chose Next Queue item 1: API/Telegram visibility for planned orders attached to a trade.
- Added `TradeDetail` response shape so `GET /trades/{trade_id}` returns the trade plus attached entry/target orders.
- Added service helpers to load orders in deterministic entry/target order and format a compact Telegram status view.
- Updated `/status <trade_id>` to show trade summary plus planned order lines, including reduce-only flag, target percent, status, and dry-run external id when present.
- Documented the richer `/status <trade_id>` behavior in `README.md`.
- Added HTTP and Telegram tests for attached order visibility.

Files changed: `kraken_telegram_gateway/gateway/schemas.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 14 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 01:10 UTC - Kraken Futures Signer Gate

- Chose Next Queue item 1: Kraken Futures REST signer interface behind the existing live-trading gate.
- Added `KrakenFuturesSigner` implementing Derivatives REST `Authent` generation from URL-encoded post data, nonce, endpoint path, and base64 API secret.
- Added `KrakenAuthenticatedRequest` and `KrakenClient.build_private_request(...)` so authenticated headers/request metadata can be prepared only when `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`, and credentials are present.
- Kept `submit_entry_order(...)` dry-run first; live network submission still raises `NotImplementedError` and does not send orders.
- Added `KRAKEN_FUTURES_BASE_URL` config/default and documented that signing is prepared but live submission is still blocked.
- Added tests for deterministic signing, dry-run gate refusal, signed request preparation only when the live gate is open, and dry-run submission without valid Kraken secrets.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `kraken_telegram_gateway/gateway/config.py`, `tests/test_kraken.py`, `.env.example`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 20 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 02:05 UTC - Safe Live Payload Boundary

- Chose Next Queue item 1: safe live-order payload boundary for Kraken Futures contract sizing.
- Added `InstrumentMetadata`, `KrakenOrderPayloadError`, and `KrakenClient.build_entry_order_payload(...)`.
- Added decimal contract-size calculation from `amount_usdc * leverage`, rounded down to instrument `size_step`, with explicit minimum-size and metadata mismatch refusals.
- Changed live-gate entry submission so missing instrument metadata returns `mode=blocked`; confirmation marks the trade `rejected`, leaves planned orders untouched, and records a `trade_rejected` audit event.
- Documented that USDC amounts cannot become live Kraken `size` values without verified instrument metadata.
- Did not enable live trading, disable dry-run defaults, or submit any network order.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `kraken_telegram_gateway/gateway/service.py`, `tests/test_kraken.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 24 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 03:05 UTC - Local Instrument Metadata Cache

- Chose Next Queue item 2: instrument metadata provider/cache for Kraken Futures contract sizing.
- Added `KRAKEN_INSTRUMENT_METADATA_PATH` and `LocalInstrumentMetadataProvider` for a manually verified local JSON cache of contract value, size step, and minimum size.
- Updated live-gate entry submission to read metadata from the provider and build the signed request boundary when metadata exists, but still return `mode=blocked` before any network submission.
- Preserved the missing-metadata rejection path so live confirmation remains rejected when no verified metadata is configured.
- Documented the local metadata JSON format and reiterated that V1 still blocks Kraken network submission even with metadata.

Files changed: `kraken_telegram_gateway/gateway/config.py`, `kraken_telegram_gateway/gateway/kraken.py`, `tests/test_kraken.py`, `.env.example`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 26 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 04:09 UTC - Metadata Cache Validator

- Chose Next Queue item 2 because Docker is still unavailable in the current OpenClaw container (`docker: command not found`).
- Added `kraken_telegram_gateway.gateway.metadata` with a local JSON cache validator for Kraken Futures instrument metadata.
- Added the `kraken-metadata-validate` console script to verify readable JSON, accepted object/list cache shapes, positive `contract_value_usdc`, `size_step`, `min_size`, and required symbols before VPS use.
- Documented the validation command and explicitly noted that it does not contact Kraken.
- Kept all live-trading guardrails unchanged: dry-run defaults remain enabled and Kraken network submission remains blocked.

Files changed: `kraken_telegram_gateway/gateway/metadata.py`, `tests/test_metadata.py`, `pyproject.toml`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 30 passed, 1 Starlette/TestClient deprecation warning. `python3 -m kraken_telegram_gateway.gateway.metadata --help` -> OK.

### 2026-08-15 05:05 UTC - Telegram Orders Command

- Chose Next Queue item 5: dedicated Telegram visibility for attached orders when `/status` becomes too dense.
- Added `format_trade_orders(...)` for a compact order-only Telegram response with trade side/pair/status and deterministic attached order lines.
- Added `/orders <trade_id>` to the Telegram dispatcher, including missing-trade handling and `/help` visibility.
- Documented `/orders <trade_id>` in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest -q` -> 32 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 06:05 UTC - API Order Filters

- Chose Next Queue item 5: API-level order filters for status-specific operator views.
- Added `GET /trades/{trade_id}/orders` with optional `status` and `role` query filters using existing order enums.
- Extended service order listing so filtered responses keep the deterministic entry/target sort order and return 404 for unknown trades.
- Added API tests for filtering by `status=planned`, `role=entry`, combined target filters, and missing trades.
- Documented the filtered order API examples in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `tests/test_api.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py -q` -> 3 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 34 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 07:05 UTC - Recent Trade Listing

- Chose Next Queue item 5: richer API pagination/listing across recent trades for operator workflows.
- Added `GET /trades` returning `{items,total,limit,offset}` sorted newest-first, with bounded `limit`, non-negative `offset`, and optional `status` and `pair` filters.
- Added a `TradeList` response schema and service-level `list_trades(...)` helper using a separate total count query.
- Added API coverage for newest-first order, status filter, pair normalization/filter, and `limit`/`offset` pagination.
- Documented the new listing endpoint and filter examples in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/schemas.py`, `tests/test_api.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py -q` -> 4 passed, 1 Starlette/TestClient deprecation warning. `python3 -m pytest -q` -> 35 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK.

### 2026-08-15 08:05 UTC - Telegram Recent Trades Command

- Chose Next Queue item 5: mobile cross-trade visibility from Telegram now that the API listing exists.
- Added `format_trade_list(...)` for compact Telegram output showing newest trades, status, side/pair, amount, and entry price.
- Added `/trades [limit=5 offset=0 status=... pair=...]` to the Telegram dispatcher with key/value parsing, status enum validation, pair normalization via the service, and a mobile-safe limit cap of 10.
- Updated `/help` and `README.md` to document the new Telegram command and filters.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py -q` -> 15 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 38 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 09:05 UTC - Telegram Orders Filters

- Chose Next Queue item 5: status-specific target views for mobile operator workflows.
- Added optional `/orders <trade_id> status=... role=...` parsing for Telegram, reusing the existing `OrderStatus` and `OrderRole` enums.
- Filtered `/orders` output locally from the deterministic trade detail order list so operators can show only planned targets after entry dry-run submission.
- Updated `/help` and `README.md` to document the new filters.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_telegram.py -q` -> 17 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 40 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 10:05 UTC - Optional Stop-Loss Confirmation Gate

- Chose Next Queue item 3: stricter no-stop-loss confirmation policy.
- Added `REQUIRE_STOP_LOSS_FOR_CONFIRMATION=false` as a default-off safety setting.
- When enabled, confirming a trade without `stop=` now rejects the trade before any Kraken adapter call, records a `trade_rejected` audit event, and leaves attached planned orders untouched.
- Verified that trades with a stop still confirm through the existing dry-run path when the policy is enabled.
- Documented the setting in `.env.example` and `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/config.py`, `kraken_telegram_gateway/gateway/service.py`, `.env.example`, `README.md`, `tests/test_api.py`, `tests/test_telegram.py`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 24 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 43 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 11:05 UTC - Audit Visibility

- Chose Next Queue item 5: API/Telegram audit visibility for rejected confirmations and safety gates.
- Added `GET /audit` returning `{items,total,limit,offset}` sorted newest-first, with optional `trade_id` and `event_type` filters.
- Added `AuditEventList`, service-level `list_audit_events(...)`, and compact Telegram formatting for audit entries.
- Added `/audit [trade_id] [event_type=... limit=5]` to Telegram so mobile operators can inspect rejection/safety events without database access.
- Documented the new API and Telegram audit surfaces in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/schemas.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 27 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 46 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 12:05 UTC - Entry Fill Lifecycle Marker

- Chose Next Queue item 3: order status tracking for entry fill -> target placement.
- Added `entry_filled` trade status plus `filled` and `ready_to_submit` order statuses.
- Added `mark_entry_filled(...)` service logic and `POST /commands/entry-filled/{trade_id}` so a confirmed entry can be marked filled locally; entry orders become `filled`, reduce-only target exits move from `planned` to `ready_to_submit`, and an `entry_filled` audit event is recorded.
- Added Telegram `/entry_filled <trade_id>` for mobile operator control and documented the API/Telegram workflow.
- Updated cancellation so planned and `ready_to_submit` orders are cancelled if the trade is cancelled after entry fill.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched. Docker validation is still blocked in this container because `docker` is not installed.

Files changed: `kraken_telegram_gateway/gateway/models.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 31 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 50 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 13:05 UTC - Dry-Run Target Submission Boundary

- Chose Next Queue item 3: dry-run submission boundary for `ready_to_submit` reduce-only target exits.
- Added `KrakenClient.submit_target_order(...)` plus `build_target_order_payload(...)`, reusing instrument metadata sizing and enforcing `reduceOnly=true` for target exits.
- Added `submit_ready_targets(...)`, `POST /commands/submit-targets/{trade_id}`, and Telegram `/submit_targets <trade_id>` so targets move from `ready_to_submit` to `dry_run_submitted` with local `dryrun-target-*` external ids.
- Added audit event `targets_submitted` for successful dry-run target marking and `targets_blocked` for blocked attempts.
- Documented the `/entry_filled` -> `/submit_targets` operator flow in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled, no live Kraken submission was enabled, and live target submission remains blocked even with metadata and credentials.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `kraken_telegram_gateway/gateway/service.py`, `kraken_telegram_gateway/gateway/app.py`, `kraken_telegram_gateway/gateway/telegram.py`, `tests/test_api.py`, `tests/test_telegram.py`, `tests/test_kraken.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py tests/test_kraken.py -q` -> 46 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 57 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 14:05 UTC - Entry Fill Idempotency

- Chose Telegram robustness work from the lifecycle flow: safe retry handling for `/entry_filled <trade_id>`.
- Made `mark_entry_filled(...)` idempotent when a trade is already marked `entry_filled`, entry orders are already `filled`, and no target remains `planned`.
- A repeated API or Telegram `/entry_filled` now returns a no-op message and does not create a duplicate `entry_filled` audit event.
- Documented the idempotent `/entry_filled` behavior in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled, no live Kraken submission path was touched, and Docker validation is still blocked in this container because `docker` is not installed.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 37 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 59 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-15 15:08 UTC - Submit Targets Retry No-Op

- Chose Next Queue item 4: clearer retry/no-op behavior for `/submit_targets <trade_id>` after targets are already submitted.
- Made `submit_ready_targets(...)` return an explicit no-op message when target exits are already `dry_run_submitted` or `live_submitted`, preserving existing external ids and avoiding duplicate `targets_submitted` audit events.
- Added API and Telegram tests for repeated target submission retries after the dry-run target submission boundary.
- Documented the idempotent `/submit_targets` retry behavior in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 39 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 61 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 14:37 UTC - Cancel Retry No-Op

- Chose local Telegram robustness work because VPS Docker validation and operator-reviewed Kraken metadata are still external-environment tasks.
- Made `cancel_trade(...)` idempotent when a trade is already `cancelled`, returning an explicit no-op message without changing orders or creating another `trade_cancelled` audit event.
- Added API and Telegram tests for repeated cancellation retries by mobile operators.
- Documented the idempotent `/cancel <trade_id>` retry behavior in `README.md`.
- Kept Kraken safety guardrails unchanged: dry-run defaults remain enabled and no live Kraken submission path was touched.

Files changed: `kraken_telegram_gateway/gateway/service.py`, `tests/test_api.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_api.py tests/test_telegram.py -q` -> 41 passed, 1 Starlette/TestClient deprecation warning. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 63 passed, 1 Starlette/TestClient deprecation warning.

### 2026-08-16 23:46 UTC - Live Kraken Order Submission

- User explicitly approved removing the final V1 network-submission block without adding a separate feature flag.
- Kept the existing live gates as the only activation path: `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`, and valid Kraken Futures credentials.
- Changed live entry and reduce-only target submission to POST signed payloads to Kraken Futures `/derivatives/api/v3/sendorder`.
- Kraken order API errors now keep the local trade/order blocked instead of marking it submitted.
- Updated live target submission messaging, README safety notes, and current dev-log guardrails.

Files changed: `kraken_telegram_gateway/gateway/kraken.py`, `kraken_telegram_gateway/gateway/service.py`, `tests/test_kraken.py`, `tests/test_telegram.py`, `README.md`, `DEV_LOG.md`.

Tests: `python3 -m pytest tests/test_kraken.py -q` -> 20 passed. `python3 -m compileall -q kraken_telegram_gateway` -> OK. `python3 -m pytest -q` -> 95 passed, 1 Starlette/TestClient deprecation warning.
