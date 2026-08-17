from decimal import Decimal

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import AccountBalance, KrakenAccountError, KrakenFill
from kraken_telegram_gateway.gateway.models import (
    AuditEvent,
    OrderRole,
    OrderStatus,
    ProcessedTelegramUpdate,
    ScalpSession,
    ScalpSessionStatus,
    ScalpSignal,
    ScalpTrade,
    ScalpTradeStatus,
    Trade,
    TradeOrder,
)
from kraken_telegram_gateway.gateway.schemas import ScalpSchedulerResult
from kraken_telegram_gateway.gateway.telegram import (
    dispatch_telegram_messages,
    dispatch_telegram_text,
    handle_telegram_update,
    render_telegram_html,
    send_telegram_message,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_trade_message_creates_preview_and_confirm_hint():
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:40% t3=72000:20%",
            session,
            Settings(max_amount_usdc=100),
        )

    assert "Preview creee" in reply
    assert "Avertissement: Aucun stop loss" in reply
    assert "Confirmer: /confirm" in reply
    assert "```bash\n/confirm " in reply
    assert "```\n\n```bash\n/cancel " in reply
    assert reply.endswith("```")


def test_trade_message_returns_copyable_trade_id_as_separate_message():
    with make_session() as session:
        replies = dispatch_telegram_messages(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            Settings(max_amount_usdc=100),
        )

    trade_id = next(line.split(": ", 1)[1] for line in replies[0].splitlines() if line.startswith("Trade ID:"))
    assert replies == [replies[0], trade_id]
    assert replies[1] == trade_id
    assert "\n" not in replies[1]


def test_scalp_start_creates_paper_session_with_multi_minute_hold():
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/scalp_start pair=LINK amount_usdc=100 leverage=2 duration=60m max_hold=5m max_losses=3 min_pnl=5",
            session,
            Settings(max_amount_usdc=100),
        )
        scalp_session = session.exec(select(ScalpSession)).one()

    assert "Session scalp paper creee" in reply
    assert "Aucun ordre Kraken ne sera envoye" in reply
    assert "Pair: PF_LINKUSD" in reply
    assert "Runtime cible: 1h" in reply
    assert "Max hold: 5m" in reply
    assert "Losses: 0 / 3" in reply
    assert scalp_session.pair == "PF_LINKUSD"
    assert scalp_session.amount_usdc == 100
    assert scalp_session.leverage == 2
    assert scalp_session.duration_seconds == 3600
    assert scalp_session.max_hold_seconds == 300
    assert scalp_session.max_losses == 3
    assert scalp_session.min_net_pnl == 5
    assert scalp_session.mode == "paper"
    assert scalp_session.status == ScalpSessionStatus.PAPER_ACTIVE


def test_scalp_start_rejects_live_mode_without_live_flag():
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 mode=live",
            session,
            Settings(max_amount_usdc=100),
        )

    assert "Commande refusee" in reply
    assert "SCALP_LIVE_ENABLED=true" in reply


def test_scalp_start_creates_live_session_when_all_gates_are_open():
    settings = Settings(
        max_amount_usdc=100,
        scalp_live_max_amount_usdc=25,
        scalp_live_enabled=True,
        dry_run=False,
        live_trading_enabled=True,
        kraken_api_key="public-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
        allowed_pairs="PF_LINKUSD",
    )
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/scalp_start pair=PF_LINKUSD amount_usdc=10 mode=live side=buy duration=60m max_hold=5m",
            session,
            settings,
        )
        scalp_session = session.exec(select(ScalpSession)).one()

    assert "Session scalp live creee" in reply
    assert "Mode: live" in reply
    assert scalp_session.mode == "live"
    assert scalp_session.status == ScalpSessionStatus.LIVE_ACTIVE


def test_scalp_status_stop_and_report_use_paper_metrics():
    with make_session() as session:
        start_reply = dispatch_telegram_text(
            "/scalp_start pair=PF_LINKUSD amount_usdc=100 duration=60m max_hold=5m",
            session,
            Settings(max_amount_usdc=100),
        )
        session_id = next(line.split(": ", 1)[1] for line in start_reply.splitlines() if line.startswith("Scalp session:"))
        scalp_session = session.get(ScalpSession, session_id)
        assert scalp_session is not None
        session.add(
            ScalpTrade(
                session_id=session_id,
                pair="PF_LINKUSD",
                side="buy",
                amount_usdc=100,
                leverage=1,
                entry_price=10,
                exit_price=10.1,
                gross_pnl=6.5,
                estimated_fees=0.5,
                net_pnl=6,
                status=ScalpTradeStatus.PAPER_CLOSED,
                close_reason="min_net_pnl",
                closed_at=scalp_session.started_at,
            )
        )
        session.add(
            ScalpTrade(
                session_id=session_id,
                pair="PF_LINKUSD",
                side="sell",
                amount_usdc=100,
                leverage=1,
                entry_price=10,
                exit_price=10.2,
                gross_pnl=-2,
                estimated_fees=0.5,
                net_pnl=-2.5,
                status=ScalpTradeStatus.PAPER_CLOSED,
                close_reason="max_hold",
                closed_at=scalp_session.started_at,
            )
        )
        session.add(
            ScalpTrade(
                session_id=session_id,
                pair="PF_LINKUSD",
                side="sell",
                amount_usdc=100,
                leverage=1,
                entry_price=10,
                status=ScalpTradeStatus.PAPER_OPEN,
            )
        )
        session.add(
            ScalpSignal(
                session_id=session_id,
                signal_kind="book_volume_v1",
                score=0.1,
                reason="imbalance below threshold",
            )
        )
        session.commit()

        status_reply = dispatch_telegram_text(f"/scalp_status {session_id}", session, Settings(max_amount_usdc=100))
        report_reply = dispatch_telegram_text(f"/scalp_report {session_id}", session, Settings(max_amount_usdc=100))
        stop_reply = dispatch_telegram_text(f"/scalp_stop {session_id}", session, Settings(max_amount_usdc=100))
        scalp_session = session.get(ScalpSession, session_id)

    assert "Trades: 2 closed, 1 open" in status_reply
    assert "Wins: 1 | Losses: 1 / 3" in status_reply
    assert "Net PnL: +3.50 USD" in status_reply
    assert "winrate=50.0%" in report_reply
    assert "Net PnL: +3.50 USD | gross=+4.50 | frais=1.00" in report_reply
    assert "Avg win=+6.00 | avg loss=-2.50 | max drawdown=2.50" in report_reply
    assert "Signaux rejetes: 1" in report_reply
    assert "Raisons de cloture: max_hold=1, min_net_pnl=1" in report_reply
    assert "Session scalp arretee" in stop_reply
    assert "Statut: stopped" in stop_reply
    assert scalp_session is not None
    assert scalp_session.status == ScalpSessionStatus.STOPPED
    assert scalp_session.stop_reason == "manual_stop"


def test_scalp_sync_fills_command_marks_live_entry_filled(monkeypatch):
    def fake_fetch_recent_fills(self):
        return [
            KrakenFill(
                order_id="OID-SCALP-1",
                symbol="PF_LINKUSD",
                side="buy",
                price=Decimal("10.02"),
            )
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_recent_fills",
        fake_fetch_recent_fills,
    )
    settings = Settings(kraken_api_key="public-key", kraken_api_secret="dGVzdC1zZWNyZXQ=")
    with make_session() as session:
        scalp_session = ScalpSession(
            id="scalp-live-1",
            pair="PF_LINKUSD",
            side_mode="buy",
            amount_usdc=10,
            leverage=1,
            duration_seconds=3600,
            max_hold_seconds=300,
            max_losses=1,
            min_net_pnl=1,
            mode="live",
            status=ScalpSessionStatus.LIVE_ACTIVE,
        )
        trade = ScalpTrade(
            id="scalp-trade-1",
            session_id=scalp_session.id,
            pair="PF_LINKUSD",
            side="buy",
            amount_usdc=10,
            leverage=1,
            entry_price=10.01,
            status=ScalpTradeStatus.LIVE_SUBMITTED,
            external_order_id="OID-SCALP-1",
        )
        session.add(scalp_session)
        session.add(trade)
        session.commit()

        reply = dispatch_telegram_text("/scalp_sync_fills scalp-live-1", session, settings)
        refreshed_trade = session.get(ScalpTrade, trade.id)

    assert "Sync fills: 1 entree(s) scalp live marquee(s) filled, 0 ignoree(s)." in reply
    assert "Statut: live_active" in reply
    assert "Scannes: 1 | Filled: 1 | Ignores: 0" in reply
    assert refreshed_trade is not None
    assert refreshed_trade.status == ScalpTradeStatus.LIVE_ENTRY_FILLED


def test_scalp_tick_kraken_runs_scheduler_from_telegram(monkeypatch):
    calls = []

    def fake_run_active_scalp_paper_sessions_from_kraken(session, *, snapshots_per_session, timeout_seconds, settings):
        calls.append(
            {
                "session": session,
                "snapshots_per_session": snapshots_per_session,
                "timeout_seconds": timeout_seconds,
                "settings": settings,
            }
        )
        return ScalpSchedulerResult(
            scanned=1,
            processed=1,
            skipped=0,
            messages=["scalp-1: Session scalp traitee."],
        )

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.telegram.run_active_scalp_paper_sessions_from_kraken",
        fake_run_active_scalp_paper_sessions_from_kraken,
    )

    with make_session() as session:
        reply = dispatch_telegram_text(
            "/scalp_tick_kraken snapshots=2 timeout=3",
            session,
            Settings(max_amount_usdc=100),
        )

    assert calls[0]["snapshots_per_session"] == 2
    assert calls[0]["timeout_seconds"] == 3
    assert calls[0]["settings"].max_amount_usdc == 100
    assert "Tick Kraken scalp termine" in reply
    assert "Sessions scannees: 1" in reply
    assert "Traitees: 1" in reply
    assert "- scalp-1: Session scalp traitee." in reply


def test_scalp_tick_kraken_rejects_invalid_options():
    with make_session() as session:
        reply = dispatch_telegram_text(
            "/scalp_tick_kraken snapshots=99",
            session,
            Settings(max_amount_usdc=100),
        )

    assert "Commande refusee" in reply
    assert "snapshots must be between 1 and 10" in reply


def test_render_telegram_html_preserves_code_block_without_markdown_underscores():
    rendered = render_telegram_html("Pair PF_XBTUSD\n```bash\n/confirm trade-1\n```")

    assert rendered == 'Pair PF_XBTUSD\n<pre><code class="language-bash">/confirm trade-1</code></pre>'


def test_render_telegram_html_keeps_separate_action_blocks_spaced_for_copy_buttons():
    rendered = render_telegram_html("```bash\n/confirm trade-1\n```\n\n```bash\n/cancel trade-1\n```")

    assert rendered == (
        '<pre><code class="language-bash">/confirm trade-1</code></pre>\n\n'
        '<pre><code class="language-bash">/cancel trade-1</code></pre>'
    )


@pytest.mark.anyio
async def test_send_telegram_message_enables_html_parse_mode(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json):
            calls.append((url, json, self.timeout))
            return FakeResponse()

    monkeypatch.setattr("kraken_telegram_gateway.gateway.telegram.httpx.AsyncClient", FakeAsyncClient)

    await send_telegram_message(
        123,
        "```bash\n/confirm trade-1\n```",
        Settings(telegram_bot_token="token"),
    )

    assert calls == [
        (
            "https://api.telegram.org/bottoken/sendMessage",
            {
                "chat_id": 123,
                "text": '<pre><code class="language-bash">/confirm trade-1</code></pre>',
                "parse_mode": "HTML",
            },
            10,
        )
    ]


def test_confirm_executes_dry_run_after_preview():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, Settings(max_amount_usdc=100))

    assert "Dry-run: no Kraken order was submitted." in confirm_reply
    assert "Statut: dry_run_executed" in confirm_reply


def test_trade_preview_creates_entry_and_reduce_only_target_orders():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:40% t3=72000:20%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    entry_orders = [order for order in orders if order.role == OrderRole.ENTRY]
    target_orders = [order for order in orders if order.role == OrderRole.TARGET_EXIT]

    assert len(entry_orders) == 1
    assert entry_orders[0].side == "buy"
    assert entry_orders[0].reduce_only is False
    assert len(target_orders) == 3
    assert {order.side for order in target_orders} == {"sell"}
    assert all(order.reduce_only for order in target_orders)
    assert sum(order.amount_usdc for order in target_orders) == 100
    assert all(order.status == OrderStatus.PLANNED for order in orders)


def test_trade_preview_allows_entry_without_targets():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 stop=63000",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    assert "targets=aucune" in preview_reply
    assert len(orders) == 1
    assert orders[0].role == OrderRole.ENTRY
    assert orders[0].reduce_only is False


def test_compact_trade_message_defaults_pair_to_usd_futures():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "LINK LONG 25USDC 2x Entry 9.356 Sl 9.298",
            session,
            Settings(max_amount_usdc=100, max_leverage=2, allowed_pairs="*"),
        )

    assert "BUY PF_LINKUSD" in preview_reply
    assert "montant=25 USDC" in preview_reply
    assert "entry=limit:9.356" in preview_reply
    assert "targets=aucune" in preview_reply
    assert "stop=9.298" in preview_reply
    assert "leverage=2x" in preview_reply


def test_confirm_marks_only_entry_order_as_dry_run_submitted():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, Settings(max_amount_usdc=100))
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    entry_order = next(order for order in orders if order.role == OrderRole.ENTRY)
    target_order = next(order for order in orders if order.role == OrderRole.TARGET_EXIT)

    assert entry_order.status == OrderStatus.DRY_RUN_SUBMITTED
    assert entry_order.external_order_id == f"dryrun-{trade_id}"
    assert target_order.side == "buy"
    assert target_order.reduce_only is True
    assert target_order.status == OrderStatus.PLANNED


def test_confirm_rejects_missing_stop_when_required():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        trade = session.get(Trade, trade_id)
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "trade_rejected")
        ).one()

    assert "stop loss requis" in confirm_reply
    assert "Statut: rejected" in confirm_reply
    assert trade is not None
    assert trade.status == "rejected"
    assert all(order.status == OrderStatus.PLANNED for order in orders)
    assert "stop loss requis" in audit.message


def test_confirm_accepts_stop_when_stop_policy_required():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:100% stop=63000",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

    assert "Dry-run: no Kraken order was submitted." in confirm_reply
    assert "Statut: dry_run_executed" in confirm_reply


def test_confirm_with_live_gate_open_rejects_when_kraken_rejects_order(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "error", "error": "authenticationError"}

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.httpx.request",
        lambda method, url, *, headers, content, timeout: FakeResponse(),
    )
    settings = Settings(
        max_amount_usdc=100,
        dry_run=False,
        live_trading_enabled=True,
        kraken_api_key="public-key",
        kraken_api_secret="dGVzdC1zZWNyZXQ=",
    )
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        trade = session.get(Trade, trade_id)
        entry_order = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.ENTRY)
        ).one()

    assert "Live Kraken submission failed: authenticationError" in confirm_reply
    assert "Statut: rejected" in confirm_reply
    assert trade is not None
    assert trade.status == "rejected"
    assert entry_order.status == OrderStatus.PLANNED


def test_status_includes_planned_order_visibility():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        status_reply = dispatch_telegram_text(f"/status {trade_id}", session, Settings(max_amount_usdc=100))

    assert f"Trade ID: {trade_id}" in status_reply
    assert "Ordres planifies:" in status_reply
    assert "- entry: buy limit 65000 | 100 USDC | reduce-only=non | statut=planned" in status_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=planned" in status_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=planned" in status_reply


def test_balance_command_formats_kraken_futures_balances(monkeypatch):
    def fake_fetch_account_balances(self):
        return [
            AccountBalance(
                account="flex",
                currency="USDC",
                balance=100,
                equity=105,
                available=90,
                margin=15,
            )
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_account_balances",
        fake_fetch_account_balances,
    )

    with make_session() as session:
        reply = dispatch_telegram_text("/balance", session, Settings())

    assert reply == "Solde Kraken Futures:\n- flex USDC | balance=100 | equity=105 | available=90 | margin=15"


def test_balance_command_hides_empty_instrument_accounts(monkeypatch):
    def fake_fetch_account_balances(self):
        return [
            AccountBalance(account="fi_adausd", currency="ADA"),
            AccountBalance(account="fi_linkusd", currency="LINK", balance=0, available=0),
            AccountBalance(account="flex", currency="USDC", balance=96.8, available=96.8),
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_account_balances",
        fake_fetch_account_balances,
    )

    with make_session() as session:
        reply = dispatch_telegram_text("/balance", session, Settings())

    assert reply == "Solde Kraken Futures:\n- flex USDC | balance=96.8 | available=96.8"


def test_balance_command_filters_account_and_currency(monkeypatch):
    def fake_fetch_account_balances(self):
        return [
            AccountBalance(account="flex", currency="USDC", balance=100),
            AccountBalance(account="cash", currency="USD", balance=25),
            AccountBalance(account="flex", currency="ETH", balance=2),
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_account_balances",
        fake_fetch_account_balances,
    )

    with make_session() as session:
        reply = dispatch_telegram_text("/balance account=FLEX currency=usdc", session, Settings())

    assert reply == "Solde Kraken Futures:\n- flex USDC | balance=100"


def test_balance_command_accepts_currency_aliases(monkeypatch):
    def fake_fetch_account_balances(self):
        return [
            AccountBalance(account="flex", currency="USDC", balance=100),
            AccountBalance(account="flex", currency="ETH", balance=2),
        ]

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_account_balances",
        fake_fetch_account_balances,
    )

    with make_session() as session:
        asset_reply = dispatch_telegram_text("/balance asset=usdc", session, Settings())
        devise_reply = dispatch_telegram_text("/solde devise=ETH", session, Settings())

    assert asset_reply == "Solde Kraken Futures:\n- flex USDC | balance=100"
    assert devise_reply == "Solde Kraken Futures:\n- flex ETH | balance=2"


def test_balance_command_rejects_invalid_filter():
    with make_session() as session:
        reply = dispatch_telegram_text("/balance wallet=USDC", session, Settings())

    assert reply == "Commande refusee: unsupported /balance argument: wallet"


def test_solde_alias_reports_missing_kraken_credentials():
    with make_session() as session:
        reply = dispatch_telegram_text("/solde", session, Settings())

    assert reply == "Commande refusee: Kraken API credentials are required for signed requests."


def test_balance_command_includes_debug_detail_when_feature_flag_enabled(monkeypatch):
    def fake_fetch_account_balances(self):
        raise KrakenAccountError(
            "Kraken balance request was not successful: authenticationError",
            debug_detail=(
                "kraken_result=error\n"
                "kraken_error=authenticationError\n"
                "method=GET\n"
                "url=https://futures.kraken.com/derivatives/api/v3/accounts\n"
                "signed_endpoint_path=/api/v3/accounts\n"
                "nonce_sent=non"
            ),
        )

    monkeypatch.setattr(
        "kraken_telegram_gateway.gateway.kraken.KrakenClient.fetch_account_balances",
        fake_fetch_account_balances,
    )

    with make_session() as session:
        reply = dispatch_telegram_text("/balance", session, Settings(kraken_balance_debug_errors=True))

    assert reply.startswith("Commande refusee: Kraken balance request was not successful: authenticationError")
    assert "Debug Kraken balance:" in reply
    assert "url=https://futures.kraken.com/derivatives/api/v3/accounts" in reply
    assert "signed_endpoint_path=/api/v3/accounts" in reply
    assert "APIKey" not in reply
    assert "Authent" not in reply


def test_orders_command_lists_attached_orders_only():
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 "
            "t1=63000:25% t2=61000:75%",
            session,
            Settings(max_amount_usdc=100),
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        orders_reply = dispatch_telegram_text(f"/orders {trade_id}", session, Settings(max_amount_usdc=100))

    assert orders_reply.startswith(f"Ordres du trade {trade_id}\nSELL PF_XBTUSD | statut=pending_confirmation")
    assert "Trade ID:" not in orders_reply
    assert "- entry: sell limit 65000 | 100 USDC | reduce-only=non | statut=planned" in orders_reply
    assert "- target_exit: buy limit 63000 | 25 USDC | target=25% | reduce-only=oui | statut=planned" in orders_reply
    assert "- target_exit: buy limit 61000 | 75 USDC | target=75% | reduce-only=oui | statut=planned" in orders_reply


def test_orders_command_filters_status_and_role():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=planned role=target_exit",
            session,
            settings,
        )

    assert "- entry:" not in orders_reply
    assert "statut=dry_run_submitted" not in orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=planned" in orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=planned" in orders_reply


def test_orders_command_accepts_case_insensitive_filters():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=DRY_RUN_SUBMITTED role=ENTRY",
            session,
            settings,
        )

    assert "- entry: buy limit 65000 | 100 USDC | reduce-only=non | statut=dry_run_submitted" in orders_reply
    assert "- target_exit:" not in orders_reply


def test_entry_filled_command_marks_targets_ready_for_mobile_visibility():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        filled_reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        ready_orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=ready_to_submit role=target_exit",
            session,
            settings,
        )

    assert "aucun ordre Kraken envoye" in filled_reply
    assert "Statut: entry_filled" in filled_reply
    assert "- entry:" not in ready_orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=ready_to_submit" in ready_orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=ready_to_submit" in ready_orders_reply


def test_entry_filled_command_accepts_hyphen_alias():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/entry-filled {trade_id}", session, settings)

    assert "Entry marquee filled" in reply
    assert "Statut: entry_filled" in reply


def test_entry_filled_command_is_idempotent_for_mobile_retries():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)

        retry_reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        audits = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "entry_filled")
        ).all()

    assert "Aucun changement applique" in retry_reply
    assert "Statut: entry_filled" in retry_reply
    assert len(audits) == 1


def test_submit_targets_command_marks_ready_targets_dry_run_submitted():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)

        submit_reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        submitted_orders_reply = dispatch_telegram_text(
            f"/orders {trade_id} status=dry_run_submitted role=target_exit",
            session,
            settings,
        )
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "targets_submitted")
        ).one()

    assert "2 target(s) reduce-only" in submit_reply
    assert "aucun ordre Kraken envoye" in submit_reply
    assert "Statut: entry_filled" in submit_reply
    assert "- entry:" not in submitted_orders_reply
    assert "- target_exit: sell limit 67000 | 40 USDC | target=40% | reduce-only=oui | statut=dry_run_submitted" in submitted_orders_reply
    assert "- target_exit: sell limit 69000 | 60 USDC | target=60% | reduce-only=oui | statut=dry_run_submitted" in submitted_orders_reply
    assert "Dry-run: 2 target(s)" in audit.message


def test_submit_targets_command_accepts_hyphen_alias():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry-filled {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/submit-targets {trade_id}", session, settings)

    assert "1 target(s) reduce-only" in reply
    assert "aucun ordre Kraken envoye" in reply
    assert "Statut: entry_filled" in reply


def test_submit_targets_command_retry_is_idempotent_for_mobile_operators():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        first_orders = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.TARGET_EXIT)
        ).all()

        retry_reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)
        retry_orders = session.exec(
            select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.TARGET_EXIT)
        ).all()
        audits = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "targets_submitted")
        ).all()

    assert "Targets deja soumises: 2 target(s)" in retry_reply
    assert "Aucun changement applique" in retry_reply
    assert "Statut: entry_filled" in retry_reply
    assert {order.status for order in retry_orders} == {OrderStatus.DRY_RUN_SUBMITTED}
    assert [order.external_order_id for order in retry_orders] == [order.external_order_id for order in first_orders]
    assert len(audits) == 1


def test_submit_targets_command_reports_partial_blocks_for_operator_diagnostics():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        trade = Trade(
            id="trade-mixed-targets",
            pair="PF_XBTUSD",
            side="buy",
            amount_usdc=100,
            entry_type="limit",
            entry_price=65000,
            targets_json="[]",
            leverage=2,
            status="entry_filled",
        )
        good_target = TradeOrder(
            id="target-good",
            trade_id=trade.id,
            role=OrderRole.TARGET_EXIT,
            pair=trade.pair,
            side="sell",
            price=67000,
            amount_usdc=50,
            target_percent=50,
            reduce_only=True,
            status=OrderStatus.READY_TO_SUBMIT,
        )
        blocked_target = TradeOrder(
            id="target-blocked",
            trade_id=trade.id,
            role=OrderRole.TARGET_EXIT,
            pair=trade.pair,
            side="sell",
            price=69000,
            amount_usdc=50,
            target_percent=50,
            reduce_only=False,
            status=OrderStatus.READY_TO_SUBMIT,
        )
        session.add(trade)
        session.add(good_target)
        session.add(blocked_target)
        session.commit()

        reply = dispatch_telegram_text(f"/submit_targets {trade.id}", session, settings)
        refreshed_good_target = session.get(TradeOrder, "target-good")
        refreshed_blocked_target = session.get(TradeOrder, "target-blocked")
        submitted_audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade.id, AuditEvent.event_type == "targets_submitted")
        ).one()
        blocked_audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade.id, AuditEvent.event_type == "targets_blocked")
        ).one()

    assert "Dry-run: 1 target(s) reduce-only" in reply
    assert "1 target(s) bloquees" in reply
    assert "target exit order must be reduce-only" in reply
    assert "Statut: entry_filled" in reply
    assert refreshed_good_target.status == OrderStatus.DRY_RUN_SUBMITTED
    assert refreshed_good_target.external_order_id == "dryrun-target-target-good"
    assert refreshed_blocked_target.status == OrderStatus.READY_TO_SUBMIT
    assert "1 target(s) bloquees" in submitted_audit.message
    assert "target exit order must be reduce-only" in blocked_audit.message


def test_submit_targets_command_rejects_before_entry_fill():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)

    assert "l'entree doit etre marquee filled" in reply
    assert "Statut: dry_run_executed" in reply


def test_submit_targets_command_handles_trade_without_targets():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 stop=63000",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        filled_reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)

        submit_reply = dispatch_telegram_text(f"/submit_targets {trade_id}", session, settings)

    assert "Aucune target definie" in filled_reply
    assert "Aucune target reduce-only prete a soumettre" in submit_reply
    assert "Statut: entry_filled" in submit_reply


def test_cancel_command_retry_is_idempotent_for_mobile_operators():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/cancel {trade_id}", session, settings)

        retry_reply = dispatch_telegram_text(f"/cancel {trade_id}", session, settings)
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()
        audits = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == trade_id, AuditEvent.event_type == "trade_cancelled")
        ).all()

    assert "Trade deja annule" in retry_reply
    assert "Aucun changement applique" in retry_reply
    assert "Statut: cancelled" in retry_reply
    assert {order.status for order in orders} == {OrderStatus.CANCELLED}
    assert len(audits) == 1


def test_cancel_command_cancels_live_submitted_order_before_local_status(monkeypatch):
    cancelled_ids = []

    def fake_cancel_order(self, external_order_id):
        cancelled_ids.append(external_order_id)
        return {"mode": "live", "message": "Live Kraken order cancelled."}

    monkeypatch.setattr("kraken_telegram_gateway.gateway.kraken.KrakenClient.cancel_order", fake_cancel_order)
    settings = Settings(max_amount_usdc=100, dry_run=False, live_trading_enabled=True)
    with make_session() as session:
        trade = Trade(
            id="trade-live",
            pair="PF_XBTUSD",
            side="buy",
            amount_usdc=100,
            entry_type="limit",
            entry_price=65000,
            targets_json="[]",
            leverage=2,
            status="live_submitted",
            dry_run=False,
        )
        order = TradeOrder(
            id="order-live",
            trade_id=trade.id,
            role=OrderRole.ENTRY,
            pair=trade.pair,
            side=trade.side,
            price=trade.entry_price,
            amount_usdc=trade.amount_usdc,
            status=OrderStatus.LIVE_SUBMITTED,
            external_order_id="OID-123",
        )
        session.add(trade)
        session.add(order)
        session.commit()

        reply = dispatch_telegram_text("/cancel trade-live", session, settings)
        refreshed_order = session.get(TradeOrder, "order-live")

    assert cancelled_ids == ["OID-123"]
    assert "Trade annule" in reply
    assert "Statut: cancelled" in reply
    assert refreshed_order.status == OrderStatus.CANCELLED


def test_cancel_command_does_not_mark_live_trade_cancelled_when_kraken_cancel_fails(monkeypatch):
    def fake_cancel_order(self, external_order_id):
        return {
            "mode": "blocked",
            "message": "Live Kraken cancellation failed: cancelStatus.status=notfound",
        }

    monkeypatch.setattr("kraken_telegram_gateway.gateway.kraken.KrakenClient.cancel_order", fake_cancel_order)
    settings = Settings(max_amount_usdc=100, dry_run=False, live_trading_enabled=True)
    with make_session() as session:
        trade = Trade(
            id="trade-live",
            pair="PF_XBTUSD",
            side="buy",
            amount_usdc=100,
            entry_type="limit",
            entry_price=65000,
            targets_json="[]",
            leverage=2,
            status="live_submitted",
            dry_run=False,
        )
        order = TradeOrder(
            id="order-live",
            trade_id=trade.id,
            role=OrderRole.ENTRY,
            pair=trade.pair,
            side=trade.side,
            price=trade.entry_price,
            amount_usdc=trade.amount_usdc,
            status=OrderStatus.LIVE_SUBMITTED,
            external_order_id="OID-123",
        )
        session.add(trade)
        session.add(order)
        session.commit()

        reply = dispatch_telegram_text("/cancel trade-live", session, settings)
        refreshed_trade = session.get(Trade, "trade-live")
        refreshed_order = session.get(TradeOrder, "order-live")
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.trade_id == "trade-live", AuditEvent.event_type == "trade_cancel_blocked")
        ).one()

    assert "Live Kraken cancellation failed" in reply
    assert "Statut: live_submitted" in reply
    assert refreshed_trade.status == "live_submitted"
    assert refreshed_order.status == OrderStatus.LIVE_SUBMITTED
    assert "cancelStatus.status=notfound" in audit.message


def test_entry_filled_command_rejects_unconfirmed_trade():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=sell amount_usdc=100 entry=limit:65000 t1=63000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        reply = dispatch_telegram_text(f"/entry_filled {trade_id}", session, settings)
        orders = session.exec(select(TradeOrder).where(TradeOrder.trade_id == trade_id)).all()

    assert "doit d'abord etre confirme" in reply
    assert "Statut: pending_confirmation" in reply
    assert {order.status for order in orders} == {OrderStatus.PLANNED}


def test_orders_command_rejects_invalid_filter():
    with make_session() as session:
        reply = dispatch_telegram_text("/orders trade-1 status=bad", session, Settings(max_amount_usdc=100))

    assert "'bad' is not a valid OrderStatus" in reply


def test_orders_command_handles_missing_trade():
    with make_session() as session:
        reply = dispatch_telegram_text("/orders missing", session, Settings(max_amount_usdc=100))

    assert reply == "Trade introuvable."


def test_trades_command_lists_recent_trades_for_mobile_visibility():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        first_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        second_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100%",
            session,
            settings,
        )
        first_trade_id = next(
            line.split(": ", 1)[1] for line in first_preview.splitlines() if line.startswith("Trade ID:")
        )
        second_trade_id = next(
            line.split(": ", 1)[1] for line in second_preview.splitlines() if line.startswith("Trade ID:")
        )

        reply = dispatch_telegram_text("/trades limit=2", session, settings)

    assert reply.startswith("Trades recents 1-2/2:")
    assert f"- {second_trade_id} | pending_confirmation | SELL PF_ETHUSD | 50 USDC | entry=3500" in reply
    assert f"- {first_trade_id} | pending_confirmation | BUY PF_XBTUSD | 100 USDC | entry=65000" in reply
    assert reply.index(second_trade_id) < reply.index(first_trade_id)


def test_trades_command_filters_status_pair_and_side():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        xbt_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        eth_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100%",
            session,
            settings,
        )
        xbt_trade_id = next(
            line.split(": ", 1)[1] for line in xbt_preview.splitlines() if line.startswith("Trade ID:")
        )
        eth_trade_id = next(
            line.split(": ", 1)[1] for line in eth_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/cancel {xbt_trade_id}", session, settings)

        reply = dispatch_telegram_text("/trades status=CANCELLED pair=pf_xbtusd side=BUY", session, settings)

    assert reply.startswith("Trades recents 1-1/1:")
    assert xbt_trade_id in reply
    assert eth_trade_id not in reply
    assert "cancelled | BUY PF_XBTUSD" in reply


def test_trades_command_rejects_invalid_side():
    with make_session() as session:
        reply = dispatch_telegram_text("/trades side=long", session, Settings(max_amount_usdc=100))

    assert reply == "Commande refusee: /trades side must be buy or sell"


def test_trades_command_rejects_invalid_limit():
    with make_session() as session:
        reply = dispatch_telegram_text("/trades limit=25", session, Settings(max_amount_usdc=100))

    assert reply == "Commande refusee: /trades limit must be between 1 and 10"


def test_audit_command_lists_and_filters_safety_events():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/confirm {trade_id}", session, settings)

        reply = dispatch_telegram_text(f"/audit {trade_id} event_type=trade_rejected", session, settings)

    assert reply.startswith("Audit 1-1/1:")
    assert f"- trade_rejected | trade={trade_id}" in reply
    assert "stop loss requis" in reply
    assert "trade_preview" not in reply


def test_audit_command_accepts_short_event_type_filter_aliases():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        rejected_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        rejected_trade_id = next(
            line.split(": ", 1)[1] for line in rejected_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/confirm {rejected_trade_id}", session, settings)
        cancelled_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100% stop=3600",
            session,
            settings,
        )
        cancelled_trade_id = next(
            line.split(": ", 1)[1] for line in cancelled_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/cancel {cancelled_trade_id}", session, settings)

        type_reply = dispatch_telegram_text("/audit type=TRADE_REJECTED", session, settings)
        event_reply = dispatch_telegram_text("/audit event=TRADE_CANCELLED", session, settings)

    assert type_reply.startswith("Audit 1-1/1:")
    assert "trade_rejected" in type_reply
    assert "trade_cancelled" not in type_reply
    assert event_reply.startswith("Audit 1-1/1:")
    assert "trade_cancelled" in event_reply
    assert "trade_rejected" not in event_reply


def test_audit_command_rejects_invalid_limit():
    with make_session() as session:
        reply = dispatch_telegram_text("/audit limit=25", session, Settings(max_amount_usdc=100))

    assert reply == "Commande refusee: /audit limit must be between 1 and 10"


def test_audit_types_command_lists_event_type_counts():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with make_session() as session:
        rejected_preview = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        rejected_trade_id = next(
            line.split(": ", 1)[1] for line in rejected_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/confirm {rejected_trade_id}", session, settings)

        cancelled_preview = dispatch_telegram_text(
            "/trade pair=PF_ETHUSD side=sell amount_usdc=50 entry=limit:3500 t1=3300:100% stop=3600",
            session,
            settings,
        )
        cancelled_trade_id = next(
            line.split(": ", 1)[1] for line in cancelled_preview.splitlines() if line.startswith("Trade ID:")
        )
        dispatch_telegram_text(f"/cancel {cancelled_trade_id}", session, settings)

        reply = dispatch_telegram_text("/audit_types", session, settings)

    assert reply.startswith("Types audit (3):")
    assert "- trade_preview: 2" in reply
    assert "- trade_cancelled: 1" in reply
    assert "- trade_rejected: 1" in reply


def test_audit_types_command_accepts_hyphen_alias():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))
        dispatch_telegram_text(f"/cancel {trade_id}", session, settings)

        reply = dispatch_telegram_text("/audit-types", session, settings)

    assert reply.startswith("Types audit (2):")
    assert "- trade_cancelled: 1" in reply
    assert "- trade_preview: 1" in reply


def test_allowed_user_ids_are_enforced():
    update = {
        "message": {
            "chat": {"id": 456},
            "from": {"id": 123},
            "text": "/help",
        }
    }

    with make_session() as session:
        reply = handle_telegram_update(
            update,
            session,
            Settings(telegram_allowed_user_ids="999"),
        )

    assert reply == "Utilisateur non autorise."


def test_pause_blocks_new_trades_and_resume_allows_them():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        pause_reply = dispatch_telegram_text("/pause", session, settings)
        blocked_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        status_reply = dispatch_telegram_text("/status", session, settings)
        resume_reply = dispatch_telegram_text("/resume", session, settings)
        allowed_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )

    assert "pause" in pause_reply
    assert "trading is paused" in blocked_reply
    assert status_reply == "Trading: pause"
    assert "relance" in resume_reply
    assert "Preview creee" in allowed_reply


def test_pause_blocks_confirm_but_allows_cancel():
    settings = Settings(max_amount_usdc=100)
    with make_session() as session:
        preview_reply = dispatch_telegram_text(
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
            session,
            settings,
        )
        trade_id = next(line.split(": ", 1)[1] for line in preview_reply.splitlines() if line.startswith("Trade ID:"))

        dispatch_telegram_text("/pause", session, settings)
        confirm_reply = dispatch_telegram_text(f"/confirm {trade_id}", session, settings)
        cancel_reply = dispatch_telegram_text(f"/cancel {trade_id}", session, settings)

    assert "Trading en pause" in confirm_reply
    assert "Trade annule" in cancel_reply


def test_telegram_update_id_is_stored_and_reused():
    update = {
        "update_id": 42,
        "message": {
            "chat": {"id": 456},
            "from": {"id": 123},
            "text": "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
        },
    }

    with make_session() as session:
        first_reply = handle_telegram_update(update, session, Settings(max_amount_usdc=100))
        second_reply = handle_telegram_update(update, session, Settings(max_amount_usdc=100))
        trades = session.exec(select(Trade)).all()
        processed = session.get(ProcessedTelegramUpdate, 42)

    assert first_reply == second_reply
    assert len(trades) == 1
    assert processed is not None
    assert isinstance(first_reply, list)
    assert len(first_reply) == 2
    assert processed.reply_text.startswith("[")
