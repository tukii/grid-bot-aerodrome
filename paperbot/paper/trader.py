import logging
import time

from paperbot.config import load_config
from paperbot.data.price import fetch_price
from paperbot.engine import Engine
from paperbot.paper.store import Store, utcnow
from paperbot.strategies.grid import Grid

logger = logging.getLogger("paperbot")


class PaperTrader:
    def __init__(self, store: Store, poll_seconds: int | None = None):
        cfg = load_config()
        self.cfg = cfg
        self.poll_seconds = poll_seconds or cfg["paper"]["poll_seconds"]
        self.store = store
        self.engine = self._build_engine(cfg)
        self.run_forever = True

    def _build_engine(self, cfg) -> Engine:
        grid_cfg = cfg["grid"]
        sim = cfg["simulation"]
        grid = Grid(
            anchor=grid_cfg["anchor_price"],
            spacing_pct=grid_cfg["spacing_pct"],
            range_pct=grid_cfg["range_pct"],
        )
        return Engine(
            grid=grid,
            capital_usd=sim["initial_capital_usd"],
            taker_fee_pct=sim["taker_fee_pct"],
            slippage_pct=sim["slippage_pct"],
            gas_usd=sim["gas_cost_usd"],
            min_trade_usd=sim["min_trade_usd"],
        )

    def one_cycle(self) -> dict:
        pool = self.cfg["pool"]["address"]
        price = fetch_price(pool)
        if price is None:
            logger.warning("No price available for pool %s", pool)
            return {"ok": False, "price": None}

        self.engine.step(price, utcnow())
        fills = [t for t in self.engine.trades if t.filled]
        # Persist only new fills (idempotent via ts/price heuristics is hard; use count)
        last_meta = self.store.get_meta("filled_count")
        filled_count = int(last_meta or 0)
        new_fills = fills[filled_count:]

        for t in new_fills:
            trade_key = f"{utcnow()}_{t.side}_{t.price:.8f}_{t.size_usd:.8f}"
            existing = self.store.get_meta(f"fill_{trade_key}")
            if existing:
                continue
            self.store.record_trade(
                ts=utcnow(), side=t.side, price=t.price, size_usd=t.size_usd,
                fee_usd=t.fee_usd, gas_usd=t.gas_usd, filled=1,
            )
            self.store.set_meta(f"fill_{trade_key}", "1")
        self.store.set_meta("filled_count", str(len(fills)))

        last = self.engine.history[-1]
        self.store.record_tick(
            ts=last.timestamp, price=last.price, side="",
            size_usd=0.0, fee_usd=0.0, gas_usd=0.0,
            cash=last.cash_usd, position_usd=last.position_usd, total_usd=last.total_usd,
        )
        return {
            "ok": True,
            "price": price,
            "fills": len(new_fills),
            "total_usd": last.total_usd,
        }

    def run(self):
        logger.info("Paper trader started: poll=%ss pool=%s", self.poll_seconds, self.cfg["pool"]["address"])
        while self.run_forever:
            try:
                res = self.one_cycle()
                if res["ok"]:
                    logger.info("price=%.4f total=$%.4f fills=%d", res["price"], res["total_usd"], res["fills"])
            except Exception as e:
                logger.exception("cycle error: %s", e)
            time.sleep(self.poll_seconds)
