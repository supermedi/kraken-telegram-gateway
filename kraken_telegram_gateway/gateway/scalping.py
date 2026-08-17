from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    volume_ratio: float = 1

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def book_imbalance(self) -> float:
        total_size = self.bid_size + self.ask_size
        if total_size <= 0:
            return 0
        return (self.bid_size - self.ask_size) / total_size


@dataclass(frozen=True)
class ScalpSignalDecision:
    side: str | None
    score: float
    reason: str


def evaluate_scalp_signal(
    snapshot: MarketSnapshot,
    *,
    side_mode: str,
    max_spread_bps: float = 20,
    min_imbalance: float = 0.25,
    min_volume_ratio: float = 1.2,
) -> ScalpSignalDecision:
    spread_bps = snapshot.spread / snapshot.mid * 10_000 if snapshot.mid > 0 else 10_000
    imbalance = snapshot.book_imbalance
    score = abs(imbalance) * snapshot.volume_ratio

    if spread_bps > max_spread_bps:
        return ScalpSignalDecision(None, score, f"spread too wide: {spread_bps:.1f}bps")
    if snapshot.volume_ratio < min_volume_ratio:
        return ScalpSignalDecision(None, score, f"volume too low: {snapshot.volume_ratio:.2f}x")
    if imbalance >= min_imbalance and side_mode in {"buy", "both"}:
        return ScalpSignalDecision("buy", score, f"buy imbalance {imbalance:.2f}, volume {snapshot.volume_ratio:.2f}x")
    if imbalance <= -min_imbalance and side_mode in {"sell", "both"}:
        return ScalpSignalDecision("sell", score, f"sell imbalance {imbalance:.2f}, volume {snapshot.volume_ratio:.2f}x")
    return ScalpSignalDecision(None, score, f"imbalance below threshold: {imbalance:.2f}")
