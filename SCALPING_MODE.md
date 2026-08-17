# Scalping Mode Plan

This document defines the first safe design for an experimental Kraken Futures scalping mode.

The objective is not to promise a fixed win rate. The objective is to run short, controlled sessions that can measure whether a strategy has positive net expectancy after fees, spread, slippage, and failed fills.

## Operating Model

The mode runs as a separate session from manual `/trade` flows.

Initial Telegram command:

```text
/scalp_start pair=PF_LINKUSD side=both amount_usdc=100 leverage=2 duration=60m max_hold=5m max_losses=3 min_net_pnl=5 mode=paper
```

Companion commands:

```text
/scalp_status [session_id]
/scalp_stop <session_id>
/scalp_report <session_id>
/scalp_tick_kraken snapshots=2 timeout=10
```

The first implementation must be paper-only. Live execution can be added later behind the existing live gates plus a dedicated scalping gate.

## Parameters

- `pair`: Kraken Futures symbol, for example `PF_LINKUSD`.
- `side`: `buy`, `sell`, or `both`.
- `amount_usdc`: intended notional per trade before leverage sizing.
- `leverage`: leverage used for sizing and risk reporting.
- `duration`: total session length. Default candidate: `60m`.
- `max_hold`: maximum time a trade may stay open. This can be seconds or minutes; default candidate: `5m`.
- `max_losses`: automatic stop after this many losing closed trades. User target: `3`.
- `min_net_pnl`: desired minimum net profit or loss threshold per closed trade in USD. User target: `5`.
- `mode`: `paper` initially. `live` must be rejected until the live scalping gate exists.

## Session Rules

- One active scalping session per pair at first.
- One open scalping trade per session at first.
- No averaging down in V1.
- No martingale or position doubling after a loss.
- Stop immediately if `max_losses` is reached.
- Stop immediately when session `duration` expires.
- Stop immediately on `/pause` or `/scalp_stop`.
- Force close or mark stale when `max_hold` is reached.
- Every decision must create audit data: signal, spread, book imbalance, trade flow, entry, exit, estimated fees, net PnL, and stop reason.

## Signal Inputs

V1 should support a small set of transparent signals:

- Order book imbalance over top N levels.
- Recent trade aggressor flow if available.
- Short-window volume spike versus local rolling average.
- Spread ceiling before entering.
- Optional micro-trend filter to avoid fading strong moves.

The signal should produce a score and a reason string. Paper reports must show both, so the operator can reject strategies that look lucky but unstable.

## Trade Lifecycle

1. Observe market data.
2. If no trade is open, evaluate signal.
3. If signal passes thresholds, create a paper scalp trade with intended entry.
4. Simulate fill only when market price crosses the entry rule with acceptable spread.
5. While open, monitor:
   - desired `min_net_pnl`,
   - loss threshold,
   - signal invalidation,
   - `max_hold`.
6. Close paper trade and compute net PnL after estimated maker/taker fees.
7. Increment loss counter only after net PnL is negative.
8. Stop the session if `max_losses`, `duration`, or manual stop is reached.

## Data Model Proposal

Add tables separate from manual trades:

- `ScalpSession`: pair, side mode, amount, leverage, duration, max hold, max losses, min net PnL, mode, status, timestamps, stop reason.
- `ScalpTrade`: session id, side, entry/exit prices, amount, leverage, opened/closed timestamps, gross PnL, estimated fees, net PnL, status, close reason.
- `ScalpSignal`: session id, optional scalp trade id, score, signal kind, spread, imbalance, volume metrics, raw reason, timestamp.

Keeping this separate avoids forcing high-frequency experiment state into the existing manual `Trade` and `TradeOrder` lifecycle.

## Reporting

Telegram status should stay compact:

```text
Scalp session: <session_id>
Pair: PF_LINKUSD
Mode: paper
Runtime: 18m / 60m
Trades: 7 closed, 1 open
Wins: 5 | Losses: 2
Net PnL: +18.42 USD
Avg hold: 2m14s
Stop: active
```

Final report should include:

- net PnL after estimated fees,
- win rate,
- average win and average loss,
- max drawdown during session,
- number of rejected signals,
- stop reason,
- whether results satisfy the configured thresholds.

## Current V1 Foundation

Implemented:

- Persistent `ScalpSession`, `ScalpTrade`, and `ScalpSignal` tables.
- `/scalp_start` parser with `duration=60m`, `max_hold=5m`, `max_losses=3`, `min_pnl=5`, `amount`/`amount_usdc`, and `mode=paper`.
- Telegram commands `/scalp_start`, `/scalp_status`, `/scalp_stop`, and `/scalp_report`.
- Telegram command `/scalp_tick_kraken` to run a manual Kraken public WebSocket paper tick.
- API endpoints `POST /commands/scalp-start`, `GET /scalp/{session_id}`, `GET /scalp/{session_id}/report`, and `POST /commands/scalp-stop/{session_id}`.
- Paper-only enforcement: `mode=live` is rejected.
- Compact status/report formatting with winrate, gross/net PnL, estimated fees, max drawdown, rejected signals, close reasons, and stop reason from persisted paper data.
- Synthetic market-data adapter and paper runner for deterministic tests.
- V1 signal evaluation from spread, top-of-book imbalance, and local volume ratio.
- Runner rules for one open trade at a time, net-PnL close, max-hold close, duration stop, and max-losses stop.
- Injectable active-session scheduler plus manual API ticks.
- Kraken Futures public WebSocket adapter for book/ticker_lite snapshots feeding paper sessions.
- Opt-in FastAPI background loop for active paper sessions via `SCALP_KRAKEN_SCHEDULER_ENABLED=true`.
- Offline deterministic replay CLI `kraken-scalp-replay` for one or more JSON, JSONL, or CSV snapshot files. It runs paper sessions in an in-memory SQLite database and emits either a single-session JSON report or a multi-replay summary without contacting Kraken.

Not implemented yet:

- Richer historical-data source adapters.
- Live order submission.

## Implementation Phases

1. Done: add command parsing, data models, Telegram/API commands, and paper session state without market-data automation.
2. Done: add market-data adapter interface and deterministic tests with synthetic ticks/book snapshots.
3. Done: add paper runner with one open trade at a time and core stop rules.
4. Done: add background scheduling for active paper sessions through an injectable snapshot provider and manual API tick.
5. Done: add Kraken WebSocket market-data integration for manual paper scheduler ticks through API and Telegram.
6. Done: add a periodic opt-in background loop for active paper sessions.
7. Done: add a deterministic offline replay CLI for saved JSON/JSONL/CSV snapshots.
8. Done: add multi-file replay summaries for comparing paper validation runs.
9. Only after repeated paper validation, add a separate `SCALPING_LIVE_ENABLED=true` gate for live orders.

## Safety Notes

Scalping with a target such as 80% winning trades is possible to measure but should not be treated as a guarantee. A strategy can win often and still lose money if losses, spread, slippage, or taker fees dominate. The acceptance target should be positive net PnL, controlled drawdown, and stable behavior across multiple paper sessions.
