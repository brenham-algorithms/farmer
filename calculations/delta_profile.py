import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core.types import Tick


class DeltaProfile:
    """
    Tracks cumulative delta (aggressive buying minus aggressive selling)
    per price bucket across a session.

    This creates a "delta footprint" of the session — showing where
    aggressive buyers and sellers were active at each price level.

    When price moves away from a zone with heavy positive delta,
    those aggressive buyers are now trapped longs (underwater).
    When price moves away from heavy negative delta, those sellers
    are trapped shorts.

    Session-scoped: resets at the configurable session boundary.
    """

    def __init__(
        self,
        tick_size: float,
        bucket_ticks: int = 4,
        session_reset_hour: int = 17,
        session_reset_minute: int = 0,
        tz_name: str = "America/Chicago",
    ) -> None:
        self.tick_size = tick_size
        self.bucket_size = tick_size * bucket_ticks
        self.session_reset_hour = session_reset_hour
        self.session_reset_minute = session_reset_minute
        self.tz = ZoneInfo(tz_name)

        # price_bucket -> {"delta": int, "volume": int, "buy_volume": int, "sell_volume": int}
        self._profile: Dict[float, Dict[str, int]] = defaultdict(
            lambda: {"delta": 0, "volume": 0, "buy_volume": 0, "sell_volume": 0}
        )

        self._current_session_key: Optional[datetime] = None

    def _session_key(self, t_utc: datetime) -> datetime:
        t_local = t_utc.astimezone(self.tz)
        reset_today = t_local.replace(
            hour=self.session_reset_hour,
            minute=self.session_reset_minute,
            second=0,
            microsecond=0,
        )
        if t_local < reset_today:
            return reset_today - timedelta(days=1)
        return reset_today

    def _bucket(self, price: float) -> float:
        return math.floor(price / self.bucket_size) * self.bucket_size

    def on_tick(self, tick: Tick) -> None:
        session = self._session_key(tick.t)
        if session != self._current_session_key:
            self._profile.clear()
            self._current_session_key = session

        bucket = self._bucket(tick.price)
        delta = tick.delta()

        entry = self._profile[bucket]
        entry["delta"] += delta
        entry["volume"] += tick.size
        if delta > 0:
            entry["buy_volume"] += tick.size
        elif delta < 0:
            entry["sell_volume"] += tick.size

    def delta_at(self, price: float) -> int:
        """Net delta at the bucket containing this price."""
        return self._profile.get(self._bucket(price), {"delta": 0})["delta"]

    def volume_at(self, price: float) -> int:
        """Total volume at the bucket containing this price."""
        return self._profile.get(self._bucket(price), {"volume": 0})["volume"]

    def zone_delta(self, low: float, high: float) -> int:
        """Sum of delta across all buckets within [low, high]."""
        total = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total += data["delta"]
        return total

    def zone_volume(self, low: float, high: float) -> int:
        """Sum of volume across all buckets within [low, high]."""
        total = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total += data["volume"]
        return total

    def trapped_longs_below(
        self, current_price: float, min_delta: int = 100
    ) -> List[Tuple[float, int, int]]:
        """
        Price buckets below current_price with heavy positive delta.
        These are aggressive buyers who are now underwater.
        Returns list of (price_bucket, delta, volume) sorted by delta descending.
        """
        result = []
        for bucket, data in self._profile.items():
            if bucket < current_price and data["delta"] > min_delta:
                result.append((bucket, data["delta"], data["volume"]))
        return sorted(result, key=lambda x: x[1], reverse=True)

    def trapped_shorts_above(
        self, current_price: float, min_delta: int = 100
    ) -> List[Tuple[float, int, int]]:
        """
        Price buckets above current_price with heavy negative delta.
        These are aggressive sellers who are now underwater.
        Returns list of (price_bucket, abs(delta), volume) sorted by abs(delta) descending.
        """
        result = []
        for bucket, data in self._profile.items():
            if bucket > current_price and data["delta"] < -min_delta:
                result.append((bucket, data["delta"], data["volume"]))
        return sorted(result, key=lambda x: x[1])

    def profile_around(
        self, center_price: float, buckets_above: int = 10, buckets_below: int = 10
    ) -> List[Tuple[float, int, int]]:
        """
        Returns (price_bucket, delta, volume) for buckets around center_price.
        Sorted by price descending (highest first) for display.
        """
        center_bucket = self._bucket(center_price)
        low = center_bucket - buckets_below * self.bucket_size
        high = center_bucket + buckets_above * self.bucket_size

        result = []
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                result.append((bucket, data["delta"], data["volume"]))
        return sorted(result, key=lambda x: x[0], reverse=True)

    def format_profile(
        self,
        center_price: float,
        buckets_above: int = 10,
        buckets_below: int = 10,
        bar_scale: int = 50,
        precision: int = 2,
    ) -> str:
        """
        Returns a human-readable ASCII visualization of the delta profile
        around a center price.

        Positive delta (buying) shown as + bars to the right.
        Negative delta (selling) shown as - bars to the left.
        """
        profile = self.profile_around(center_price, buckets_above, buckets_below)

        if not profile:
            return "(no data)"

        max_abs_delta = max(abs(d) for _, d, _ in profile) if profile else 1
        if max_abs_delta == 0:
            max_abs_delta = 1

        lines = []
        center_bucket = self._bucket(center_price)

        for price, delta, volume in profile:
            bar_len = int(abs(delta) / max_abs_delta * bar_scale)

            if delta >= 0:
                bar = " " * bar_scale + "|" + "█" * bar_len
            else:
                padding = bar_scale - bar_len
                bar = " " * padding + "█" * bar_len + "|"

            marker = " <-- LEVEL" if price == center_bucket else ""
            lines.append(
                f"  {price:>{10}.{precision}f}  {bar}  {delta:>+8d}  vol={volume:<8d}{marker}"
            )

        header = f"\n{'Delta Profile':^80}\n{'=' * 80}"
        return header + "\n" + "\n".join(lines)
