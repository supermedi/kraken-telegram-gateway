from typing import Any
from kraken_telegram_gateway.gateway.kraken import KrakenClient

def get_market_trend(client: KrakenClient, symbol: str, timeframe: str = "60") -> str:
    """
    Analyse la tendance sur une timeframe donnée (60 = 1H, 30 = 30M).
    Retourne 'bullish', 'bearish' ou 'neutral'.
    """
    candles = client.fetch_ohlcv(symbol, interval=timeframe, count=2)
    if len(candles) < 2:
        return "neutral"
    
    # Logique simple : direction de la dernière bougie clôturée
    # candles format typique Kraken: [timestamp, open, high, low, close, volume, ...]
    # Dans l'adapter Kraken, les bougies sont souvent des dicts ou des listes.
    # Ici, nous attendons des dicts basés sur l'API OHLCV de Kraken Futures.
    
    prev_close = float(candles[-2].get("close", 0))
    curr_close = float(candles[-1].get("close", 0))
    
    if curr_close > prev_close:
        return "bullish"
    elif curr_close < prev_close:
        return "bearish"
    return "neutral"

def validate_signal_with_trend(trend_1h: str, trend_30m: str, requested_side: str) -> bool:
    """
    Filtre le signal basé sur la tendance.
    Ex: rejet LONG si trend 1H/30M est Bearish.
    """
    if requested_side == "buy":
        return trend_1h != "bearish" and trend_30m != "bearish"
    if requested_side == "sell":
        return trend_1h != "bullish" and trend_30m != "bullish"
    return True
