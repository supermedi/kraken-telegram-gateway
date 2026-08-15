from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from kraken_telegram_gateway.gateway.app import app
from kraken_telegram_gateway.gateway.config import Settings, get_settings
from kraken_telegram_gateway.gateway.db import get_session


def test_trade_detail_api_includes_attached_orders():
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
    app.dependency_overrides[get_settings] = lambda: Settings(max_amount_usdc=100)
    try:
        client = TestClient(app)
        preview_response = client.post(
            "/commands/trade",
            json={
                "text": (
                    "/trade pair=PF_XBTUSD side=sell amount_usdc=100 "
                    "entry=limit:65000 t1=63000:100%"
                )
            },
        )
        trade_id = preview_response.json()["trade_id"]

        detail_response = client.get(f"/trades/{trade_id}")
    finally:
        app.dependency_overrides.clear()

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
