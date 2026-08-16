from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from kraken_telegram_gateway.gateway.app import app
from kraken_telegram_gateway.gateway.config import Settings, get_settings
from kraken_telegram_gateway.gateway.db import get_session


@contextmanager
def api_client(settings: Settings | None = None):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings or Settings(max_amount_usdc=100)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def create_preview(client: TestClient, text: str | None = None):
    return client.post(
        "/commands/trade",
        json={
            "text": text
            or (
                "/trade pair=PF_XBTUSD side=sell amount_usdc=100 "
                "entry=limit:65000 t1=63000:100%"
            )
        },
    )


def test_trade_detail_api_includes_attached_orders():
    with api_client() as client:
        preview_response = create_preview(client)
        trade_id = preview_response.json()["trade_id"]

        detail_response = client.get(f"/trades/{trade_id}")

    assert preview_response.status_code == 200
    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert payload["trade"]["id"] == trade_id
    assert len(payload["orders"]) == 2
    assert payload["orders"][0]["role"] == "entry"
    assert payload["orders"][0]["reduce_only"] is False
    assert payload["orders"][1]["role"] == "target_exit"
    assert payload["orders"][1]["reduce_only"] is True
    assert payload["orders"][1]["side"] == "buy"


def test_trade_orders_api_filters_by_status_and_role():
    with api_client() as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")

        planned_response = client.get(f"/trades/{trade_id}/orders", params={"status": "planned"})
        entry_response = client.get(f"/trades/{trade_id}/orders", params={"role": "entry"})
        target_planned_response = client.get(
            f"/trades/{trade_id}/orders",
            params={"status": "planned", "role": "target_exit"},
        )

    assert preview_response.status_code == 200
    assert planned_response.status_code == 200
    assert [order["role"] for order in planned_response.json()] == ["target_exit", "target_exit"]
    assert {order["status"] for order in planned_response.json()} == {"planned"}

    assert entry_response.status_code == 200
    assert [order["role"] for order in entry_response.json()] == ["entry"]
    assert entry_response.json()[0]["status"] == "dry_run_submitted"

    assert target_planned_response.status_code == 200
    assert [order["target_percent"] for order in target_planned_response.json()] == [40.0, 60.0]


def test_trade_orders_api_handles_missing_trade():
    with api_client() as client:
        response = client.get("/trades/missing/orders")

    assert response.status_code == 404
    assert response.json()["detail"] == "Trade not found"


def test_entry_filled_api_marks_entry_filled_and_targets_ready():
    with api_client() as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")

        filled_response = client.post(f"/commands/entry-filled/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")
        audit_response = client.get("/audit", params={"trade_id": trade_id, "event_type": "entry_filled"})

    assert filled_response.status_code == 200
    assert filled_response.json()["status"] == "entry_filled"
    assert "aucun ordre Kraken envoye" in filled_response.json()["message"]

    orders = orders_response.json()
    assert orders[0]["role"] == "entry"
    assert orders[0]["status"] == "filled"
    assert [order["status"] for order in orders[1:]] == ["ready_to_submit", "ready_to_submit"]

    assert audit_response.status_code == 200
    assert audit_response.json()["total"] == 1
    assert audit_response.json()["items"][0]["event_type"] == "entry_filled"


def test_entry_filled_api_is_idempotent_after_first_marker():
    with api_client() as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")
        first_response = client.post(f"/commands/entry-filled/{trade_id}")

        second_response = client.post(f"/commands/entry-filled/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")
        audit_response = client.get("/audit", params={"trade_id": trade_id, "event_type": "entry_filled"})

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["status"] == "entry_filled"
    assert "Aucun changement applique" in second_response.json()["message"]
    assert [order["status"] for order in orders_response.json()] == [
        "filled",
        "ready_to_submit",
        "ready_to_submit",
    ]
    assert audit_response.json()["total"] == 1


def test_entry_filled_api_rejects_unconfirmed_trade_without_changing_orders():
    with api_client() as client:
        preview_response = create_preview(client)
        trade_id = preview_response.json()["trade_id"]

        filled_response = client.post(f"/commands/entry-filled/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")

    assert filled_response.status_code == 200
    assert filled_response.json()["status"] == "pending_confirmation"
    assert "doit d'abord etre confirme" in filled_response.json()["message"]
    assert {order["status"] for order in orders_response.json()} == {"planned"}


def test_submit_targets_api_marks_ready_targets_submitted_in_dry_run():
    with api_client() as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")
        client.post(f"/commands/entry-filled/{trade_id}")

        submit_response = client.post(f"/commands/submit-targets/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders", params={"role": "target_exit"})
        audit_response = client.get("/audit", params={"trade_id": trade_id, "event_type": "targets_submitted"})

    assert submit_response.status_code == 200
    assert submit_response.json()["status"] == "entry_filled"
    assert "2 target(s) reduce-only" in submit_response.json()["message"]
    assert "aucun ordre Kraken envoye" in submit_response.json()["message"]

    target_orders = orders_response.json()
    assert [order["status"] for order in target_orders] == ["dry_run_submitted", "dry_run_submitted"]
    assert all(order["external_order_id"].startswith("dryrun-target-") for order in target_orders)

    assert audit_response.status_code == 200
    assert audit_response.json()["total"] == 1
    assert audit_response.json()["items"][0]["event_type"] == "targets_submitted"


def test_submit_targets_api_retry_is_noop_after_targets_are_submitted():
    with api_client() as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 "
            "t1=67000:40% t2=69000:60%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")
        client.post(f"/commands/entry-filled/{trade_id}")
        client.post(f"/commands/submit-targets/{trade_id}")
        first_orders = client.get(f"/trades/{trade_id}/orders", params={"role": "target_exit"}).json()

        retry_response = client.post(f"/commands/submit-targets/{trade_id}")
        retry_orders = client.get(f"/trades/{trade_id}/orders", params={"role": "target_exit"}).json()
        audit_response = client.get("/audit", params={"trade_id": trade_id, "event_type": "targets_submitted"})

    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == "entry_filled"
    assert "Targets deja soumises: 2 target(s)" in retry_response.json()["message"]
    assert "Aucun changement applique" in retry_response.json()["message"]
    assert [order["status"] for order in retry_orders] == ["dry_run_submitted", "dry_run_submitted"]
    assert [order["external_order_id"] for order in retry_orders] == [
        order["external_order_id"] for order in first_orders
    ]
    assert audit_response.json()["total"] == 1


def test_submit_targets_api_rejects_before_entry_is_filled():
    with api_client() as client:
        preview_response = create_preview(client)
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")

        submit_response = client.post(f"/commands/submit-targets/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")

    assert submit_response.status_code == 200
    assert submit_response.json()["status"] == "dry_run_executed"
    assert "l'entree doit etre marquee filled" in submit_response.json()["message"]
    assert {order["status"] for order in orders_response.json()} == {"dry_run_submitted", "planned"}


def test_cancel_api_retry_is_idempotent():
    with api_client() as client:
        preview_response = create_preview(client)
        trade_id = preview_response.json()["trade_id"]
        first_response = client.post(f"/commands/cancel/{trade_id}")

        retry_response = client.post(f"/commands/cancel/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")
        audit_response = client.get("/audit", params={"trade_id": trade_id, "event_type": "trade_cancelled"})

    assert first_response.status_code == 200
    assert first_response.json()["status"] == "cancelled"
    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == "cancelled"
    assert "Aucun changement applique" in retry_response.json()["message"]
    assert {order["status"] for order in orders_response.json()} == {"cancelled"}
    assert audit_response.json()["total"] == 1


def test_confirm_api_rejects_missing_stop_when_required():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with api_client(settings) as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
        )
        trade_id = preview_response.json()["trade_id"]

        confirm_response = client.post(f"/commands/confirm/{trade_id}")
        orders_response = client.get(f"/trades/{trade_id}/orders")

    assert preview_response.status_code == 200
    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "rejected"
    assert "stop loss requis" in confirm_response.json()["message"]
    assert {order["status"] for order in orders_response.json()} == {"planned"}


def test_audit_api_returns_recent_events_with_filters_and_pagination():
    settings = Settings(max_amount_usdc=100, require_stop_loss_for_confirmation=True)
    with api_client(settings) as client:
        preview_response = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=100 entry=limit:65000 t1=67000:100%",
        )
        trade_id = preview_response.json()["trade_id"]
        client.post(f"/commands/confirm/{trade_id}")

        all_response = client.get("/audit")
        trade_response = client.get("/audit", params={"trade_id": trade_id})
        rejected_response = client.get("/audit", params={"event_type": "trade_rejected"})
        page_response = client.get("/audit", params={"limit": 1, "offset": 1})

    assert all_response.status_code == 200
    all_payload = all_response.json()
    assert all_payload["total"] == 2
    assert all_payload["limit"] == 20
    assert all_payload["offset"] == 0
    assert [event["event_type"] for event in all_payload["items"]] == ["trade_rejected", "trade_preview"]

    assert trade_response.status_code == 200
    assert trade_response.json()["total"] == 2
    assert {event["trade_id"] for event in trade_response.json()["items"]} == {trade_id}

    assert rejected_response.status_code == 200
    assert rejected_response.json()["total"] == 1
    assert rejected_response.json()["items"][0]["event_type"] == "trade_rejected"
    assert "stop loss requis" in rejected_response.json()["items"][0]["message"]

    assert page_response.status_code == 200
    assert page_response.json()["total"] == 2
    assert page_response.json()["limit"] == 1
    assert [event["event_type"] for event in page_response.json()["items"]] == ["trade_preview"]


def test_trade_list_api_returns_recent_trades_with_filters_and_pagination():
    with api_client() as client:
        first = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=buy amount_usdc=50 entry=limit:65000 t1=67000:100%",
        ).json()["trade_id"]
        second = create_preview(
            client,
            "/trade pair=PF_ETHUSD side=buy amount_usdc=60 entry=limit:3200 t1=3400:100%",
        ).json()["trade_id"]
        third = create_preview(
            client,
            "/trade pair=PF_XBTUSD side=sell amount_usdc=70 entry=limit:66000 t1=64000:100%",
        ).json()["trade_id"]
        client.post(f"/commands/confirm/{second}")
        client.post(f"/commands/cancel/{third}")

        all_response = client.get("/trades")
        cancelled_response = client.get("/trades", params={"status": "cancelled"})
        xbt_response = client.get("/trades", params={"pair": "pf_xbtusd"})
        page_response = client.get("/trades", params={"limit": 1, "offset": 1})

    assert all_response.status_code == 200
    all_payload = all_response.json()
    assert all_payload["total"] == 3
    assert all_payload["limit"] == 20
    assert all_payload["offset"] == 0
    assert [trade["id"] for trade in all_payload["items"]] == [third, second, first]

    assert cancelled_response.status_code == 200
    assert [trade["id"] for trade in cancelled_response.json()["items"]] == [third]

    assert xbt_response.status_code == 200
    assert xbt_response.json()["total"] == 2
    assert [trade["id"] for trade in xbt_response.json()["items"]] == [third, first]

    assert page_response.status_code == 200
    page_payload = page_response.json()
    assert page_payload["total"] == 3
    assert page_payload["limit"] == 1
    assert page_payload["offset"] == 1
    assert [trade["id"] for trade in page_payload["items"]] == [second]
