import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core.types import Tick


class AbsorptionProfile:
    """
    Tracks true absorption per price level across a session.

    Absorption occurs when aggressive volume hits a price but fails
    to move it. This indicates a passive defender (large resting
    limit order) eating the flow.

    Detection per tick:
      - Sell-aggressive trade (delta < 0) and price didn't drop
        (tick.price >= prev_price): passive BUYER absorbed the sell.
      - Buy-aggressive trade (delta > 0) and price didn't rise
        (tick.price <= prev_price): passive SELLER absorbed the buy.

    Per price bucket, tracks:
      - absorbed_sell_vol: sells that were absorbed (passive buyer defending)
      - absorbed_buy_vol: buys that were absorbed (passive seller defending)
      - total_sell_vol / total_buy_vol / total_vol for context

    This answers: "where in today's session are passive defenders
    clearly sitting?" — without needing a BandAttempt at a specific level.

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

        self._profile: Dict[float, Dict[str, int]] = defaultdict(
            lambda: {
                "absorbed_buy_vol": 0,
                "absorbed_sell_vol": 0,
                "total_buy_vol": 0,
                "total_sell_vol": 0,
                "total_vol": 0,
            }
        )

        self._prev_price: Optional[float] = None
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
            self._prev_price = None
            self._current_session_key = session

        bucket = self._bucket(tick.price)
        delta = tick.delta()
        entry = self._profile[bucket]

        entry["total_vol"] += tick.size

        if delta > 0:
            entry["total_buy_vol"] += tick.size
            # Buy-aggressive but price didn't rise: passive seller absorbed it
            if self._prev_price is not None and tick.price <= self._prev_price:
                entry["absorbed_buy_vol"] += tick.size

        elif delta < 0:
            entry["total_sell_vol"] += tick.size
            # Sell-aggressive but price didn't drop: passive buyer absorbed it
            if self._prev_price is not None and tick.price >= self._prev_price:
                entry["absorbed_sell_vol"] += tick.size

        self._prev_price = tick.price

    # Point queries

    def absorbed_sell_vol_at(self, price: float) -> int:
        """Volume of selling that was absorbed at this level (passive buyer defending)."""
        bucket = self._bucket(price)
        return self._profile.get(bucket, {"absorbed_sell_vol": 0})["absorbed_sell_vol"]

    def absorbed_buy_vol_at(self, price: float) -> int:
        """Volume of buying that was absorbed at this level (passive seller defending)."""
        bucket = self._bucket(price)
        return self._profile.get(bucket, {"absorbed_buy_vol": 0})["absorbed_buy_vol"]

    def sell_absorption_ratio_at(self, price: float) -> float:
        """Fraction of sell aggression absorbed at this level.
        High ratio = strong passive buyer defending."""
        bucket = self._bucket(price)
        data = self._profile.get(bucket)
        if data is None or data["total_sell_vol"] == 0:
            return 0.0
        return data["absorbed_sell_vol"] / data["total_sell_vol"]

    def buy_absorption_ratio_at(self, price: float) -> float:
        """Fraction of buy aggression absorbed at this level.
        High ratio = strong passive seller defending."""
        bucket = self._bucket(price)
        data = self._profile.get(bucket)
        if data is None or data["total_buy_vol"] == 0:
            return 0.0
        return data["absorbed_buy_vol"] / data["total_buy_vol"]

    # Zone queries

    def zone_absorbed_sell_vol(self, low: float, high: float) -> int:
        """Total absorbed sell volume across a price range."""
        total = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total += data["absorbed_sell_vol"]
        return total

    def zone_absorbed_buy_vol(self, low: float, high: float) -> int:
        """Total absorbed buy volume across a price range."""
        total = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total += data["absorbed_buy_vol"]
        return total

    def zone_sell_absorption_ratio(self, low: float, high: float) -> float:
        """Fraction of sell aggression absorbed across a price range.
        High = passive buyers defending this zone."""
        total_sell = 0
        absorbed_sell = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total_sell += data["total_sell_vol"]
                absorbed_sell += data["absorbed_sell_vol"]
        if total_sell == 0:
            return 0.0
        return absorbed_sell / total_sell

    def zone_buy_absorption_ratio(self, low: float, high: float) -> float:
        """Fraction of buy aggression absorbed across a price range.
        High = passive sellers defending this zone."""
        total_buy = 0
        absorbed_buy = 0
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                total_buy += data["total_buy_vol"]
                absorbed_buy += data["absorbed_buy_vol"]
        if total_buy == 0:
            return 0.0
        return absorbed_buy / total_buy

    # Discovery

    def passive_buyers(
        self, min_ratio: float = 0.5, min_volume: int = 100
    ) -> List[Tuple[float, float, int, int]]:
        """Find levels where passive buyers are defending (absorbing sells).
        Returns (price_bucket, sell_absorption_ratio, absorbed_sell_vol, total_sell_vol)
        sorted by absorbed volume descending."""
        result = []
        for bucket, data in self._profile.items():
            if data["total_sell_vol"] < min_volume:
                continue
            ratio = data["absorbed_sell_vol"] / data["total_sell_vol"]
            if ratio >= min_ratio:
                result.append(
                    (bucket, ratio, data["absorbed_sell_vol"], data["total_sell_vol"])
                )
        return sorted(result, key=lambda x: x[2], reverse=True)

    def passive_sellers(
        self, min_ratio: float = 0.5, min_volume: int = 100
    ) -> List[Tuple[float, float, int, int]]:
        """Find levels where passive sellers are defending (absorbing buys).
        Returns (price_bucket, buy_absorption_ratio, absorbed_buy_vol, total_buy_vol)
        sorted by absorbed volume descending."""
        result = []
        for bucket, data in self._profile.items():
            if data["total_buy_vol"] < min_volume:
                continue
            ratio = data["absorbed_buy_vol"] / data["total_buy_vol"]
            if ratio >= min_ratio:
                result.append(
                    (bucket, ratio, data["absorbed_buy_vol"], data["total_buy_vol"])
                )
        return sorted(result, key=lambda x: x[2], reverse=True)

    # Visualization

    def format_profile(
        self,
        center_price: float,
        buckets_above: int = 10,
        buckets_below: int = 10,
        bar_scale: int = 40,
        precision: int = 2,
    ) -> str:
        """
        ASCII visualization of absorption around a price.

        Left side: absorbed sell volume (passive buyers defending)
        Right side: absorbed buy volume (passive sellers defending)
        """
        center_bucket = self._bucket(center_price)
        low = center_bucket - buckets_below * self.bucket_size
        high = center_bucket + buckets_above * self.bucket_size

        rows = []
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                rows.append(
                    (
                        bucket,
                        data["absorbed_sell_vol"],
                        data["absorbed_buy_vol"],
                        data["total_vol"],
                    )
                )

        if not rows:
            return "(no data)"

        rows.sort(key=lambda x: x[0], reverse=True)

        max_absorbed = max(
            max(r[1] for r in rows),
            max(r[2] for r in rows),
            1,
        )

        lines = []
        for price, abs_sell, abs_buy, total_vol in rows:
            sell_bar_len = int(abs_sell / max_absorbed * bar_scale)
            buy_bar_len = int(abs_buy / max_absorbed * bar_scale)

            sell_bar = " " * (bar_scale - sell_bar_len) + "█" * sell_bar_len
            buy_bar = "█" * buy_bar_len

            marker = " <-- CENTER" if price == center_bucket else ""

            # Compute ratios for display
            sell_ratio = (
                abs_sell / data["total_sell_vol"]
                if self._profile[price]["total_sell_vol"] > 0
                else 0
            )
            buy_ratio = (
                abs_buy / data["total_buy_vol"]
                if self._profile[price]["total_buy_vol"] > 0
                else 0
            )

            lines.append(
                f"  {price:>{10}.{precision}f}  "
                f"{sell_bar}|{buy_bar:<{bar_scale}}  "
                f"sell_abs={abs_sell:<6d}({sell_ratio:.0%}) "
                f"buy_abs={abs_buy:<6d}({buy_ratio:.0%}) "
                f"vol={total_vol:<8d}{marker}"
            )

        header = (
            f"\n{'Absorption Profile':^120}\n"
            f"{'=' * 120}\n"
            f"{'':>14}  {'← Passive Buyers (absorbed sells)':>{bar_scale + 1}}"
            f"{'Passive Sellers (absorbed buys) →':<{bar_scale}}"
        )
        return header + "\n" + "\n".join(lines)

    def profile_around(
        self, center_price: float, buckets_above: int = 10, buckets_below: int = 10
    ) -> List[Tuple[float, int, int, int]]:
        """Returns (price_bucket, absorbed_sell_vol, absorbed_buy_vol, total_vol)
        around center_price, sorted by price descending."""
        center_bucket = self._bucket(center_price)
        low = center_bucket - buckets_below * self.bucket_size
        high = center_bucket + buckets_above * self.bucket_size

        result = []
        for bucket, data in self._profile.items():
            if low <= bucket <= high:
                result.append(
                    (
                        bucket,
                        data["absorbed_sell_vol"],
                        data["absorbed_buy_vol"],
                        data["total_vol"],
                    )
                )
        return sorted(result, key=lambda x: x[0], reverse=True)
