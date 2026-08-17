import json
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from kraken_telegram_gateway.gateway.config import Settings
from kraken_telegram_gateway.gateway.kraken import AccountBalance, KrakenClient, KrakenFill
from kraken_telegram_gateway.gateway.market_data import collect_kraken_futures_snapshots
from kraken_telegram_gateway.gateway.models import (
    AuditEvent,
    BotState,
    OrderRole,
    OrderStatus,
    ScalpSession,
    ScalpSessionStatus,
    ScalpSignal,
    ScalpTrade,
    ScalpTradeStatus,
    Trade,
    TradeOrder,
    TradeStatus,
    utc_now,
)
from kraken_telegram_gateway.gateway.parser import parse_scalp_start_command, parse_trade_command
from kraken_telegram_gateway.gateway.risk import validate_risk
from kraken_telegram_gateway.gateway.scalping import MarketSnapshot, evaluate_scalp_signal
from kraken_telegram_gateway.gateway.schemas import (
    AuditEventList,
    AuditEventTypeList,
    AuditEventTypeSummary,
    ConfirmResult,
    ScalpIntent,
    ScalpFillSyncResult,
    ScalpSchedulerResult,
    ScalpSessionDetail,
    ScalpSessionReport,
    ScalpSessionResult,
    TradeDetail,
    TradeList,
    TradePreview,
)


def create_trade_preview(text: str, session: Session, settings: Settings) -> TradePreview:
    if is_trading_paused(session):
        raise ValueError("trading is paused")
    intent = parse_trade_command(text)
    warning = validate_risk(intent, settings)
    trade = Trade(
        pair=intent.pair,
        side=intent.side,
        amount_usdc=intent.amount_usdc,
        entry_type=intent.entry_type,
        entry_price=intent.entry_price,
        targets_json=json.dumps([target.model_dump() for target in intent.targets]),
        stop_price=intent.stop_price,
        leverage=intent.leverage,
        dry_run=settings.dry_run,
        warning=warning,
    )
    session.add(trade)
    for order in build_planned_orders(trade):
        session.add(order)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_preview", message="Trade parsed and validated."))
    session.commit()
    session.refresh(trade)
    return TradePreview(trade_id=trade.id, summary=format_trade_summary(trade), warning=warning, dry_run=trade.dry_run)


def start_scalp_session(text: str, session: Session, settings: Settings | None = None) -> ScalpSessionResult:
    if is_trading_paused(session):
        return ScalpSessionResult(session_id="", status="paused", message="Trading en pause.")

    intent = parse_scalp_start_command(text)
    if settings is not None:
        _validate_scalp_intent(intent, settings)
    active_status = ScalpSessionStatus.LIVE_ACTIVE if intent.mode == "live" else ScalpSessionStatus.PAPER_ACTIVE
    active = session.exec(
        select(ScalpSession).where(
            ScalpSession.pair == intent.pair,
            ScalpSession.status == active_status,
        )
    ).first()
    if active is not None:
        return ScalpSessionResult(
            session_id=active.id,
            status=active.status,
            message=f"Session scalp deja active pour {active.pair}.",
        )

    scalp_session = ScalpSession(
        pair=intent.pair,
        side_mode=intent.side_mode,
        amount_usdc=intent.amount_usdc,
        leverage=intent.leverage,
        duration_seconds=intent.duration_seconds,
        max_hold_seconds=intent.max_hold_seconds,
        max_losses=intent.max_losses,
        min_net_pnl=intent.min_net_pnl,
        mode=intent.mode,
        status=active_status,
    )
    session.add(scalp_session)
    session.commit()
    session.refresh(scalp_session)
    session.add(
        AuditEvent(
            event_type="scalp_session_started",
            message=(
                f"Scalp {scalp_session.mode} session {scalp_session.id} started for {scalp_session.pair}; "
                f"duration={scalp_session.duration_seconds}s max_hold={scalp_session.max_hold_seconds}s."
            ),
        )
    )
    session.commit()
    if scalp_session.mode == "live":
        message = (
            "Session scalp live creee. Les signaux peuvent envoyer des ordres limit Kraken "
            "si le scheduler/tick market-data tourne."
        )
    else:
        message = "Session scalp paper creee. Aucun ordre Kraken ne sera envoye."
    return ScalpSessionResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        message=message,
    )


def stop_scalp_session(session_id: str, session: Session, *, reason: str = "manual_stop") -> ScalpSessionResult:
    scalp_session = session.get(ScalpSession, session_id)
    if scalp_session is None:
        return ScalpSessionResult(session_id=session_id, status="not_found", message="Session scalp introuvable.")
    if not _is_active_scalp_status(scalp_session.status):
        return ScalpSessionResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            message=f"Session scalp deja arretee: {scalp_session.stop_reason or scalp_session.status}.",
        )

    scalp_session.status = ScalpSessionStatus.STOPPED
    scalp_session.stop_reason = reason
    scalp_session.stopped_at = utc_now()
    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.add(
        AuditEvent(
            event_type="scalp_session_stopped",
            message=f"Scalp {scalp_session.mode} session {scalp_session.id} stopped: {reason}.",
        )
    )
    session.commit()
    return ScalpSessionResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        message="Session scalp arretee.",
    )


def get_scalp_session_detail(session_id: str, session: Session) -> ScalpSessionDetail | None:
    scalp_session = session.get(ScalpSession, session_id)
    if scalp_session is None:
        return None
    trades = session.exec(
        select(ScalpTrade)
        .where(ScalpTrade.session_id == session_id)
        .order_by(ScalpTrade.created_at.asc(), ScalpTrade.id.asc())
    ).all()
    signals = session.exec(
        select(ScalpSignal)
        .where(ScalpSignal.session_id == session_id)
        .order_by(ScalpSignal.created_at.desc(), ScalpSignal.id.desc())
        .limit(20)
    ).all()
    return ScalpSessionDetail(session=scalp_session, trades=list(trades), signals=list(signals))


def get_scalp_session_report(session_id: str, session: Session) -> ScalpSessionReport | None:
    detail = get_scalp_session_detail(session_id, session)
    if detail is None:
        return None
    return build_scalp_session_report(detail)


def run_scalp_paper_snapshots(
    session_id: str,
    snapshots: list[MarketSnapshot],
    session: Session,
) -> ScalpSessionResult:
    scalp_session = session.get(ScalpSession, session_id)
    if scalp_session is None:
        return ScalpSessionResult(session_id=session_id, status="not_found", message="Session scalp introuvable.")
    if scalp_session.status != ScalpSessionStatus.PAPER_ACTIVE:
        return ScalpSessionResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            message="Session scalp inactive; aucun snapshot traite.",
        )
    if not snapshots:
        return ScalpSessionResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            message="Aucun snapshot market-data a traiter.",
        )

    opened = 0
    closed = 0
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        if _scalp_session_elapsed(scalp_session, snapshot):
            _complete_scalp_session(scalp_session, "duration_elapsed", session)
            break

        open_trade = _get_open_scalp_trade(scalp_session.id, session)
        if open_trade is not None:
            if _maybe_close_scalp_trade(scalp_session, open_trade, snapshot, session):
                closed += 1
                if _scalp_loss_count(scalp_session.id, session) >= scalp_session.max_losses:
                    _complete_scalp_session(scalp_session, "max_losses", session)
                    break
            continue

        decision = evaluate_scalp_signal(snapshot, side_mode=scalp_session.side_mode)
        signal = ScalpSignal(
            session_id=scalp_session.id,
            signal_kind="book_volume_v1",
            score=decision.score,
            spread=snapshot.spread,
            book_imbalance=snapshot.book_imbalance,
            volume_ratio=snapshot.volume_ratio,
            reason=decision.reason,
            created_at=snapshot.timestamp,
        )
        session.add(signal)
        if decision.side is None:
            continue

        entry_price = snapshot.ask if decision.side == "buy" else snapshot.bid
        scalp_trade = ScalpTrade(
            session_id=scalp_session.id,
            pair=scalp_session.pair,
            side=decision.side,
            amount_usdc=scalp_session.amount_usdc,
            leverage=scalp_session.leverage,
            entry_price=entry_price,
            opened_at=snapshot.timestamp,
            created_at=snapshot.timestamp,
            updated_at=snapshot.timestamp,
        )
        session.add(scalp_trade)
        session.flush()
        signal.scalp_trade_id = scalp_trade.id
        session.add(signal)
        opened += 1

    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.commit()
    return ScalpSessionResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        message=f"Paper runner: {opened} trade(s) ouverts, {closed} trade(s) fermes.",
    )


def run_scalp_live_snapshots(
    session_id: str,
    snapshots: list[MarketSnapshot],
    session: Session,
    settings: Settings,
) -> ScalpSessionResult:
    scalp_session = session.get(ScalpSession, session_id)
    if scalp_session is None:
        return ScalpSessionResult(session_id=session_id, status="not_found", message="Session scalp introuvable.")
    if scalp_session.status != ScalpSessionStatus.LIVE_ACTIVE:
        return ScalpSessionResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            message="Session scalp live inactive; aucun snapshot traite.",
        )
    if not snapshots:
        return ScalpSessionResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            message="Aucun snapshot market-data a traiter.",
        )

    if not settings.scalp_live_enabled:
        return _block_live_scalp_session(scalp_session, "SCALP_LIVE_ENABLED=false.", session)
    if not settings.can_live_trade:
        return _block_live_scalp_session(
            scalp_session,
            "Kraken live gates fermes: LIVE_TRADING_ENABLED=true, DRY_RUN=false et credentials requis.",
            session,
        )
    if scalp_session.amount_usdc > settings.scalp_live_max_amount_usdc:
        return _block_live_scalp_session(
            scalp_session,
            f"Montant scalp live {scalp_session.amount_usdc:g} > limite {settings.scalp_live_max_amount_usdc:g} USDC.",
            session,
        )

    submitted = 0
    blocked = 0
    client = KrakenClient(settings)
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        if _scalp_session_elapsed(scalp_session, snapshot):
            _complete_scalp_session(scalp_session, "duration_elapsed", session)
            break
        if _get_open_scalp_trade(scalp_session.id, session) is not None:
            continue

        decision = evaluate_scalp_signal(snapshot, side_mode=scalp_session.side_mode)
        signal = ScalpSignal(
            session_id=scalp_session.id,
            signal_kind="book_volume_v1",
            score=decision.score,
            spread=snapshot.spread,
            book_imbalance=snapshot.book_imbalance,
            volume_ratio=snapshot.volume_ratio,
            reason=decision.reason,
            created_at=snapshot.timestamp,
        )
        session.add(signal)
        if decision.side is None:
            continue

        entry_price = snapshot.ask if decision.side == "buy" else snapshot.bid
        result = client.submit_scalp_entry_order(scalp_session, decision.side, entry_price)
        scalp_trade = ScalpTrade(
            session_id=scalp_session.id,
            pair=scalp_session.pair,
            side=decision.side,
            amount_usdc=scalp_session.amount_usdc,
            leverage=scalp_session.leverage,
            entry_price=entry_price,
            status=ScalpTradeStatus.LIVE_SUBMITTED if result["mode"] == "live" else ScalpTradeStatus.LIVE_BLOCKED,
            external_order_id=result.get("external_order_id"),
            close_reason=None if result["mode"] == "live" else result["message"],
            opened_at=snapshot.timestamp,
            created_at=snapshot.timestamp,
            updated_at=snapshot.timestamp,
        )
        session.add(scalp_trade)
        session.flush()
        signal.scalp_trade_id = scalp_trade.id
        session.add(signal)
        event_type = "scalp_live_entry_submitted" if result["mode"] == "live" else "scalp_live_entry_blocked"
        session.add(AuditEvent(event_type=event_type, message=result["message"]))
        if result["mode"] == "live":
            submitted += 1
        else:
            blocked += 1

    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.commit()
    return ScalpSessionResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        message=f"Live runner: {submitted} ordre(s) envoyes, {blocked} ordre(s) bloques.",
    )


ScalpSnapshotProvider = Callable[[ScalpSession], list[MarketSnapshot]]


def run_active_scalp_paper_sessions(
    session: Session,
    snapshot_provider: ScalpSnapshotProvider,
    settings: Settings | None = None,
) -> ScalpSchedulerResult:
    active_sessions = session.exec(
        select(ScalpSession)
        .where(ScalpSession.status.in_([ScalpSessionStatus.PAPER_ACTIVE, ScalpSessionStatus.LIVE_ACTIVE]))
        .order_by(ScalpSession.started_at.asc(), ScalpSession.id.asc())
    ).all()

    processed = 0
    skipped = 0
    messages: list[str] = []
    for scalp_session in active_sessions:
        snapshots = snapshot_provider(scalp_session)
        if not snapshots:
            skipped += 1
            messages.append(f"{scalp_session.id}: aucun snapshot disponible.")
            continue
        if scalp_session.mode == "live":
            if settings is None:
                skipped += 1
                messages.append(f"{scalp_session.id}: settings live manquants.")
                continue
            result = run_scalp_live_snapshots(scalp_session.id, snapshots, session, settings)
        else:
            result = run_scalp_paper_snapshots(scalp_session.id, snapshots, session)
        processed += 1
        messages.append(f"{scalp_session.id}: {result.message}")

    return ScalpSchedulerResult(
        scanned=len(active_sessions),
        processed=processed,
        skipped=skipped,
        messages=messages,
    )


def run_active_scalp_paper_sessions_from_kraken(
    session: Session,
    *,
    snapshots_per_session: int = 1,
    timeout_seconds: float = 10,
    settings: Settings | None = None,
) -> ScalpSchedulerResult:
    def snapshot_provider(scalp_session: ScalpSession) -> list[MarketSnapshot]:
        return asyncio_run_collect_snapshots(
            scalp_session.pair,
            limit=snapshots_per_session,
            timeout_seconds=timeout_seconds,
        )

    return run_active_scalp_paper_sessions(session, snapshot_provider, settings=settings)


def sync_scalp_entry_fills(session_id: str, session: Session, settings: Settings) -> ScalpFillSyncResult:
    scalp_session = session.get(ScalpSession, session_id)
    if scalp_session is None:
        return ScalpFillSyncResult(
            session_id=session_id,
            status="not_found",
            scanned=0,
            filled=0,
            skipped=0,
            message="Session scalp introuvable.",
        )
    if scalp_session.mode != "live":
        return ScalpFillSyncResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            scanned=0,
            filled=0,
            skipped=0,
            message="Sync fills ignoree: session scalp non-live.",
        )

    live_trades = session.exec(
        select(ScalpTrade).where(
            ScalpTrade.session_id == scalp_session.id,
            ScalpTrade.status == ScalpTradeStatus.LIVE_SUBMITTED,
            ScalpTrade.external_order_id.is_not(None),
        )
    ).all()
    if not live_trades:
        return ScalpFillSyncResult(
            session_id=scalp_session.id,
            status=scalp_session.status,
            scanned=0,
            filled=0,
            skipped=0,
            message="Aucun trade scalp live soumis a synchroniser.",
        )

    fills_by_order_id = {
        fill.order_id: fill
        for fill in KrakenClient(settings).fetch_recent_fills()
        if fill.order_id
    }
    filled = 0
    skipped = 0
    for trade in live_trades:
        if trade.external_order_id is None:
            skipped += 1
            continue
        fill = fills_by_order_id.get(trade.external_order_id)
        if fill is None or not _kraken_fill_matches_scalp_trade(fill, trade):
            skipped += 1
            continue

        trade.status = ScalpTradeStatus.LIVE_ENTRY_FILLED
        trade.entry_price = float(fill.price)
        trade.opened_at = _parse_kraken_event_time(fill.fill_time) or trade.opened_at
        trade.updated_at = utc_now()
        session.add(trade)
        session.add(
            AuditEvent(
                event_type="scalp_live_entry_filled",
                message=(
                    f"Scalp live trade {trade.id} entry fill detected for order "
                    f"{trade.external_order_id} at {fill.price:g}."
                ),
            )
        )
        filled += 1

    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.commit()
    return ScalpFillSyncResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        scanned=len(live_trades),
        filled=filled,
        skipped=skipped,
        message=f"Sync fills: {filled} entree(s) scalp live marquee(s) filled, {skipped} ignoree(s).",
    )


def asyncio_run_collect_snapshots(product_id: str, *, limit: int, timeout_seconds: float) -> list[MarketSnapshot]:
    import asyncio

    try:
        return asyncio.run(
            collect_kraken_futures_snapshots(
                product_id,
                limit=limit,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception:
        return []


def confirm_trade(trade_id: str, session: Session, settings: Settings) -> ConfirmResult:
    if is_trading_paused(session):
        return ConfirmResult(trade_id=trade_id, status="paused", message="Trading en pause.")

    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status != TradeStatus.PENDING_CONFIRMATION:
        return ConfirmResult(trade_id=trade.id, status=trade.status, message="Trade deja traite.")
    if settings.require_stop_loss_for_confirmation and trade.stop_price is None:
        message = "Confirmation refusee: stop loss requis par la configuration de securite."
        trade.status = TradeStatus.REJECTED
        trade.updated_at = utc_now()
        session.add(trade)
        session.add(AuditEvent(trade_id=trade.id, event_type="trade_rejected", message=message))
        session.commit()
        return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)

    result = KrakenClient(settings).submit_entry_order(trade)
    if result["mode"] == "dry_run":
        trade.status = TradeStatus.DRY_RUN_EXECUTED
    elif result["mode"] == "blocked":
        trade.status = TradeStatus.REJECTED
    else:
        trade.status = TradeStatus.LIVE_SUBMITTED
    trade.updated_at = utc_now()
    session.add(trade)
    if result["mode"] != "blocked":
        mark_entry_orders_submitted(trade.id, result, session)
    event_type = "trade_rejected" if result["mode"] == "blocked" else "trade_confirmed"
    session.add(AuditEvent(trade_id=trade.id, event_type=event_type, message=result["message"]))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=result["message"])


def cancel_trade(trade_id: str, session: Session, settings: Settings) -> ConfirmResult:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status == TradeStatus.CANCELLED:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Trade deja annule. Aucun changement applique.",
        )
    live_orders = list_live_cancellable_orders(trade.id, session)
    if live_orders:
        client = KrakenClient(settings)
        for order in live_orders:
            if order.external_order_id is None:
                continue
            result = client.cancel_order(order.external_order_id)
            if result["mode"] == "blocked":
                session.add(
                    AuditEvent(
                        trade_id=trade.id,
                        event_type="trade_cancel_blocked",
                        message=result["message"],
                    )
                )
                session.commit()
                return ConfirmResult(trade_id=trade.id, status=trade.status, message=result["message"])
            order.status = OrderStatus.CANCELLED
            order.updated_at = utc_now()
            session.add(order)
    trade.status = TradeStatus.CANCELLED
    trade.updated_at = utc_now()
    session.add(trade)
    cancel_planned_orders(trade.id, session)
    session.add(AuditEvent(trade_id=trade.id, event_type="trade_cancelled", message="Trade cancelled by command."))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message="Trade annule.")


def mark_entry_filled(trade_id: str, session: Session) -> ConfirmResult:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status not in {
        TradeStatus.DRY_RUN_EXECUTED,
        TradeStatus.LIVE_SUBMITTED,
        TradeStatus.ENTRY_FILLED,
    }:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Entry fill refuse: le trade doit d'abord etre confirme.",
        )

    entry_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade.id, TradeOrder.role == OrderRole.ENTRY)
    ).all()
    target_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade.id, TradeOrder.role == OrderRole.TARGET_EXIT)
    ).all()

    entry_needs_update = any(order.status != OrderStatus.FILLED for order in entry_orders)
    planned_targets = [order for order in target_orders if order.status == OrderStatus.PLANNED]
    if trade.status == TradeStatus.ENTRY_FILLED and not entry_needs_update and not planned_targets:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Entry deja marquee filled. Aucun changement applique.",
        )

    for order in entry_orders:
        if order.status != OrderStatus.FILLED:
            order.status = OrderStatus.FILLED
            order.updated_at = utc_now()
            session.add(order)
    for order in planned_targets:
        order.status = OrderStatus.READY_TO_SUBMIT
        order.updated_at = utc_now()
        session.add(order)

    trade.status = TradeStatus.ENTRY_FILLED
    trade.updated_at = utc_now()
    session.add(trade)
    message = (
        "Entry marquee filled. Targets reduce-only pretes a soumettre; aucun ordre Kraken envoye."
        if planned_targets
        else "Entry marquee filled. Aucune target definie; aucun ordre Kraken envoye."
    )
    session.add(AuditEvent(trade_id=trade.id, event_type="entry_filled", message=message))
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)


def submit_ready_targets(trade_id: str, session: Session, settings: Settings) -> ConfirmResult:
    if is_trading_paused(session):
        return ConfirmResult(trade_id=trade_id, status="paused", message="Trading en pause.")

    trade = session.get(Trade, trade_id)
    if trade is None:
        return ConfirmResult(trade_id=trade_id, status="not_found", message="Trade introuvable.")
    if trade.status != TradeStatus.ENTRY_FILLED:
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Soumission targets refusee: l'entree doit etre marquee filled.",
        )

    ready_targets = session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade.id,
            TradeOrder.role == OrderRole.TARGET_EXIT,
            TradeOrder.status == OrderStatus.READY_TO_SUBMIT,
        )
    ).all()
    if not ready_targets:
        submitted_targets = session.exec(
            select(TradeOrder).where(
                TradeOrder.trade_id == trade.id,
                TradeOrder.role == OrderRole.TARGET_EXIT,
                TradeOrder.status.in_([OrderStatus.DRY_RUN_SUBMITTED, OrderStatus.LIVE_SUBMITTED]),
            )
        ).all()
        if submitted_targets:
            return ConfirmResult(
                trade_id=trade.id,
                status=trade.status,
                message=(
                    f"Targets deja soumises: {len(submitted_targets)} target(s) reduce-only deja enregistrees. "
                    "Aucun changement applique."
                ),
            )
        return ConfirmResult(
            trade_id=trade.id,
            status=trade.status,
            message="Aucune target reduce-only prete a soumettre.",
        )

    client = KrakenClient(settings)
    submitted_count = 0
    live_count = 0
    blocked_messages = []
    for order in sorted(ready_targets, key=order_sort_key):
        result = client.submit_target_order(trade, order)
        if result["mode"] == "blocked":
            blocked_messages.append(result["message"])
            continue
        order.status = OrderStatus.DRY_RUN_SUBMITTED if result["mode"] == "dry_run" else OrderStatus.LIVE_SUBMITTED
        order.external_order_id = result.get("external_order_id")
        order.updated_at = utc_now()
        session.add(order)
        submitted_count += 1
        if result["mode"] == "live":
            live_count += 1

    if submitted_count:
        if live_count:
            message = f"Live: {submitted_count} target(s) reduce-only envoyees a Kraken."
        else:
            message = f"Dry-run: {submitted_count} target(s) reduce-only marquees soumises; aucun ordre Kraken envoye."
        if blocked_messages:
            blocked_summary = (
                f"{len(blocked_messages)} target(s) bloquees; premiere erreur: {blocked_messages[0]}"
            )
            message = f"{message} {blocked_summary}"
            session.add(AuditEvent(trade_id=trade.id, event_type="targets_blocked", message=blocked_summary))
        session.add(AuditEvent(trade_id=trade.id, event_type="targets_submitted", message=message))
    else:
        message = blocked_messages[0] if blocked_messages else "Aucune target reduce-only soumise."
        session.add(AuditEvent(trade_id=trade.id, event_type="targets_blocked", message=message))
    trade.updated_at = utc_now()
    session.add(trade)
    session.commit()
    return ConfirmResult(trade_id=trade.id, status=trade.status, message=message)


def get_trade_detail(trade_id: str, session: Session) -> TradeDetail | None:
    trade = session.get(Trade, trade_id)
    if trade is None:
        return None
    return TradeDetail(trade=trade, orders=list_trade_orders(trade.id, session))


def get_trade_orders(
    trade_id: str,
    session: Session,
    *,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
) -> list[TradeOrder] | None:
    if session.get(Trade, trade_id) is None:
        return None
    return list_trade_orders(trade_id, session, status=status, role=role)


def list_trades(
    session: Session,
    *,
    status: TradeStatus | None = None,
    pair: str | None = None,
    side: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> TradeList:
    filters = []
    if status is not None:
        filters.append(Trade.status == status)
    if pair:
        filters.append(Trade.pair == pair.upper())
    if side:
        normalized_side = side.lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("/trades side must be buy or sell")
        filters.append(Trade.side == normalized_side)

    count_statement = select(func.count()).select_from(Trade)
    if filters:
        count_statement = count_statement.where(*filters)
    total = session.exec(count_statement).one()

    trade_statement = (
        select(Trade)
        .where(*filters)
        .order_by(Trade.created_at.desc(), Trade.id.desc())
        .offset(offset)
        .limit(limit)
    )
    trades = session.exec(trade_statement).all()
    return TradeList(items=list(trades), total=total, limit=limit, offset=offset)


def list_audit_events(
    session: Session,
    *,
    trade_id: str | None = None,
    event_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AuditEventList:
    filters = []
    if trade_id:
        filters.append(AuditEvent.trade_id == trade_id)
    if event_type:
        filters.append(AuditEvent.event_type == event_type)

    count_statement = select(func.count()).select_from(AuditEvent)
    if filters:
        count_statement = count_statement.where(*filters)
    total = session.exec(count_statement).one()

    event_statement = (
        select(AuditEvent)
        .where(*filters)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    events = session.exec(event_statement).all()
    return AuditEventList(items=list(events), total=total, limit=limit, offset=offset)


def list_audit_event_types(session: Session) -> AuditEventTypeList:
    count_expr = func.count(AuditEvent.id)
    latest_expr = func.max(AuditEvent.created_at)
    statement = (
        select(AuditEvent.event_type, count_expr, latest_expr)
        .group_by(AuditEvent.event_type)
        .order_by(count_expr.desc(), AuditEvent.event_type.asc())
    )
    rows = session.exec(statement).all()
    items = [
        AuditEventTypeSummary(event_type=event_type, count=count, latest_at=latest_at)
        for event_type, count, latest_at in rows
    ]
    return AuditEventTypeList(items=items, total=len(items))


def get_account_balances(
    settings: Settings,
    *,
    account: str | None = None,
    currency: str | None = None,
) -> list[AccountBalance]:
    balances = [
        balance
        for balance in KrakenClient(settings).fetch_account_balances()
        if has_visible_account_balance(balance)
    ]
    if account:
        wanted_account = account.lower()
        balances = [balance for balance in balances if balance.account.lower() == wanted_account]
    if currency:
        wanted_currency = currency.upper()
        balances = [balance for balance in balances if balance.currency.upper() == wanted_currency]
    return balances


def has_visible_account_balance(balance: AccountBalance) -> bool:
    return any(
        value is not None and value != 0
        for value in (balance.balance, balance.equity, balance.available, balance.margin)
    )


def format_trade_summary(trade: Trade) -> str:
    targets = ", ".join(
        f"{target['price']:g}:{target['percent']:g}%"
        for target in json.loads(trade.targets_json)
    ) or "aucune"
    stop = f"{trade.stop_price:g}" if trade.stop_price else "aucun"
    return (
        f"{trade.side.upper()} {trade.pair} | montant={trade.amount_usdc:g} USDC | "
        f"entry={trade.entry_type}:{trade.entry_price:g} | targets={targets} | "
        f"stop={stop} | leverage={trade.leverage}x"
    )


def format_trade_status(trade: Trade, orders: list[TradeOrder]) -> str:
    lines = [
        f"Trade ID: {trade.id}",
        f"Statut: {trade.status}",
        format_trade_summary(trade),
    ]
    if orders:
        lines.append("Ordres planifies:")
        lines.extend(format_trade_order(order) for order in orders)
    return "\n".join(lines)


def format_trade_orders(trade: Trade, orders: list[TradeOrder]) -> str:
    lines = [
        f"Ordres du trade {trade.id}",
        f"{trade.side.upper()} {trade.pair} | statut={trade.status}",
    ]
    if not orders:
        lines.append("Aucun ordre attache.")
    else:
        lines.extend(format_trade_order(order) for order in orders)
    return "\n".join(lines)


def format_trade_list(trades: TradeList) -> str:
    if not trades.items:
        return "Aucun trade trouve."

    start = trades.offset + 1
    end = trades.offset + len(trades.items)
    lines = [f"Trades recents {start}-{end}/{trades.total}:"]
    for trade in trades.items:
        lines.append(
            f"- {trade.id} | {trade.status} | {trade.side.upper()} {trade.pair} | "
            f"{trade.amount_usdc:g} USDC | entry={trade.entry_price:g}"
        )
    return "\n".join(lines)


def format_audit_events(events: AuditEventList) -> str:
    if not events.items:
        return "Aucun evenement d'audit trouve."

    start = events.offset + 1
    end = events.offset + len(events.items)
    lines = [f"Audit {start}-{end}/{events.total}:"]
    for event in events.items:
        trade = f" | trade={event.trade_id}" if event.trade_id else ""
        lines.append(f"- {event.event_type}{trade} | {event.message}")
    return "\n".join(lines)


def format_audit_event_types(event_types: AuditEventTypeList) -> str:
    if not event_types.items:
        return "Aucun type d'evenement d'audit trouve."

    lines = [f"Types audit ({event_types.total}):"]
    for item in event_types.items:
        lines.append(f"- {item.event_type}: {item.count}")
    return "\n".join(lines)


def format_account_balances(balances: list[AccountBalance]) -> str:
    if not balances:
        return "Aucun solde trouve."

    lines = ["Solde Kraken Futures:"]
    for balance in sorted(balances, key=lambda item: (item.account, item.currency)):
        parts = [f"- {balance.account} {balance.currency}"]
        parts.extend(
            f"{label}={format_optional_decimal(value)}"
            for label, value in (
                ("balance", balance.balance),
                ("equity", balance.equity),
                ("available", balance.available),
                ("margin", balance.margin),
            )
            if value is not None
        )
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def format_scalp_status(detail: ScalpSessionDetail) -> str:
    metrics = compute_scalp_metrics(detail.trades)
    scalp_session = detail.session
    lines = [
        f"Scalp session: {scalp_session.id}",
        f"Pair: {scalp_session.pair}",
        f"Mode: {scalp_session.mode}",
        f"Statut: {scalp_session.status}",
        f"Runtime cible: {format_seconds(scalp_session.duration_seconds)}",
        f"Max hold: {format_seconds(scalp_session.max_hold_seconds)}",
        f"Montant: {scalp_session.amount_usdc:g} USDC | leverage={scalp_session.leverage}x | side={scalp_session.side_mode}",
        f"Trades: {metrics['closed']} closed, {metrics['open']} open",
        f"Wins: {metrics['wins']} | Losses: {metrics['losses']} / {scalp_session.max_losses}",
        f"Net PnL: {metrics['net_pnl']:+.2f} USD",
    ]
    if scalp_session.stop_reason:
        lines.append(f"Stop: {scalp_session.stop_reason}")
    return "\n".join(lines)


def format_scalp_report(detail: ScalpSessionDetail) -> str:
    report = build_scalp_session_report(detail)
    close_reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(report.close_reasons.items()))
    lines = [
        f"Rapport scalp: {detail.session.id}",
        f"{detail.session.pair} | mode={detail.session.mode} | statut={detail.session.status}",
        f"Trades fermes: {report.closed_trades} | ouverts: {report.open_trades} | winrate={report.win_rate:.1f}%",
        f"Net PnL: {report.net_pnl:+.2f} USD | gross={report.gross_pnl:+.2f} | frais={report.estimated_fees:.2f}",
        f"Avg win={report.avg_win:+.2f} | avg loss={report.avg_loss:+.2f} | max drawdown={report.max_drawdown:.2f}",
        f"Max losses: {report.losses} / {detail.session.max_losses}",
        f"Signaux rejetes: {report.rejected_signals} | signaux visibles: {len(detail.signals)}",
    ]
    if close_reasons:
        lines.append(f"Raisons de cloture: {close_reasons}")
    if detail.session.stop_reason:
        lines.append(f"Raison d'arret: {detail.session.stop_reason}")
    if report.closed_trades == 0:
        lines.append("Aucun trade ferme pour l'instant.")
    return "\n".join(lines)


def build_scalp_session_report(detail: ScalpSessionDetail) -> ScalpSessionReport:
    metrics = compute_scalp_metrics(detail.trades)
    close_reasons = compute_scalp_close_reasons(detail.trades)
    rejected_signals = len([signal for signal in detail.signals if signal.scalp_trade_id is None])
    win_rate = (metrics["wins"] / metrics["closed"] * 100) if metrics["closed"] else 0
    return ScalpSessionReport(
        session_id=detail.session.id,
        pair=detail.session.pair,
        status=detail.session.status,
        closed_trades=metrics["closed"],
        open_trades=metrics["open"],
        wins=metrics["wins"],
        losses=metrics["losses"],
        win_rate=win_rate,
        gross_pnl=metrics["gross_pnl"],
        estimated_fees=metrics["estimated_fees"],
        net_pnl=metrics["net_pnl"],
        avg_win=metrics["avg_win"],
        avg_loss=metrics["avg_loss"],
        max_drawdown=metrics["max_drawdown"],
        rejected_signals=rejected_signals,
        close_reasons=close_reasons,
        stop_reason=detail.session.stop_reason,
    )


def compute_scalp_metrics(trades: list[ScalpTrade]) -> dict[str, float | int]:
    closed = [trade for trade in trades if trade.status == ScalpTradeStatus.PAPER_CLOSED]
    open_trades = [
        trade
        for trade in trades
        if trade.status in {
            ScalpTradeStatus.PAPER_OPEN,
            ScalpTradeStatus.LIVE_SUBMITTED,
            ScalpTradeStatus.LIVE_ENTRY_FILLED,
        }
    ]
    pnl_values = [trade.net_pnl or 0 for trade in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_values = [trade.gross_pnl or 0 for trade in closed]
    fee_values = [trade.estimated_fees or 0 for trade in closed]
    return {
        "closed": len(closed),
        "open": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "gross_pnl": sum(gross_values),
        "estimated_fees": sum(fee_values),
        "net_pnl": sum(pnl_values),
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "max_drawdown": compute_scalp_max_drawdown(closed),
    }


def compute_scalp_close_reasons(trades: list[ScalpTrade]) -> dict[str, int]:
    close_reasons: dict[str, int] = {}
    for trade in trades:
        if trade.status != ScalpTradeStatus.PAPER_CLOSED:
            continue
        reason = trade.close_reason or "unknown"
        close_reasons[reason] = close_reasons.get(reason, 0) + 1
    return close_reasons


def compute_scalp_max_drawdown(closed_trades: list[ScalpTrade]) -> float:
    running_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    sorted_trades = sorted(closed_trades, key=lambda trade: (trade.closed_at or trade.updated_at, trade.id))
    for trade in sorted_trades:
        running_pnl += trade.net_pnl or 0
        peak_pnl = max(peak_pnl, running_pnl)
        max_drawdown = max(max_drawdown, peak_pnl - running_pnl)
    return max_drawdown


def format_seconds(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _get_open_scalp_trade(session_id: str, session: Session) -> ScalpTrade | None:
    return session.exec(
        select(ScalpTrade).where(
            ScalpTrade.session_id == session_id,
            ScalpTrade.status.in_(
                [
                    ScalpTradeStatus.PAPER_OPEN,
                    ScalpTradeStatus.LIVE_SUBMITTED,
                    ScalpTradeStatus.LIVE_ENTRY_FILLED,
                ]
            ),
        )
    ).first()


def _is_active_scalp_status(status: ScalpSessionStatus) -> bool:
    return status in {ScalpSessionStatus.PAPER_ACTIVE, ScalpSessionStatus.LIVE_ACTIVE}


def _validate_scalp_intent(intent: ScalpIntent, settings: Settings) -> None:
    if intent.mode != "live":
        return
    if not settings.scalp_live_enabled:
        raise ValueError("mode=live requires SCALP_LIVE_ENABLED=true")
    if not settings.can_live_trade:
        raise ValueError("mode=live requires LIVE_TRADING_ENABLED=true, DRY_RUN=false and Kraken credentials")
    if not settings.allows_all_pairs and intent.pair not in settings.allowed_pair_set:
        raise ValueError(f"pair not allowed: {intent.pair}")
    if intent.amount_usdc > settings.max_amount_usdc:
        raise ValueError(f"amount_usdc exceeds max_amount_usdc={settings.max_amount_usdc:g}")
    if intent.amount_usdc > settings.scalp_live_max_amount_usdc:
        raise ValueError(
            f"amount_usdc exceeds scalp_live_max_amount_usdc={settings.scalp_live_max_amount_usdc:g}"
        )
    if intent.leverage > settings.max_leverage:
        raise ValueError(f"leverage exceeds max_leverage={settings.max_leverage}")


def _block_live_scalp_session(scalp_session: ScalpSession, reason: str, session: Session) -> ScalpSessionResult:
    scalp_session.status = ScalpSessionStatus.STOPPED
    scalp_session.stop_reason = "live_blocked"
    scalp_session.stopped_at = utc_now()
    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.add(
        AuditEvent(
            event_type="scalp_live_blocked",
            message=f"Scalp live session {scalp_session.id} blocked: {reason}",
        )
    )
    session.commit()
    return ScalpSessionResult(
        session_id=scalp_session.id,
        status=scalp_session.status,
        message=f"Scalp live bloque: {reason}",
    )


def _maybe_close_scalp_trade(
    scalp_session: ScalpSession,
    trade: ScalpTrade,
    snapshot: MarketSnapshot,
    session: Session,
) -> bool:
    exit_price = snapshot.bid if trade.side == "buy" else snapshot.ask
    gross_pnl = _scalp_gross_pnl(trade, exit_price)
    estimated_fees = _estimate_scalp_fees(trade)
    net_pnl = gross_pnl - estimated_fees
    hold_seconds = _seconds_between(trade.opened_at, snapshot.timestamp)

    close_reason = None
    if net_pnl >= scalp_session.min_net_pnl:
        close_reason = "min_net_pnl"
    elif net_pnl <= -scalp_session.min_net_pnl:
        close_reason = "max_loss_per_trade"
    elif hold_seconds >= scalp_session.max_hold_seconds:
        close_reason = "max_hold"

    if close_reason is None:
        return False

    trade.exit_price = exit_price
    trade.gross_pnl = gross_pnl
    trade.estimated_fees = estimated_fees
    trade.net_pnl = net_pnl
    trade.status = ScalpTradeStatus.PAPER_CLOSED
    trade.close_reason = close_reason
    trade.closed_at = snapshot.timestamp
    trade.updated_at = snapshot.timestamp
    session.add(trade)
    session.flush()
    return True


def _scalp_gross_pnl(trade: ScalpTrade, exit_price: float) -> float:
    if trade.entry_price <= 0:
        return 0
    direction = 1 if trade.side == "buy" else -1
    move_ratio = (exit_price - trade.entry_price) / trade.entry_price
    return trade.amount_usdc * trade.leverage * move_ratio * direction


def _estimate_scalp_fees(trade: ScalpTrade) -> float:
    taker_round_trip_rate = 0.001
    return trade.amount_usdc * trade.leverage * taker_round_trip_rate


def _scalp_loss_count(session_id: str, session: Session) -> int:
    closed_trades = session.exec(
        select(ScalpTrade).where(
            ScalpTrade.session_id == session_id,
            ScalpTrade.status == ScalpTradeStatus.PAPER_CLOSED,
        )
    ).all()
    return len([trade for trade in closed_trades if (trade.net_pnl or 0) < 0])


def _scalp_session_elapsed(scalp_session: ScalpSession, snapshot: MarketSnapshot) -> bool:
    return _seconds_between(scalp_session.started_at, snapshot.timestamp) >= scalp_session.duration_seconds


def _seconds_between(start: datetime, end: datetime) -> float:
    return (_as_utc_aware(end) - _as_utc_aware(start)).total_seconds()


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _complete_scalp_session(scalp_session: ScalpSession, reason: str, session: Session) -> None:
    scalp_session.status = ScalpSessionStatus.COMPLETED
    scalp_session.stop_reason = reason
    scalp_session.stopped_at = utc_now()
    scalp_session.updated_at = utc_now()
    session.add(scalp_session)
    session.add(
        AuditEvent(
            event_type="scalp_session_completed",
            message=f"Scalp paper session {scalp_session.id} completed: {reason}.",
        )
    )
    session.flush()


def _kraken_fill_matches_scalp_trade(fill: KrakenFill, trade: ScalpTrade) -> bool:
    if fill.symbol and fill.symbol != trade.pair.upper():
        return False
    if fill.side and fill.side != trade.side.lower():
        return False
    return True


def _parse_kraken_event_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def format_optional_decimal(value) -> str:
    return f"{value:g}"


def format_trade_order(order: TradeOrder) -> str:
    reduce_only = "oui" if order.reduce_only else "non"
    target = f" | target={order.target_percent:g}%" if order.target_percent is not None else ""
    external_id = f" | external_id={order.external_order_id}" if order.external_order_id else ""
    return (
        f"- {order.role}: {order.side} {order.order_type} {order.price:g} | "
        f"{order.amount_usdc:g} USDC{target} | reduce-only={reduce_only} | "
        f"statut={order.status}{external_id}"
    )


def list_trade_orders(
    trade_id: str,
    session: Session,
    *,
    status: OrderStatus | None = None,
    role: OrderRole | None = None,
) -> list[TradeOrder]:
    filters = [TradeOrder.trade_id == trade_id]
    if status is not None:
        filters.append(TradeOrder.status == status)
    if role is not None:
        filters.append(TradeOrder.role == role)
    orders = session.exec(select(TradeOrder).where(*filters)).all()
    return sorted(orders, key=order_sort_key)


def build_planned_orders(trade: Trade) -> list[TradeOrder]:
    targets = json.loads(trade.targets_json)
    orders = [
        TradeOrder(
            trade_id=trade.id,
            role=OrderRole.ENTRY,
            pair=trade.pair,
            side=trade.side,
            price=trade.entry_price,
            amount_usdc=trade.amount_usdc,
            reduce_only=False,
        )
    ]
    exit_side = opposite_side(trade.side)
    for target in targets:
        orders.append(
            TradeOrder(
                trade_id=trade.id,
                role=OrderRole.TARGET_EXIT,
                pair=trade.pair,
                side=exit_side,
                price=target["price"],
                amount_usdc=trade.amount_usdc * target["percent"] / 100,
                target_percent=target["percent"],
                reduce_only=True,
            )
        )
    return orders


def order_sort_key(order: TradeOrder) -> tuple[int, float, str]:
    role_rank = 0 if order.role == OrderRole.ENTRY else 1
    target_rank = order.target_percent if order.target_percent is not None else 0
    return (role_rank, target_rank, order.id)


def mark_entry_orders_submitted(trade_id: str, result: dict[str, str], session: Session) -> None:
    status = OrderStatus.DRY_RUN_SUBMITTED if result["mode"] == "dry_run" else OrderStatus.LIVE_SUBMITTED
    entry_orders = session.exec(
        select(TradeOrder).where(TradeOrder.trade_id == trade_id, TradeOrder.role == OrderRole.ENTRY)
    ).all()
    for order in entry_orders:
        order.status = status
        order.external_order_id = result.get("external_order_id")
        order.updated_at = utc_now()
        session.add(order)


def cancel_planned_orders(trade_id: str, session: Session) -> None:
    cancellable_orders = session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade_id,
            TradeOrder.status.in_([OrderStatus.PLANNED, OrderStatus.READY_TO_SUBMIT]),
        )
    ).all()
    for order in cancellable_orders:
        order.status = OrderStatus.CANCELLED
        order.updated_at = utc_now()
        session.add(order)


def list_live_cancellable_orders(trade_id: str, session: Session) -> list[TradeOrder]:
    return session.exec(
        select(TradeOrder).where(
            TradeOrder.trade_id == trade_id,
            TradeOrder.status == OrderStatus.LIVE_SUBMITTED,
            TradeOrder.external_order_id.is_not(None),
        )
    ).all()


def opposite_side(side: str) -> str:
    if side == "buy":
        return "sell"
    if side == "sell":
        return "buy"
    raise ValueError(f"unsupported side: {side}")


def pause_trading(session: Session) -> str:
    set_bot_state(session, "trading_paused", "true")
    session.add(AuditEvent(event_type="trading_paused", message="Trading paused by command."))
    session.commit()
    return "Trading mis en pause. Les nouveaux trades et confirmations sont bloques."


def resume_trading(session: Session) -> str:
    set_bot_state(session, "trading_paused", "false")
    session.add(AuditEvent(event_type="trading_resumed", message="Trading resumed by command."))
    session.commit()
    return "Trading relance. Les previews et confirmations sont de nouveau autorisees."


def is_trading_paused(session: Session) -> bool:
    state = session.get(BotState, "trading_paused")
    return state is not None and state.value == "true"


def set_bot_state(session: Session, key: str, value: str) -> None:
    state = session.get(BotState, key)
    if state is None:
        state = BotState(key=key, value=value)
    else:
        state.value = value
        state.updated_at = utc_now()
    session.add(state)
