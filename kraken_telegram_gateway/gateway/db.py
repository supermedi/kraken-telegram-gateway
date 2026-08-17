from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from kraken_telegram_gateway.gateway.config import get_settings

engine = create_engine(get_settings().database_url, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    ensure_runtime_schema()


def ensure_runtime_schema() -> None:
    inspector = inspect(engine)
    if "scalptrade" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("scalptrade")}
    with engine.begin() as connection:
        if "external_order_id" not in columns:
            connection.execute(text("ALTER TABLE scalptrade ADD COLUMN external_order_id VARCHAR"))


def get_session():
    with Session(engine) as session:
        yield session
