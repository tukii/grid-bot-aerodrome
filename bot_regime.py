#!/usr/bin/env python3
"""
BOT DE RÉGIMEN (reemplaza al grid para capital pequeño).

Estrategia probada por los datos (periodo 41d: BUY&HOLD WETH +29.3% vs grid +0.67%):
- En TENDENCIA ALCISTA fuerte (>+3% en 24h o >+8% en 7d): mantener WETH (captura el rally)
- En TENDENCIA BAJISTA fuerte (<-3% en 24h o <-8% en 7d): pasar a USDC (protege)
- En LATERAL: mantener lo que haya (sin operar = sin gas)

CERO gas en hold: solo hace swaps cuando CAMBIA el régimen (1 tx cada varios días).
A diferencia del grid, no quema gas en approves/órdenes que nunca rotan.

Uso: bot_regime.py --once  (una pasada, para cron/supervisor)
      bot_regime.py --loop  (bucle continuo, para systemd)
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

BASE = Path("/home/tt/thinking/plan")
sys.path.insert(0, str(BASE))

from paperbot.config import load_config  # noqa: E402
from paperbot.data.price import fetch_price  # noqa: E402
from paperbot.live.aerodrome import AerodromeLive  # noqa: E402
from paperbot.live.alerts import send_alert  # noqa: E402

LOG = BASE / "data" / "regime.log"
STATE = BASE / "data" / "regime_state.json"

# Umbrales de régimen (configurables)
TREND_24H_BULL = 3.0    # +3% en 24h -> alcista
TREND_24H_BEAR = -3.0   # -3% en 24h -> bajista
TREND_7D_BULL = 8.0     # +8% en 7d -> alcista
TREND_7D_BEAR = -8.0    # -8% en 7d -> bajista

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
log = logging.getLogger("regime")


def load_state() -> dict:
    if STATE.exists():
        try:
            import json
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"regime": "unknown", "last_switch_ts": 0.0, "switches": []}


def save_state(state: dict):
    import json
    try:
        STATE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error("no pude guardar estado: %s", e)


def get_trend() -> dict:
    """Tendencia desde el log de precios (últimas ~500 líneas ≈ 24h+)."""
    prices = []
    try:
        for line in (BASE / "data" / "live.log").read_text().splitlines()[-600:]:
            import re
            m = re.search(r"price=([0-9.]+)", line)
            if m:
                prices.append(float(m.group(1)))
    except Exception as e:
        log.error("leyendo log: %s", e)
    if len(prices) < 24:
        return {"trend_24h": 0.0, "trend_7d": 0.0, "n": len(prices)}
    t24 = (prices[-1] - prices[-24]) / prices[-24] * 100
    t7d = (prices[-1] - prices[0]) / prices[0] * 100 if len(prices) >= 168 else t24
    return {"trend_24h": t24, "trend_7d": t7d, "n": len(prices)}


def classify_regime(trend: dict) -> str:
    t24, t7d = trend["trend_24h"], trend["trend_7d"]
    if t24 >= TREND_24H_BULL or t7d >= TREND_7D_BULL:
        return "bull"
    if t24 <= TREND_24H_BEAR or t7d <= TREND_7D_BEAR:
        return "bear"
    return "sideways"


def run_once(dry_run: bool = False):
    state = load_state()
    trend = get_trend()
    regime = classify_regime(trend)
    log.info("régimen=%s tendencia_24h=%+.2f%% tendencia_7d=%+.2f%% (n=%d)",
             regime, trend["trend_24h"], trend["trend_7d"], trend["n"])

    # Leer posición on-chain real
    bot = AerodromeLive()
    acc = bot.get_account()
    base = bot.token_balance(bot.base_token, acc.address) / 10 ** bot.base_decimals
    usdc = bot.token_balance(bot.usdc, acc.address) / 10 ** bot.quote_decimals
    price = fetch_price(bot.cfg["pool"]["address"]) or 2450.0
    total = base * price + usdc
    base_pct = base * price / total * 100 if total else 0
    log.info("posición: base=%.4f WETH (~$%.2f, %.0f%%) usdc=$%.2f total=$%.2f",
             base, base * price, base_pct, usdc, total)

    old_regime = state.get("regime", "unknown")
    action = "hold"
    detail = ""

    if regime == "bull" and base_pct < 80:
        # Comprar WETH con el USDC (capturar rally)
        usdc_to_swap = usdc * 0.9
        if usdc_to_swap > 0.5:  # mínimo para que el gas valga la pena
            action = "buy_weth"
            detail = f"cambio bull: comprar ~${usdc_to_swap:.2f} WETH"
            if not dry_run:
                r = bot.swap_exact_in(bot.usdc, bot.base_token,
                                      int(usdc_to_swap * 10 ** bot.quote_decimals),
                                      account=acc, dry_run=False)
                log.info("BUY WETH: %s", r.message)
                if r.ok:
                    send_alert(f"🐂 RÉGIMEN BULL: comprado WETH con ${usdc_to_swap:.2f} ({r.message})")
                    state["switches"].append({"ts": time.time(), "to": "bull", "usd": usdc_to_swap})
                    state["last_switch_ts"] = time.time()
        else:
            detail = f"bull pero USDC insuficiente (${usdc:.2f}) para swap rentable"
    elif regime == "bear" and base_pct > 20:
        # Vender WETH a USDC (proteger)
        base_to_sell = base * 0.9
        if base_to_sell * price > 0.5:
            action = "sell_weth"
            detail = f"cambio bear: vender {base_to_sell:.6f} WETH (~${base_to_sell*price:.2f})"
            if not dry_run:
                r = bot.swap_exact_in(bot.base_token, bot.usdc,
                                      int(base_to_sell * 10 ** bot.base_decimals),
                                      account=acc, dry_run=False)
                log.info("SELL WETH: %s", r.message)
                if r.ok:
                    send_alert(f"🐻 RÉGIMEN BEAR: vendido WETH a USDC ({r.message})")
                    state["switches"].append({"ts": time.time(), "to": "bear", "usd": base_to_sell * price})
                    state["last_switch_ts"] = time.time()
        else:
            detail = f"bear pero WETH insuficiente para swap rentable"
    else:
        detail = f"hold: régimen {regime} con {base_pct:.0f}% WETH (sin operar = sin gas)"

    state["regime"] = regime
    save_state(state)
    log.info("acción: %s | %s", action, detail)
    return {"regime": regime, "action": action, "detail": detail, "total": total}


def main():
    parser = argparse.ArgumentParser(prog="bot_regime")
    parser.add_argument("--once", action="store_true", help="una pasada")
    parser.add_argument("--loop", action="store_true", help="bucle continuo (systemd)")
    parser.add_argument("--dry-run", action="store_true", help="no enviar txs")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                run_once(args.dry_run)
            except Exception as e:
                log.exception("error en pasada: %s", e)
            time.sleep(3600)  # cada hora
    else:
        run_once(args.dry_run)


if __name__ == "__main__":
    main()
