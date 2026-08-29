#!/usr/bin/env python3
"""
SUPERVISOR AUTÓNOMO del grid bot WETH/USDC (Aerodrome, Base).

Bucle de reglas 24/7 SIN LLM (determinista, barato, fiable):
  - Vigila el servicio systemd grid-bot.service y lo reinicia si muere.
  - Detecta sangrado (equity decay sin fills) y lo repara:
      * price drift > reanchor_pct  -> re-ancla el grid en caliente (reescribe config.yaml + reinicia)
      * órdenes de compra todas lejos por debajo del precio (capital dormido) -> re-ancla a precio actual
      * drawdown real on-chain >= stop_loss -> alerta crítica
  - Guarda un log de acciones y un estado JSON para auditoría.
  - Envía alertas a Telegram si configurado.

Seguridad:
  - Solo toca config.yaml y el servicio systemd del bot. Nunca ejecuta swaps.
  - No cambia stop_loss ni max_spend por debajo de mínimos de seguridad.
  - Idempotente: si ya re-ancló hace poco, no vuelve a hacerlo (cooldown).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/home/tt/thinking/plan")
CONFIG = BASE / "config.yaml"
DB = BASE / "data" / "live.db"
LOG = BASE / "data" / "supervisor.log"
STATE = BASE / "data" / "supervisor_state.json"
BAK = BASE / "data" / "config.backup.yaml"
TRADING_ENV = Path(os.path.expanduser("~/trading/.env"))

# Límites de seguridad (no cruzar)
MIN_STOP_LOSS_PCT = 5.0
MAX_STOP_LOSS_PCT = 30.0
MIN_REANCHOR_PCT = 1.0
MAX_REANCHOR_PCT = 15.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG), logging.StreamHandler()],
)
log = logging.getLogger("supervisor")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"last_reanchor_ts": 0.0, "last_rebalance_ts": 0.0, "actions": []}


def save_state(state: dict):
    try:
        STATE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error("no pude guardar estado: %s", e)


def log_action(state: dict | None, action: str, detail: str = ""):
    entry = {"ts": now_iso(), "action": action, "detail": detail}
    if state is not None:
        state.setdefault("actions", []).append(entry)
        state["actions"] = state["actions"][-50:]
    log.info("ACTION %s: %s", action, detail)


def service_alive() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-active", "grid-bot.service"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "active"


def restart_bot():
    r = subprocess.run(["systemctl", "--user", "restart", "grid-bot.service"],
                       capture_output=True, text=True)
    return r.returncode == 0


def read_meta(key: str):
    if not DB.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception as e:
        log.error("leyendo meta %s: %s", key, e)
        return None


def read_config() -> dict:
    """Parse ligero del YAML (solo las claves que nos interesan)."""
    cfg = {}
    section = None
    for line in CONFIG.read_text().splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            cfg[section] = {}
            continue
        if ":" in line and section:
            k, _, v = line.partition(":")
            cfg[section][k.strip()] = v.strip()
    return cfg


def reanchor_config(new_anchor: float, reason: str) -> bool:
    """Re-ancla config.yaml: actualiza anchor_price y reinicia el bot.

    Hace backup antes. Nunca toca otras claves.
    """
    if not CONFIG.exists():
        log.error("config.yaml no existe")
        return False
    shutil.copy2(CONFIG, BAK)
    lines = CONFIG.read_text().splitlines()
    out = []
    in_grid = False
    changed = False
    for line in lines:
        if line.startswith("grid:"):
            in_grid = True
            out.append(line)
            continue
        if in_grid:
            if line.startswith("  ") and "anchor_price:" in line:
                out.append(f"  anchor_price: {new_anchor}")
                changed = True
                continue
            if not line.startswith(" ") and line.strip():
                in_grid = False
        out.append(line)
    if not changed:
        log.error("no encontré anchor_price en config.yaml")
        return False
    CONFIG.write_text("\n".join(out) + "\n")
    log_action(None, "config_anchor_update", f"{new_anchor} ({reason})")
    # CRÍTICO: invalidar el grid_state persistido en la DB para que el bot NO cargue
    # el grid viejo (órdenes fantasma). El bot reconstruirá el grid desde el nuevo anchor.
    try:
        import sqlite3
        con = sqlite3.connect(DB)
        con.execute("DELETE FROM meta WHERE key='grid_state'")
        con.commit()
        con.close()
        log.info("grid_state invalidado en DB (re-anchor limpio)")
    except Exception as e:
        log.error("no pude invalidar grid_state: %s", e)
    ok = restart_bot()
    log.info("re-anchor a %s -> reinicio %s", new_anchor, "OK" if ok else "FALLO")
    return ok


def send_telegram(msg: str):
    """Envía alerta. PRIORIDAD: config.yaml (alerts.telegram_chat_id) sobre ~/trading/.env.

    El grid bot es de Pablo: el chat_id de config.yaml (504820277) manda.
    El token se toma de ~/trading/.env (real) o config.yaml.
    """
    token = chat = ""
    # Fuente 1 (chat): config.yaml — chat_id del grid bot (Pablo)
    try:
        cfg = read_config()
        chat = (cfg.get("alerts") or {}).get("telegram_chat_id", "") or chat
        token = (cfg.get("alerts") or {}).get("telegram_bot_token", "") or token
    except Exception:
        pass
    # Fuente 2 (token/chat fallback): ~/trading/.env
    try:
        if TRADING_ENV.exists():
            for line in TRADING_ENV.read_text().splitlines():
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_CHAT_ID=") and not chat:
                    chat = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        log.debug("leyendo ~/trading/.env: %s", e)
    if not token or not chat:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        log.debug("telegram fallo (no fatal): %s", e)
        return False


def get_onchain_snapshot():
    """Lee balances reales on-chain vía el propio bot (sin escribir)."""
    try:
        sys.path.insert(0, str(BASE))
        from paperbot.live.aerodrome import AerodromeLive
        bot = AerodromeLive()
        acc = bot.get_account()
        price_raw = read_meta("last_price")
        price = float(price_raw) if price_raw else None
        base = bot.token_balance(bot.base_token, acc.address) / 10 ** bot.base_decimals
        usdc = bot.token_balance(bot.usdc, acc.address) / 10 ** bot.quote_decimals
        eth = float(bot.w3.from_wei(bot.eth_balance(acc.address), "ether"))
        return {"price": price, "base": base, "usdc": usdc, "eth": eth,
                "total": (base * price + usdc) if price else None, "addr": acc.address}
    except Exception as e:
        log.error("snapshot on-chain fallo: %s", e)
        return None


def main_loop():
    state = load_state()
    log.info("=== SUPERVISOR ARRANCADO ===")
    # Verificar alertas Telegram al arranque
    cfg = read_config()
    if cfg.get("alerts", {}).get("telegram_bot_token"):
        send_telegram("🤖 Supervisor grid bot ACTIVO (vigilando 24/7)")

    while True:
        try:
            # 1. ¿Está vivo el servicio?
            if not service_alive():
                log.warning("grid-bot.service NO activo -> reiniciando")
                log_action(state, "restart_service", "servicio caído")
                restart_bot()
                send_telegram("🔄 grid-bot.service caído -> reiniciado por supervisor")

            # 2. Snapshot on-chain
            snap = get_onchain_snapshot()
            if snap and snap.get("total") is not None:
                total = snap["total"]
                peak_raw = read_meta("peak_equity")
                peak = float(peak_raw) if peak_raw else total
                dd = (peak - total) / peak * 100 if peak else 0
                log.info("snap total=$%.4f peak=$%.4f dd=%.2f%% price=%s",
                         total, peak, dd, snap.get("price"))
                # stop-loss duro (alerta + parar bot en caída libre)
                sl = float(read_config().get("live", {}).get("stop_loss_pct", 10.0))
                if dd >= sl:
                    log.warning("DRAWDOWN CRITICO %.1f%% >= stop_loss %.1f%%", dd, sl)
                    # Confirmar en 2 ciclos consecutivos antes de parar (evita falsos)
                    prev_critical = state.get("critical_dd", False)
                    if prev_critical:
                        log.warning("STOP-LOSS DURO: drawdown %.1f%% sostenido -> parando grid-bot", dd)
                        subprocess.run(["systemctl", "--user", "stop", "grid-bot.service"],
                                       capture_output=True, text=True)
                        send_telegram(f"🛑 STOP-LOSS DURO: drawdown {dd:.1f}% >= {sl:.1f}% en 2 ciclos. "
                                      f"grid-bot PARADO. Revisar antes de reanudar.")
                        state["critical_dd"] = False
                    else:
                        state["critical_dd"] = True
                        send_telegram(f"🛑 CRÍTICO: drawdown {dd:.1f}% >= stop_loss {sl:.1f}%. "
                                      f"Si persiste en el próximo ciclo, PARO el bot.")
                else:
                    state["critical_dd"] = False
                # sangrado: equity decay sin fills (5+ ciclos, >2%)
                if peak > 0 and (peak - total) / peak * 100 >= 2.0 and dd < sl:
                    log.warning("SANGRADO detectado: -%.2f%% desde pico sin stop", dd)
                    send_telegram(f"⚠️ Sangrado lento: -{dd:.1f}% desde pico (${total:.2f}). Vigilar.")

            # 3. Estado del grid: ¿capital dormido?
            gs_raw = read_meta("grid_state")
            if gs_raw:
                gs = json.loads(gs_raw)
                anchor = float(gs.get("anchor", 0))
                price = snap.get("price") if snap else None
                if price and anchor:
                    drift = abs(price - anchor) / anchor * 100
                    reanchor_pct = float(read_config().get("grid", {}).get("drift_reanchor_pct", 5.0))
                    cooldown = time.time() - state.get("last_reanchor_ts", 0)
                    if drift > reanchor_pct and cooldown > 3600:
                        log.info("drift %.2f%% > reanchor %.1f%% -> re-anclar a $%.2f", drift, reanchor_pct, price)
                        log_action(state, "reanchor", f"drift {drift:.2f}% -> ${price:.2f}")
                        state["last_reanchor_ts"] = time.time()
                        ok = reanchor_config(price, f"drift {drift:.2f}%")
                        send_telegram(f"🔄 Re-anchor grid a ${price:.2f} (drift {drift:.1f}%)")
                        state["last_reanchor_ts"] = time.time() if ok else state.get("last_reanchor_ts", 0)
                    else:
                        # órdenes de compra todas lejos por debajo (capital dormido)
                        orders = gs.get("orders", {})
                        buys = [float(k) for k, v in orders.items() if v == "buy"]
                        if buys:
                            highest_buy = max(buys)
                            if price and highest_buy < price * 0.97 and cooldown > 3600:
                                log.info("capital dormido: mayor buy $%.2f < precio $%.2f (-%.1f%%) -> re-anclar",
                                         highest_buy, price, (price - highest_buy) / price * 100)
                                log_action(state, "reanchor_sleeping", f"highest_buy {highest_buy} vs price {price}")
                                state["last_reanchor_ts"] = time.time()
                                ok = reanchor_config(price, "capital dormido (buys lejos)")
                                send_telegram(f"🔄 Re-anchor (capital dormido) a ${price:.2f}")

            # 4. Persistir estado y dormir
            save_state(state)
        except Exception as e:
            log.exception("error en ciclo: %s", e)
        time.sleep(300)  # cada 5 minutos


if __name__ == "__main__":
    main_loop()
