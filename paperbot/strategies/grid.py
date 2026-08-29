from dataclasses import dataclass


@dataclass
class GridLevel:
    index: int
    price: float
    is_buy: bool  # True -> we buy at this level (buy the dip); False -> sell


class Grid:
    """Classic grid bot: equidistant price levels around an anchor.

    - buy levels below the anchor (increment position)
    - sell levels above the anchor (decrement position)
    Starting position: 50% cash / 50% base asset at the anchor.
    Each fill is always the minimum increment (one grid step).
    """

    def __init__(self, anchor: float, spacing_pct: float, range_pct: float):
        if anchor <= 0:
            raise ValueError("anchor must be > 0")
        if spacing_pct <= 0 or range_pct <= 0:
            raise ValueError("spacing/range must be > 0")
        self.anchor = anchor
        self.spacing = spacing_pct / 100.0
        self.range_pct = range_pct / 100.0

        steps = int(round(self.range_pct / self.spacing))
        self.buy_levels = [
            GridLevel(i, anchor * (1 - self.spacing * (i + 1)), is_buy=True)
            for i in range(steps)
        ]
        self.sell_levels = [
            GridLevel(i, anchor * (1 + self.spacing * (i + 1)), is_buy=False)
            for i in range(steps)
        ]

    @property
    def levels(self) -> list[float]:
        return [lv.price for lv in self.buy_levels] + [self.anchor] + [lv.price for lv in self.sell_levels]

    def nearest_buy_price(self, price: float) -> float | None:
        """The highest buy level that is below or equal to price."""
        cand = [lv.price for lv in self.buy_levels if lv.price <= price]
        return max(cand) if cand else None

    def nearest_sell_price(self, price: float) -> float | None:
        """The lowest sell level that is above or equal to price."""
        cand = [lv.price for lv in self.sell_levels if lv.price >= price]
        return min(cand) if cand else None
