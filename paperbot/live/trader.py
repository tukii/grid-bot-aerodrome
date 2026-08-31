"""Autonomous live grid trader on Aerodrome Slipstream.

Generalized for any base token (WETH, VELVET, etc.) against USDC.
"""
import json
import logging
import time
from datetime import datetime, timezone

from paperbot.config import load_config
from paperbot.data.price import fetch_price
from paperbot.live.aerodrome import AerodromeLive
from paperbot.live.alerts import send_alert
from paperbot.paper.store import Store
from paperbot.strategies.grid import Grid

logger = logging.getLogger("paperbot.live")

USD_PER_BASE = None  # cached price of base token (invalidated on pool change)


def _usd_value(bot, account) -> tuple[float, float, float]:
    """Return (base_usd, usdc_usd, total_usd) using on-chain balances."""
    global USD_PER_BASE
    price = fetch_price(bot.cfg["pool"]["address"])
    if price:
        USD_PER_BASE = price
    elif USD_PER_BASE is None:
        raise RuntimeError("No price available")
    else:
        logger.warning("price fetch failed, using cached USD_PER_BASE=%.4f", USD_PER_BASE)
    base = bot.token_balance(bot.base_token, account.address) / 10 ** bot.base_decimals
    usdc = bot.token_balance(bot.usdc, account.address) / 10 ** bot.quote_decimals
    return base * USD_PER_BASE, usdc, base * USD_PER_BASE + usdc


class LiveGridTrader:
    def __init__(self, store: Store, poll_seconds: int | None = None,
                 dry_run: bool | None = None):
        cfg = load_config()
        self.cfg = cfg
        self.live_cfg = cfg["live"]
        self.grid_cfg = cfg["grid"]
        self.poll_seconds = poll_seconds or self.live_cfg["poll_seconds"]
        self.bot = AerodromeLive()
        self.account = self.bot.get_account()
        self.store = store
        self.dry_run = self.live_cfg["dry_run"] if dry_run is None else dry_run
        self.stop_loss_pct = self.live_cfg.get("stop_loss_pct", 10.0)
        self.reanchor_pct = self.grid_cfg.get("drift_reanchor_pct", 25.0)
        self.rebalance_threshold = self.live_cfg.get("rebalance_threshold", 0.15)
        self.max_spend_usd = self.live_cfg.get("max_spend_usd", 5.0)
        self.max_position_pct = self.live_cfg.get("max_position_pct", 0.5)
        self.min_liquidity_usd = self.live_cfg.get("min_liquidity_usd", 250_000)
        self.min_volume_usd = self.live_cfg.get("min_volume_usd", 500_000)
        self.rotation_enabled = self.live_cfg.get("rotation_enabled", False)
        self.rotation_interval_h = self.live_cfg.get("rotation_interval_h", 6.0)
        self.min_vol_ratio = self.live_cfg.get("min_vol_ratio", 1.5)
        self._last_scan = 0.0
        self.peak_equity = 0.0
        self.running = True

        # Grid state (persisted)
        self.grid = Grid(
            anchor=cfg["grid"]["anchor_price"],
            spacing_pct=cfg["grid"]["spacing_pct"],
            range_pct=cfg["grid"]["range_pct"],
        )
        self._orders: dict[float, str] = {}
        self._last_price = cfg["grid"]["anchor_price"]
        self._load_state()
        if not self._orders:
            self._reset_orders()
            self._save_state()

    # ---- guard-rails: pool sanity ----
    def check_pool_safety(self) -> bool:
        """Guard-rail: reject low-liquidity / low-volume pools (rug risk)."""
        try:
            from paperbot.data.geckoterminal import fetch_pool_info
            info = fetch_pool_info(self.cfg["pool"]["address"])
            liq = info["reserve_in_usd"]
            vol = info["volume_usd_24h"]
            ok = liq >= self.min_liquidity_usd and vol >= self.min_volume_usd
            logger.info("pool safety: liq=$%.0f vol24h=$%.0f -> %s",
                        liq, vol, "OK" if ok else "REJECT")
            return ok
        except Exception as e:
            logger.error("pool safety check failed: %s", e)
            return False

    # ---- state persistence ----
    def _load_state(self):
        raw = self.store.get_meta("grid_state")
        if not raw:
            return
        try:
            state = json.loads(raw)
            stored_pool = state.get("active_pool")
            current_pool = self.cfg["pool"]["address"]
            if stored_pool and stored_pool.lower() != current_pool.lower():
                logger.warning("grid_state belongs to pool %s, current pool %s; discarding",
                               stored_pool, current_pool)
                return
            anchor = state.get("anchor")
            cfg_anchor = self.cfg["grid"]["anchor_price"]
            if anchor and cfg_anchor:
                ratio = anchor / cfg_anchor
                if ratio < 0.5 or ratio > 2.0:
                    logger.warning("saved anchor %.4f incompatible with pool anchor %.4f; discarding state",
                                   anchor, cfg_anchor)
                    return
            saved_spacing = state.get("spacing_pct", self.grid_cfg["spacing_pct"])
            if abs(saved_spacing - self.grid_cfg["spacing_pct"]) > 0.01:
                logger.warning("spacing changed %.1f%% -> %.1f%%; rebuilding grid",
                               saved_spacing, self.grid_cfg["spacing_pct"])
                return
            if anchor:
                self.grid = Grid(anchor=anchor, spacing_pct=self.grid_cfg["spacing_pct"],
                                 range_pct=self.grid_cfg["range_pct"])
            self._orders = {float(k): v for k, v in state.get("orders", {}).items()}
            self._last_price = float(state.get("last_price", self._last_price))
            self.peak_equity = float(state.get("peak_equity", 0.0))
            logger.info("grid state loaded: anchor=%s orders=%d", self.grid.anchor, len(self._orders))
        except Exception as e:
            logger.warning("could not load grid state: %s", e)

    def _save_state(self):
        state = {
            "anchor": self.grid.anchor,
            "orders": {str(k): v for k, v in self._orders.items()},
            "last_price": self._last_price,
            "peak_equity": self.peak_equity,
            "active_pool": self.cfg["pool"]["address"],
            "spacing_pct": self.grid_cfg["spacing_pct"],
        }
        self.store.set_meta("grid_state", json.dumps(state))

    def _reset_orders(self):
        self._orders = {}
        for lv in self.grid.buy_levels:
            self._orders[round(lv.price, 8)] = "buy"
        for lv in self.grid.sell_levels:
            self._orders[round(lv.price, 8)] = "sell"
        # Anchor-buy SOLO si no hay posición base (alineado con el backtest).
        # Evita comprar $0.50 extra fuera de estrategia en cada rebuild.
        try:
            base_bal = self.bot.token_balance(self.bot.base_token, self.account.address) / 10 ** self.bot.base_decimals
            base_usd = base_bal * self._last_price if self._last_price else 0.0
            if base_usd < 0.05:
                self._orders[round(self.grid.anchor, 8)] = "buy"
            else:
                logger.info("_reset_orders: hay posición base (~$%.4f), NO añado anchor-buy", base_usd)
        except Exception as e:
            logger.warning("_reset_orders: no pude leer balance base (%s); añado anchor-buy por defecto", e)
            self._orders[round(self.grid.anchor, 8)] = "buy"
        self._save_state()

    # ---- re-anchoring ----
    def _maybe_reanchor(self, price: float):
        drift = abs(price - self.grid.anchor) / self.grid.anchor * 100
        if drift > self.reanchor_pct:
            logger.warning("RE-ANCHORING: price %.2f drifted %.1f%% from anchor %.2f",
                           price, drift, self.grid.anchor)
            self.grid = Grid(anchor=price, spacing_pct=self.grid_cfg["spacing_pct"],
                             range_pct=self.grid_cfg["range_pct"])
            self._orders = {}
            self._reset_orders()
            send_alert(f"🔄 Re-anchor a ${price:.2f} (drift {drift:.1f}%)")

    # ---- account / balance ----
    def account_status(self) -> dict:
        b = self.bot
        return {
            "address": self.account.address,
            "eth": float(b.w3.from_wei(b.eth_balance(self.account.address), "ether")),
            "base": b.token_balance(b.base_token, self.account.address) / 10 ** b.base_decimals,
            "usdc": b.token_balance(b.usdc, self.account.address) / 10 ** b.quote_decimals,
        }

    # ---- rebalance ----
    def _maybe_rebalance(self, total: float):
        b = self.bot
        base_bal = b.token_balance(b.base_token, self.account.address) / 10 ** b.base_decimals
        usdc_bal = b.token_balance(b.usdc, self.account.address) / 10 ** b.quote_decimals
        if total <= 0:
            return
        base_usd = base_bal * self._last_price
        ratio = base_usd / total
        target = 0.5
        max_ratio = min(self.max_position_pct, 0.8)
        if ratio > target + self.rebalance_threshold:
            diff_usd = (ratio - target) * total
            if diff_usd > 0.05:
                base_amt = int(diff_usd / self._last_price * 10 ** b.base_decimals)
                r = b.swap_exact_in(b.base_token, b.usdc, base_amt,
                                    account=self.account, dry_run=self.dry_run)
                if r.ok and r.receipt_status == 1:
                    logger.info("REBALANCE sell base -> USDC: OK (gas=%s)", r.gas_used)
                    send_alert(f"⚖️ Rebalance base→USDC OK")
                else:
                    logger.error("REBALANCE sell base -> USDC: FAILED (%s)", r.message)
        elif ratio < target - self.rebalance_threshold:
            diff_usd = (target - ratio) * total
            if diff_usd > 0.05:
                usdc_amt = int(diff_usd * 10 ** b.quote_decimals)
                if (ratio + diff_usd / total) <= max_ratio:
                    r = b.swap_exact_in(b.usdc, b.base_token, usdc_amt,
                                        account=self.account, dry_run=self.dry_run)
                    if r.ok and r.receipt_status == 1:
                        logger.info("REBALANCE buy base <- USDC: OK (gas=%s)", r.gas_used)
                        send_alert(f"⚖️ Rebalance USDC→base OK")
                    else:
                        logger.error("REBALANCE buy base <- USDC: FAILED (%s)", r.message)
                else:
                    logger.info("rebalance buy skipped: would exceed max_position %.0f%%", max_ratio * 100)

    # ---- core: evaluate and trade ----
    def one_cycle(self) -> dict:
        price = fetch_price(self.cfg["pool"]["address"])
        if price is None:
            logger.warning("no price")
            return {"ok": False}
        self._last_price = price
        step = self.grid.spacing

        # check stop-loss
        base_usd, usdc_usd, total = _usd_value(self.bot, self.account)
        self.peak_equity = max(self.peak_equity, total)
        drawdown = (self.peak_equity - total) / self.peak_equity * 100 if self.peak_equity else 0
        logger.info("price=%.2f total=$%.4f (base=$%.4f usdc=$%.4f) dd=%.2f%%",
                    price, total, base_usd, usdc_usd, drawdown)

        self.store.set_meta("last_price", str(price))
        self.store.set_meta("last_total", f"{total:.6f}")
        self.store.set_meta("peak_equity", f"{self.peak_equity:.6f}")
        self.store.set_meta("drawdown_pct", f"{drawdown:.4f}")
        self._save_state()

        if drawdown >= self.stop_loss_pct:
            logger.warning("STOP-LOSS triggered: drawdown %.2f%% >= %.2f%%", drawdown, self.stop_loss_pct)
            send_alert(f"🛑 STOP-LOSS: drawdown {drawdown:.1f}% >= {self.stop_loss_pct}%")
            self._emergency_unwind()
            return {"ok": False, "stop_loss": True}

        self._maybe_reanchor(price)
        self._maybe_rebalance(total)
        self._maybe_rotate()

        # trigger pending buys — SOLO niveles del grid vigente (evita órdenes fantasma)
        valid_levels = {round(lv.price, 8) for lv in self.grid.buy_levels} | \
                       {round(lv.price, 8) for lv in self.grid.sell_levels} | \
                       {round(self.grid.anchor, 8)}
        # Poda de órdenes fantasma ANTES de evaluar fills: los niveles del grid
        # vigente (re-anchor o cambio de config) invalidan las órdenes viejas.
        for p in list(self._orders.keys()):
            if round(p, 8) not in valid_levels:
                logger.warning("orden fantasma %s@%.2f fuera del grid vigente -> descartada", self._orders[p], p)
                del self._orders[p]
        for p, side in list(self._orders.items()):
            if side == "buy" and p >= price:
                try:
                    filled = self._execute_buy(p, total=total)
                except RuntimeError as e:
                    # FIX G1: gas cap abortó el trade — registrar y continuar
                    logger.warning("BUY@%.2f skipped (gas cap): %s", p, e)
                    self.store.set_meta(
                        "gas_skip_count",
                        str(int(self.store.get_meta("gas_skip_count") or 0) + 1),
                    )
                    filled = False
                if filled:
                    del self._orders[p]
                    self._orders[round(p * (1 + step), 8)] = "sell"
                else:
                    logger.info("buy@%.2f failed/skipped: order retained", p)

        # trigger pending sells
        for p, side in list(self._orders.items()):
            if side == "sell" and p <= price:
                try:
                    filled = self._execute_sell(p, total=total)
                except RuntimeError as e:
                    # FIX G1: gas cap abortó el trade — registrar y continuar
                    logger.warning("SELL@%.2f skipped (gas cap): %s", p, e)
                    self.store.set_meta(
                        "gas_skip_count",
                        str(int(self.store.get_meta("gas_skip_count") or 0) + 1),
                    )
                    filled = False
                if filled:
                    del self._orders[p]
                    self._orders[round(p / (1 + step), 8)] = "buy"
                else:
                    logger.info("sell@%.2f failed/skipped: order retained", p)

        self._save_state()
        return {"ok": True, "price": price, "total": total}

    # ---- order execution ----
    def _order_size_base(self, total: float = 0.0) -> int:
        """Grid increment sized to min(available capital, grid increment).

        SECURITY: cap per-order size at 15% of current total equity AND at
        max_spend_usd/n, whichever is smaller. Prevents oversized orders when
        max_spend_usd is set larger than real capital.

        ALTO #1: 'total' is passed from one_cycle() to avoid re-fetching
        _usd_value() which would waste 3 RPCs per order.
        """
        n = len(self.grid.levels)
        step_usd = self.max_spend_usd / n
        step_usd = min(step_usd, self.max_spend_usd * 0.25)
        # Hard cap: never more than 15% of current total equity per order
        if total > 0:
            step_usd = min(step_usd, total * 0.15)
        else:
            # Fallback: if caller did not provide total, fetch it (legacy path)
            try:
                _, _, total = _usd_value(self.bot, self.account)
                if total > 0:
                    step_usd = min(step_usd, total * 0.15)
                else:
                    step_usd = min(step_usd, self.max_spend_usd * 0.05)
            except Exception:
                # If equity fetch fails, use a conservative cap (5% of max_spend)
                step_usd = min(step_usd, self.max_spend_usd * 0.05)
                logger.warning("equity fetch failed in order sizing; using conservative cap")
        if not self._last_price or self._last_price <= 0:
            # Sin precio (fetch falló o arranque sin tick): no podemos convertir
            # USD -> base. Devolver 0 -> el caller skipea la orden (no revienta).
            logger.warning("order sizing skipped: no valid price (%.4f)", self._last_price)
            return 0
        return int(step_usd / self._last_price * 10 ** self.bot.base_decimals)

    def _execute_buy(self, level_price: float, total: float = 0.0) -> bool:
        b = self.bot
        usdc_bal_raw = b.token_balance(b.usdc, self.account.address)
        usdc_bal = usdc_bal_raw / 10 ** b.quote_decimals
        if usdc_bal < 0.05:
            logger.info("buy@%.2f skip: insufficient USDC (%.4f)", level_price, usdc_bal)
            return False
        base_need = self._order_size_base(total)
        usdc_needed_raw = int(base_need * level_price / 10 ** b.base_decimals * 10 ** b.quote_decimals)
        # FIX BAJA #6: reuse usdc_bal_raw instead of second RPC call
        usdc_max = min(usdc_needed_raw, usdc_bal_raw)
        usdc_max_usd = usdc_max / 10 ** b.quote_decimals
        # Idea Hummingbot (min_order_amount_quote): no ejecutar órdenes tan pequeñas
        # que las fees/gas se las coman. Umbral configurable (min_order_usd).
        min_order_usd = self.live_cfg.get("min_order_usd", 0.25)
        if usdc_max_usd < min_order_usd:
            logger.info("buy@%.2f skip: order $%.4f < min_order_usd $%.2f", level_price, usdc_max_usd, min_order_usd)
            return False
        if usdc_max < 0.05 * 10 ** b.quote_decimals:
            logger.info("buy@%.2f skip: order too small", level_price)
            return False
        logger.info("BUY base @ %.2f (swap %s USDC -> base)", level_price, usdc_max_usd)
        # min_out anclado al NIVEL de grid esperado (no al quote vivo): si el pool
        # se movió y el precio real está >= 5-25% peor, la tx revierte en cadena en
        # lugar de comprar a precio de mercado. +1% de margen sobre el nivel (la
        # compra en grid se da cuando price cruza el nivel hacia abajo; el precio
        # real puede estar ligeramente por debajo).
        min_out_override = int(usdc_max / (level_price * 1.01) * 10 ** b.base_decimals)
        r = b.swap_exact_in(b.usdc, b.base_token, usdc_max,
                            account=self.account, dry_run=self.dry_run,
                            min_out_override=min_out_override)
        logger.info("BUY result: %s", r.message)
        self._record_trade("buy", level_price, usdc_max / 10 ** b.quote_decimals, r)
        filled = bool(r.ok and r.receipt_status == 1)
        if r.ok:
            send_alert(f"🟢 COMPRA base @ ${level_price:.2f} ({r.message})")
        else:
            send_alert(f"🔴 FALLO COMPRA @ ${level_price:.2f}: {r.message}")
        return filled

    def _execute_sell(self, level_price: float, total: float = 0.0) -> bool:
        b = self.bot
        min_base = 0.05 / self._last_price * 10 ** b.base_decimals
        base_bal = b.token_balance(b.base_token, self.account.address)
        if base_bal < min_base:
            logger.info("sell@%.2f skip: insufficient base", level_price)
            return False
        base_amt = min(self._order_size_base(total), base_bal)
        if base_amt < min_base:
            logger.info("sell@%.2f skip: order too small", level_price)
            return False
        # Idea Hummingbot (min_order_amount_quote): no vender cantidades tan
        # pequeñas que las fees/gas se las coman.
        min_order_usd = self.live_cfg.get("min_order_usd", 0.25)
        sell_usd = base_amt / 10 ** b.base_decimals * self._last_price
        if sell_usd < min_order_usd:
            logger.info("sell@%.2f skip: order $%.4f < min_order_usd $%.2f", level_price, sell_usd, min_order_usd)
            return False
        logger.info("SELL base @ %.2f (swap %s base -> USDC)", level_price,
                    base_amt / 10 ** b.base_decimals)
        # min_out anclado al NIVEL de grid esperado: se vende al nivel del grid,
        # no al precio de mercado del momento. -1% de margen sobre el nivel (la
        # venta en grid se da cuando price cruza el nivel hacia arriba; el precio
        # real puede estar ligeramente por encima).
        min_out_override = int(base_amt * (level_price * 0.99) / 10 ** b.base_decimals * 10 ** b.quote_decimals)
        r = b.swap_exact_in(b.base_token, b.usdc, base_amt,
                            account=self.account, dry_run=self.dry_run,
                            min_out_override=min_out_override)
        logger.info("SELL result: %s", r.message)
        self._record_trade("sell", level_price,
                           base_amt / 10 ** b.base_decimals * level_price, r)
        filled = bool(r.ok and r.receipt_status == 1)
        if r.ok:
            send_alert(f"🟡 VENTA base @ ${level_price:.2f} ({r.message})")
        else:
            send_alert(f"🔴 FALLO VENTA @ ${level_price:.2f}: {r.message}")
        return filled

    def _record_trade(self, side: str, level_price: float, size_usd: float, r):
        filled = 1 if r.ok and r.receipt_status == 1 else 0
        gas_price_wei = self.bot.w3.eth.gas_price if r.gas_used else 0
        gas_eth = (r.gas_used * gas_price_wei / 1e18) if r.gas_used else 0.0
        # Convert gas ETH to USD using last known price
        gas_usd = gas_eth * self._last_price if self._last_price else 0.0
        self.store.record_trade(
            ts=_now(), side=side, price=level_price, size_usd=size_usd,
            fee_usd=0.0, gas_usd=gas_usd,
            filled=filled,
            tx_hash=r.tx_hash, gas_used=r.gas_used,
        )

    # ---- stop-loss ----
    def _emergency_unwind(self):
        logger.warning("EMERGENCY UNWIND: selling all base -> USDC")
        unwound = False
        b = self.bot
        for attempt in range(3):
            try:
                base_bal = b.token_balance(b.base_token, self.account.address)
                if base_bal <= 0:
                    unwound = True
                    break
                r = b.swap_exact_in(b.base_token, b.usdc, base_bal,
                                    account=self.account, dry_run=self.dry_run)
                logger.warning("unwind attempt %d result: %s", attempt + 1, r.message)
                if r.ok and r.receipt_status == 1:
                    unwound = True
                    send_alert(f"🛑 Unwind completado (intento {attempt + 1}): {r.message}")
                    break
                time.sleep(3)
            except Exception as e:
                logger.error("unwind attempt %d failed: %s", attempt + 1, e)
                time.sleep(3)
        if not unwound:
            logger.error("UNWIND FAILED after 3 attempts: still holding base token!")
            send_alert("🚨 UNWIND FALLÓ: aún con posición en el token. Revisión manual.")
        # FIX C1: write halted=true BEFORE stopping so bot.py reads it on
        # restart (systemd Restart=always) and exits without trading.
        self.store.set_meta("halted", "true")
        # FIX N10: limpiar órdenes pendientes para evitar re-ejecución al reiniciar
        self._orders.clear()
        self.running = False
        self.store.set_meta("status", "stopped")

    # ---- asset rotation ----
    def _maybe_rotate(self):
        if not self.rotation_enabled:
            return
        now = time.time()
        if now - self._last_scan < self.rotation_interval_h * 3600:
            return
        self._last_scan = now
        try:
            import threading
            from paperbot.live.rotator import AssetRotator
            rotator = AssetRotator(min_vol_ratio=self.min_vol_ratio)
            current_pool = self.cfg["pool"]["address"]
            current_symbol = self.cfg["pool"]["base_token"]
            cur_vol = None
            try:
                v = rotator.volatility(current_pool)
                if v:
                    cur_vol = v[0]
            except Exception:
                pass
            result = [None]
            error = [None]
            cancel_event = threading.Event()
            def _run():
                try:
                    if not cancel_event.is_set():
                        result[0] = rotator.evaluate(current_pool, current_symbol, cur_vol or 0.0)
                except Exception as e:
                    error[0] = e
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=90)
            if t.is_alive():
                cancel_event.set()
                logger.warning("rotation scan timed out; skipping")
                return
            if error[0]:
                raise error[0]
            candidate = result[0]
            if candidate:
                logger.warning("ROTATE: %s (vol %.2f%%) beats %s (vol %.2f%%)",
                               candidate.symbol, candidate.volatility_daily * 100,
                               current_symbol, (cur_vol or 0) * 100)
                send_alert(f"🔄 Rotación: {candidate.symbol} vol "
                           f"{candidate.volatility_daily:.1%} > {current_symbol} "
                           f"{(cur_vol or 0):.1%}")
                self._migrate_asset(candidate)
        except Exception as e:
            logger.error("rotation scan failed: %s", e)

    def _migrate_asset(self, cand):
        from paperbot.live.rotator import AssetRotator
        rotator = AssetRotator(min_vol_ratio=self.min_vol_ratio)
        if cand.liquidity_usd < rotator.min_liquidity or cand.volume_usd_24h < rotator.min_volume:
            logger.error("MIGRATE aborted: candidate fails safety checks (liq=$%.0f vol=$%.0f)",
                         cand.liquidity_usd, cand.volume_usd_24h)
            send_alert(f"🚨 Migración abortada por guardarraíles: {cand.symbol}")
            return

        b = self.bot
        base_bal = b.token_balance(b.base_token, self.account.address)
        logger.info("MIGRATE: selling %s base %s -> USDC", base_bal / 10 ** b.base_decimals,
                    self.cfg["pool"]["base_token"])
        if base_bal > 0:
            r = b.swap_exact_in(b.base_token, b.usdc, base_bal,
                                account=self.account, dry_run=self.dry_run)
            logger.info("migrate sell result: %s", r.message)
            send_alert(f"🔄 Venta {self.cfg['pool']['base_token']} → USDC: {r.message}")
            if not (r.ok and r.receipt_status == 1):
                logger.error("MIGRATE aborted: exit trade failed")
                return

        # Invalidate USD cache BEFORE pool config change to prevent stale price
        global USD_PER_BASE
        USD_PER_BASE = None

        self._update_pool_config(cand)

        self.cfg = load_config()
        self.live_cfg = self.cfg["live"]
        self.grid_cfg = self.cfg["grid"]
        self.bot = AerodromeLive()
        self.bot.invalidate_verification_cache()  # BAJA #30: force re-verify for new pool
        self.account = self.bot.get_account()
        self.store.set_meta("active_asset", cand.symbol)
        self.store.set_meta("active_pool", cand.pool_address)
        self.grid = Grid(anchor=cand.price_usd,
                         spacing_pct=self.grid_cfg["spacing_pct"],
                         range_pct=self.grid_cfg["range_pct"])
        self._orders = {}
        self._reset_orders()
        self.peak_equity = 0.0
        self._last_price = cand.price_usd
        send_alert(f"🆕 Nuevo activo: {cand.symbol} @ ${cand.price_usd:.4f} "
                   f"(liq ${cand.liquidity_usd:,.0f}, vol ${cand.volume_usd_24h:,.0f})")

    def _update_pool_config(self, cand):
        from paperbot.live.rotator import resolve_router_for_pool
        routing = resolve_router_for_pool(cand.pool_address, w3=self.bot.w3)
        if not routing:
            raise RuntimeError(f"cannot resolve router for pool {cand.pool_address}")
        cfg_data = load_config()
        cfg_data["pool"]["address"] = cand.pool_address
        cfg_data["pool"]["base_token"] = cand.symbol
        cfg_data["pool"]["base_token_address"] = cand.base_token
        cfg_data["pool"]["quote_token_address"] = cand.quote_token
        cfg_data["pool"]["fee_pct"] = routing["pool_fee"] / 10000
        cfg_data["grid"]["anchor_price"] = cand.price_usd
        cfg_data["live"]["router_address"] = routing["router"]
        cfg_data["live"]["quoter_address"] = routing["quoter"]
        cfg_data["live"]["verified_factory"] = routing["verified_factory"]
        cfg_data["live"]["tick_spacing"] = routing["tick_spacing"]
        cfg_data["live"]["pool_fee"] = routing["pool_fee"]
        import yaml
        from paperbot.config import CONFIG_PATH
        tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
        with open(tmp_path, "w") as f:
            yaml.safe_dump(cfg_data, f, sort_keys=False)
        tmp_path.replace(CONFIG_PATH)
        logger.info("config.yaml updated (atomic write) for %s", cand.symbol)

    # ---- loop ----
    def _reconcile_onchain(self):
        """Reconcilia el estado de órdenes con los balances reales on-chain al arrancar.

        Cura la falta de idempotencia de fill: si el bot murió justo después de enviar
        un swap (tx minada pero estado no persistido), al reiniciar NO debe re-ejecutar.
        Estrategia: reconstruir el grid desde los balances reales — si ya no hay USDC
        para la orden de compra pendiente, o no hay base para la venta pendiente, la
        orden se considera ejecutada y se rota al nivel contrario.
        """
        try:
            b = self.bot
            base_bal = b.token_balance(b.base_token, self.account.address) / 10 ** b.base_decimals
            usdc_bal = b.token_balance(b.usdc, self.account.address) / 10 ** b.quote_decimals
            price = self._last_price
            if not price:
                return
            base_usd = base_bal * price
            logger.info("RECONCILE on-chain: base=%.6f (~$%.4f) usdc=%.4f",
                        base_bal, base_usd, usdc_bal)
            # Órdenes de compra pendientes: si no hay USDC para cubrir la menor, ya se ejecutó
            pending_buys = [p for p, s in self._orders.items() if s == "buy"]
            if pending_buys and usdc_bal < 0.05:
                # Sin USDC -> todas las compras pendientes se consideran ejecutadas -> rotar a venta
                for p in pending_buys:
                    del self._orders[p]
                    self._orders[round(p * (1 + self.grid.spacing), 8)] = "sell"
                logger.warning("RECONCILE: sin USDC -> %d compras pendientes rotadas a venta (fill no registrado)",
                               len(pending_buys))
            # Órdenes de venta pendientes: si no hay base, ya se vendió -> rotar a compra
            pending_sells = [p for p, s in self._orders.items() if s == "sell"]
            if pending_sells and base_bal < 0.0001:
                for p in pending_sells:
                    del self._orders[p]
                    self._orders[round(p / (1 + self.grid.spacing), 8)] = "buy"
                logger.warning("RECONCILE: sin base -> %d ventas pendientes rotadas a compra (fill no registrado)",
                               len(pending_sells))
            self._save_state()
        except Exception as e:
            logger.error("RECONCILE falló (no bloquea): %s", e)

    def run(self):
        logger.info("LIVE grid trader started: poll=%ss dry_run=%s stop_loss=%.1f%% addr=%s",
                    self.poll_seconds, self.dry_run, self.stop_loss_pct, self.account.address)
        self.store.set_meta("status", "running")
        self._reconcile_onchain()
        while self.running:
            try:
                self.one_cycle()
            except Exception as e:
                logger.exception("cycle error: %s", e)
            time.sleep(self.poll_seconds)
        logger.warning("Trader stopped (running=False)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
