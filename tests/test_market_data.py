from datetime import datetime, timezone

from kraken_telegram_gateway.gateway.market_data import KrakenFuturesBook


def test_kraken_futures_book_builds_snapshot_from_book_snapshot():
    book = KrakenFuturesBook("PF_XBTUSD")

    snapshot = book.apply(
        {
            "feed": "book_snapshot",
            "product_id": "PF_XBTUSD",
            "timestamp": 1612269825817,
            "bids": [
                {"price": 34892.5, "qty": 6385},
                {"price": 34892, "qty": 10924},
            ],
            "asks": [
                {"price": 34911.5, "qty": 20598},
                {"price": 34912, "qty": 2300},
            ],
        }
    )

    assert snapshot is not None
    assert snapshot.timestamp == datetime.fromtimestamp(1612269825817 / 1000, timezone.utc)
    assert snapshot.bid == 34892.5
    assert snapshot.ask == 34911.5
    assert snapshot.bid_size == 6385
    assert snapshot.ask_size == 20598


def test_kraken_futures_book_applies_delta_updates_and_deletes():
    book = KrakenFuturesBook("PF_XBTUSD")
    book.apply(
        {
            "feed": "book_snapshot",
            "product_id": "PF_XBTUSD",
            "timestamp": 1612269825817,
            "bids": [{"price": 100, "qty": 5}],
            "asks": [{"price": 101, "qty": 6}],
        }
    )

    updated = book.apply(
        {
            "feed": "book",
            "product_id": "PF_XBTUSD",
            "side": "buy",
            "price": 100.5,
            "qty": 8,
            "timestamp": 1612269826817,
        }
    )
    deleted = book.apply(
        {
            "feed": "book",
            "product_id": "PF_XBTUSD",
            "side": "sell",
            "price": 101,
            "qty": 0,
            "timestamp": 1612269827817,
        }
    )

    assert updated is not None
    assert updated.bid == 100.5
    assert updated.ask == 101
    assert updated.bid_size == 8
    assert deleted is None


def test_kraken_futures_book_uses_ticker_lite_fallback_and_volume_ratio():
    book = KrakenFuturesBook("PF_XBTUSD")

    first = book.apply(
        {
            "feed": "ticker_lite",
            "product_id": "PF_XBTUSD",
            "bid": 100,
            "ask": 100.5,
            "volume": 10,
        }
    )
    second = book.apply(
        {
            "feed": "ticker_lite",
            "product_id": "PF_XBTUSD",
            "bid": 101,
            "ask": 101.5,
            "volume": 15,
        }
    )

    assert first is not None
    assert first.volume_ratio == 1
    assert second is not None
    assert second.bid == 101
    assert second.ask == 101.5
    assert second.bid_size == 1
    assert second.ask_size == 1
    assert second.volume_ratio == 1.5
