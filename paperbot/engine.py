from dataclasses import dataclass, field

import pandas as pd

from paperbot.strategies.grid import Grid


@dataclass
class OrderResult:
    filled: bool
    side: str  # buy/sell
    price: float
    size_usd: float
    fee_usd: float
    gas_usd: float
    reason: str = ""


@dataclass
class EquityPoint:
    timestamp: object
    price: float
    cash_usd: float
    position_usd: float
    total_usd: float


@dataclass
class BacktestResult:
    grid: Grid
    trades: list[OrderResult] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    final_total_usd: float = 0.0
    initial_total_usd: float = 0.0

    @property
    def pnl_usd(self) -> float:
        return self.final_total_usd - self.initial_total_usd

    @property
    def pnl_pct(self) -> float:
        return self.pnl_usd / self.initial_total_usd * 100 if self.initial_total_usd else 0.0

    @property
    def n_buys(self) -> int:
        return sum(1 for t in self.trades if t.filled and t.side == "buy")

    @property
    def n_sells(self) -> int:
        return sum(1 for t in self.trades if t.filled and t.side == "sell")

    @property
    def wins(self) -> int:
        wins = 0
        for i, t in enumerate(self.trades):
            if t.filled and t.side == "sell":
                for b in reversed(self.trades[:i]):
                    if b.filled and b.side == "buy":
                        if t.price > b.price:
                            wins += 1
                        break
        return wins

    @property
    def win_rate_pct(self) -> float:
        if self.n_sells == 0:
            return 0.0
        return self.wins / self.n_sells * 100

    @property
    def max_drawdown_pct(self) -> float:
        peak = -float("inf")
        mdd = 0.0
        for pt in self.equity:
            peak = max(peak, pt.total_usd)
            if peak > 0:
                mdd = max(mdd, (peak - pt.total_usd) / peak * 100)
        return mdd

    @property
    def total_fees_usd(self) -> float:
        return sum(t.fee_usd + t.gas_usd for t in self.trades if t.filled)


class Engine:
    """Simulated grid market maker with asset-unit accounting.

    The base position is tracked in units of the base token (WETH). Each grid
    trade moves a fixed number of units; a round trip (buy low, sell higher)
    therefore accrues real USD profit/loss. Cash is tracked in USD.

    Supports:
    - `floating`: re-anchor the grid when price drifts > `reanchor_pct`.
    - one-time setup costs: an approval + a wrap, subtracted from capital.
    """

    def __init__(self, grid: Grid, capital_usd: float, taker_fee_pct: float = 0.05,
                 slippage_pct: float = 0.02, gas_usd: float = 0.01, min_trade_usd: float = 0.10,
                 seed_base_ratio: float = 0.5, floating: bool = False,
                 reanchor_pct: float = 25.0, setup_cost_usd: float = 0.0):
        self.grid = grid
        self.capital = capital_usd
        self.setup_cost = setup_cost_usd
        self.capital = max(0.0, capital_usd - setup_cost_usd)
        anchor = grid.anchor
        self.cash = self.capital * (1 - seed_base_ratio)
        self.base_units = self.capital * seed_base_ratio / anchor
        self.taker_fee = taker_fee_pct / 100.0
        self.slippage = slippage_pct / 100.0
        self.gas_usd = gas_usd
        self.min_trade = min_trade_usd
        self.floating = floating
        self.reanchor_pct = reanchor_pct / 100.0
        self.trades: list[OrderResult] = []
        self.history: list[EquityPoint] = []
        self.last_price: float = anchor

        n_levels = len(grid.buy_levels) + len(grid.sell_levels) + 1
        self.level_size_usd = self.capital / n_levels
        self.units_per_trade = self.level_size_usd / anchor
        self._pending: dict[float, str] = {}
        self._reset_orders()

    def _reset_orders(self):
        self._pending = {}
        for lv in self.grid.buy_levels:
            self._pending[round(lv.price, 8)] = "buy"
        for lv in self.grid.sell_levels:
            self._pending[round(lv.price, 8)] = "sell"
        # Only add anchor buy if no position exists (avoid double-buy)
        if self.base_units <= 0:
            self._pending[round(self.grid.anchor, 8)] = "buy"

    def step(self, price: float, timestamp=None):
        self.last_price = price
        step = self.grid.spacing

        if self.floating:
            drift = abs(price - self.grid.anchor) / self.grid.anchor
            if drift > self.reanchor_pct:
                self.grid = Grid(anchor=price, spacing_pct=self.grid.spacing * 100,
                                 range_pct=self.grid.range_pct * 100)
                self._reset_orders()

        for p, side in list(self._pending.items()):
            if side == "buy" and p >= price:
                self._execute_buy(p, timestamp)
                del self._pending[p]
                sell_price = round(p * (1 + step), 8)
                self._pending[sell_price] = "sell"
                # If the new sell level is already at/below price, execute immediately
                if sell_price <= price:
                    self._execute_sell(sell_price, timestamp)
                    del self._pending[sell_price]
                    self._pending[round(sell_price / (1 + step), 8)] = "buy"

        for p, side in list(self._pending.items()):
            if side == "sell" and p <= price:
                self._execute_sell(p, timestamp)
                del self._pending[p]
                self._pending[round(p / (1 + step), 8)] = "buy"

        self._record(timestamp)

    def _execute_buy(self, level_price: float, timestamp):
        units = self.units_per_trade
        exec_price = level_price * (1 + self.slippage)
        cost_usd = units * exec_price
        fee = cost_usd * self.taker_fee
        gas = self.gas_usd
        total_cost = cost_usd + fee + gas
        if cost_usd < self.min_trade:
            self.trades.append(OrderResult(False, "buy", exec_price, cost_usd, fee, gas, "size < min_trade"))
            return
        if total_cost > self.cash:
            # scale units to what cash allows
            units = max(0.0, (self.cash - gas) / (exec_price * (1 + self.taker_fee)))
            cost_usd = units * exec_price
            fee = cost_usd * self.taker_fee
            total_cost = cost_usd + fee + gas
            if cost_usd < self.min_trade:
                self.trades.append(OrderResult(False, "buy", exec_price, cost_usd, fee, gas, "no cash"))
                return
        self.cash -= total_cost
        self.base_units += units
        self.trades.append(OrderResult(True, "buy", exec_price, cost_usd, fee, gas))

    def _execute_sell(self, level_price: float, timestamp):
        units = min(self.units_per_trade, self.base_units)
        exec_price = level_price * (1 - self.slippage)
        size_usd = units * exec_price
        fee = size_usd * self.taker_fee
        gas = self.gas_usd
        proceeds = size_usd - fee - gas
        if size_usd < self.min_trade or units <= 0:
            self.trades.append(OrderResult(False, "sell", exec_price, size_usd, fee, gas, "no base"))
            return
        if proceeds <= 0:
            self.trades.append(OrderResult(False, "sell", exec_price, size_usd, fee, gas, "proceeds <= 0"))
            return
        self.cash += proceeds
        self.base_units -= units
        self.trades.append(OrderResult(True, "sell", exec_price, size_usd, fee, gas))

    @property
    def position_usd(self) -> float:
        return self.base_units * self.last_price

    def _record(self, timestamp):
        total = self.cash + self.position_usd
        self.history.append(EquityPoint(timestamp, self.last_price, self.cash, self.position_usd, total))

    def run(self, df: pd.DataFrame) -> BacktestResult:
        for ts, row in df.iterrows():
            self.step(float(row["close"]), ts)
        res = BacktestResult(grid=self.grid, trades=self.trades, equity=self.history)
        res.initial_total_usd = self.capital
        res.final_total_usd = self.history[-1].total_usd if self.history else self.capital
        return res
