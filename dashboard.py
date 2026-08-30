#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard SSR (server-side rendered, SIN JavaScript) para el Grid Bot WETH/USDC.
Lee SOLO datos (data/live.db + logs). NO toca config.yaml ni el bot.

Servidor: ThreadingHTTPServer, puerto 8899 (arg --port).
Auto-refresh: <meta http-equiv="refresh" content="60">
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import html as html_mod
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "live.db")
LIVE_LOG = os.path.join(BASE_DIR, "data", "live.log")
SUP_LOG = os.path.join(BASE_DIR, "data", "supervisor.log")
REFRESH_SEC = 60
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

# Cache corto para `systemctl is-active` (evita lanzar subprocesos en cada render)
_unit_cache = {"t": 0.0, "data": {}}


def now_local():
    return datetime.now().astimezone()


def parse_ts(s):
    """ISO con +00:00 -> datetime local. Fallback: raw."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").astimezone()
        except Exception:
            return None


def unit_active(name, ttl=10):
    """grid-bot.service / grid-supervisor.service -> 'active'|'inactive'|..."""
    now = time.time()
    if now - _unit_cache["t"] < ttl and name in _unit_cache["data"]:
        return _unit_cache["data"][name]
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        st = r.stdout.strip() or "unknown"
    except Exception:
        st = "unknown"
    _unit_cache["data"][name] = st
    _unit_cache["t"] = now
    return st


def db_conn():
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def read_meta():
    out = {}
    try:
        con = db_conn()
        try:
            for k, v in con.execute("SELECT key, value FROM meta"):
                out[k] = v
        finally:
            con.close()
    except Exception:
        pass
    return out


def read_trades(limit=15):
    """Últimos trades con estado: filled=1 -> OK, filled=0 -> REJ."""
    rows = []
    try:
        con = db_conn()
        try:
            cur = con.execute(
                "SELECT id, ts, side, price, size_usd, fee_usd, gas_usd, filled, tx_hash "
                "FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "ts": parse_ts(r[1]), "side": r[2],
                    "price": r[3], "size_usd": r[4], "fee_usd": r[5],
                    "gas_usd": r[6], "filled": r[7], "tx": r[8],
                })
        finally:
            con.close()
    except Exception:
        pass
    return rows


def trade_stats():
    st = {"n": 0, "ok": 0, "rej": 0, "buys": 0, "sells": 0, "fees": 0.0}
    try:
        con = db_conn()
        try:
            row = con.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN filled=1 THEN 1 ELSE 0 END) ok, "
                "SUM(CASE WHEN filled=0 THEN 1 ELSE 0 END) rej, "
                "SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) buys, "
                "SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) sells, "
                "COALESCE(SUM(COALESCE(fee_usd,0)+COALESCE(gas_usd,0)),0) fees "
                "FROM trades").fetchone()
            if row:
                st = {
                    "n": row[0] or 0, "ok": row[1] or 0, "rej": row[2] or 0,
                    "buys": row[3] or 0, "sells": row[4] or 0, "fees": row[5] or 0.0,
                }
        finally:
            con.close()
    except Exception:
        pass
    return st


def log_tail(path, n=14):
    """Últimas n líneas de un log (tail eficiente)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and len(data) < 64 * 1024:
                size = max(0, size - block)
                f.seek(size)
                data = f.read(block) + data
            lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def live_log_freshness():
    """Segundos desde la última línea de live.log (o -1 si no existe)."""
    lines = log_tail(LIVE_LOG, 3)
    if not lines:
        return -1
    last = parse_ts(lines[-1][:23].strip())
    if last is None:
        return -1
    return (now_local() - last).total_seconds()


def grid_levels(gs):
    """Devuelve (anchor, buys_sorted, sells_sorted, spacing)."""
    anchor = gs.get("anchor")
    orders = gs.get("orders") or {}
    buys, sells = [], []
    for k, side in orders.items():
        try:
            p = float(k)
        except Exception:
            continue
        (buys if side == "buy" else sells).append(p)
    buys.sort(reverse=True)
    sells.sort()
    return anchor, buys, sells, gs.get("spacing_pct")


def render():
    meta = read_meta()
    trades = read_trades(15)
    stats = trade_stats()
    sup_lines = log_tail(SUP_LOG, 14)
    freshness = live_log_freshness()

    # ---- estado del bot ----
    status = (meta.get("status") or "unknown").strip().lower()
    halted = (meta.get("halted") or "").strip().lower() in ("1", "true", "yes")
    unit = unit_active("grid-bot.service")
    if halted:
        estado_emoji, estado_txt, estado_color = "🛑", "HALTED (stop-loss)", "#ef4444"
    elif unit == "active" and status == "running":
        estado_emoji, estado_txt, estado_color = "✅", "ACTIVO", "#22c55e"
    elif unit == "active":
        estado_emoji, estado_txt, estado_color = "⚠️", f"ACTIVO (status='{status}')", "#f59e0b"
    else:
        estado_emoji, estado_txt, estado_color = "⛔", "PARADO", "#ef4444"

    if freshness < 0:
        señal = "sin log"
        señal_color = "#ef4444"
    elif freshness > 600:
        señal = f"sin señal ({int(freshness // 60)} min)"
        señal_color = "#ef4444"
    elif freshness > 300:
        señal = f"señal débil ({int(freshness // 60)} min)"
        señal_color = "#f59e0b"
    else:
        señal = f"señal OK (hace {int(freshness // 60)} min)"
        señal_color = "#22c55e"

    sup_unit = unit_active("grid-supervisor.service")

    # ---- KPIs ----
    try:
        total = float(meta.get("last_total", 0) or 0)
    except Exception:
        total = 0.0
    try:
        price = float(meta.get("last_price", 0) or 0)
    except Exception:
        price = 0.0
    try:
        peak = float(meta.get("peak_equity", 0) or 0)
    except Exception:
        peak = 0.0
    try:
        dd = float(meta.get("drawdown_pct", 0) or 0)
    except Exception:
        dd = 0.0

    try:
        gs = json.loads(meta.get("grid_state") or "{}")
    except Exception:
        gs = {}
    anchor, buys, sells, spacing = grid_levels(gs)

    # ---- grid_state: último precio y pico dentro del JSON ----
    gs_price = gs.get("last_price")
    gs_peak = gs.get("peak_equity")

    # ---- filas de trades ----
    tr_rows = ""
    for t in trades:
        if t["filled"] == 1:
            st_emoji, st_txt, st_col = "✅", "OK", "#22c55e"
        else:
            st_emoji, st_txt, st_col = "❌", "REJ", "#ef4444"
        side_emoji = "🔵" if t["side"] == "buy" else "🔴"
        side_col = "#3b82f6" if t["side"] == "buy" else "#ef4444"
        ts = t["ts"].strftime("%d/%m %H:%M:%S") if t["ts"] else t["id"]
        tx = (t["tx"] or "")[:10] + "…" if t["tx"] else "—"
        tr_rows += (
            f"<tr><td>{ts}</td>"
            f"<td style='color:{side_col};font-weight:600'>{side_emoji} {t['side'].upper()}</td>"
            f"<td>{t['price']:,.2f}</td>"
            f"<td>${t['size_usd']:,.4f}</td>"
            f"<td>${t['fee_usd']:,.4f}</td>"
            f"<td>${t['gas_usd']:,.4f}</td>"
            f"<td style='color:{st_col};font-weight:600'>{st_emoji} {st_txt}</td>"
            f"<td style='font-family:monospace;font-size:0.8em'>{tx}</td></tr>"
        )
    if not tr_rows:
        tr_rows = "<tr><td colspan='8' style='color:#71717a'>Sin trades todavía</td></tr>"

    # ---- filas de grid (compras y ventas) ----
    buy_rows = "".join(
        f"<tr><td style='color:#3b82f6;font-weight:600'>🔵 {p:,.2f}</td></tr>" for p in buys
    ) or "<tr><td style='color:#71717a'>—</td></tr>"
    sell_rows = "".join(
        f"<tr><td style='color:#ef4444;font-weight:600'>🔴 {p:,.2f}</td></tr>" for p in sells
    ) or "<tr><td style='color:#71717a'>—</td></tr>"

    # ---- alertas del supervisor ----
    sup_rows = ""
    for ln in sup_lines:
        level = "info"
        if "ERROR" in ln or "FALLO" in ln or "STOP" in ln:
            level = "err"
        elif "ACTION" in ln or "WARNING" in ln:
            level = "warn"
        col = {"err": "#ef4444", "warn": "#f59e0b", "info": "#94a3b8"}[level]
        ts = ln[:23]
        body = ln[24:] if len(ln) > 24 else ln
        body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        sup_rows += (
            f"<tr><td style='color:#71717a;white-space:nowrap'>{ts}</td>"
            f"<td style='color:{col}'>{body}</td></tr>"
        )

    dd_color = "#22c55e" if dd <= 0.5 else ("#f59e0b" if dd <= 3 else "#ef4444")
    up_down = "🟢" if price >= (anchor or 0) else "🔻"

    last_meta_ts = ""
    try:
        mtime = os.path.getmtime(DB_PATH)
        last_meta_ts = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        pass

    # ---- mini-resumen ----
    resumen = (
        f"<div class='cards'>"
        f"<div class='card'><div class='card-k'>📊 Trades</div><div class='card-v'>{stats['n']}</div></div>"
        f"<div class='card'><div class='card-k'>✅ OK</div><div class='card-v' style='color:#22c55e'>{stats['ok']}</div></div>"
        f"<div class='card'><div class='card-k'>❌ REJ</div><div class='card-v' style='color:#ef4444'>{stats['rej']}</div></div>"
        f"<div class='card'><div class='card-k'>🔵 Compras</div><div class='card-v' style='color:#3b82f6'>{stats['buys']}</div></div>"
        f"<div class='card'><div class='card-k'>🔴 Ventas</div><div class='card-v' style='color:#ef4444'>{stats['sells']}</div></div>"
        f"<div class='card'><div class='card-k'>💸 Fees+Gas</div><div class='card-v'>${stats['fees']:,.4f}</div></div>"
        f"</div>"
    )

    body = f"""
<h1>🤖 Grid Bot WETH/USDC <span class="badge" style="color:{estado_color};border-color:{estado_color}">{estado_emoji} {estado_txt}</span></h1>
<p class="sub">Actualizado: {now_local().strftime('%d/%m/%Y %H:%M:%S')} · auto-refresh {REFRESH_SEC}s · <span style="color:{señal_color}">{señal}</span> · supervisor: {"✅ activo" if sup_unit == "active" else "⛔ " + sup_unit}</p>

<div class="cards">
  <div class="card"><div class="card-k">💰 Total cartera</div><div class="card-v">${total:,.4f}</div></div>
  <div class="card"><div class="card-k">{up_down} Precio WETH</div><div class="card-v">${price:,.2f}</div></div>
  <div class="card"><div class="card-k">📉 Drawdown</div><div class="card-v" style="color:{dd_color}">{dd:.2f}%</div></div>
  <div class="card"><div class="card-k">🏔️ Pico</div><div class="card-v">${peak:,.4f}</div></div>
</div>

<h2>🧩 Grid activo</h2>
<table>
  <tr><th>Anchor</th><th>Spacing</th><th>Niveles compra</th><th>Niveles venta</th></tr>
  <tr>
    <td style="font-weight:700;font-size:1.1em">${anchor:,.2f}</td>
    <td>{spacing}%</td>
    <td style="vertical-align:top"><table class="inner">{buy_rows}</table></td>
    <td style="vertical-align:top"><table class="inner">{sell_rows}</table></td>
  </tr>
</table>
<p class="sub">Último precio en grid_state: ${gs_price} · peak_equity grid: ${gs_peak:,.4f} · pool: <span style="font-family:monospace;font-size:0.85em">{gs.get('active_pool','—')}</span></p>

<h2>🕐 Últimos 15 trades</h2>
<table>
  <tr><th>Fecha (local)</th><th>Lado</th><th>Precio</th><th>Tamaño</th><th>Fee</th><th>Gas</th><th>Estado</th><th>Tx</th></tr>
  {tr_rows}
</table>

<h2>🛰️ Alertas del supervisor (últimas)</h2>
<table class="logtable">{sup_rows}</table>

<h2>📋 Mini-resumen</h2>
{resumen}

<p class="foot">Datos: data/live.db (meta+trades) y data/supervisor.log · DB actualizada: {last_meta_ts} · Lectura en modo read-only</p>
"""

    return html_doc(body)


CSS = r"""
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e17;color:#e2e8f0;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;padding:1.2rem;max-width:1100px;margin:0 auto}
h1{font-size:1.4rem;font-weight:800;margin-bottom:.4rem}
h2{font-size:1.05rem;font-weight:700;margin:1.4rem 0 .5rem;color:#93c5fd;border-bottom:1px solid #1e293b;padding-bottom:.3rem}
.sub{color:#71717a;font-size:.82rem;margin-bottom:.8rem}
.foot{color:#475569;font-size:.75rem;margin-top:1.6rem;border-top:1px solid #1e293b;padding-top:.6rem}
.badge{display:inline-block;padding:.15rem .6rem;border:1px solid;border-radius:999px;font-size:.85rem;font-weight:700;vertical-align:middle}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.7rem;margin:.6rem 0}
.card{background:#111827;border:1px solid #1e293b;border-radius:.6rem;padding:.8rem}
.card-k{color:#71717a;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.card-v{font-size:1.3rem;font-weight:800;margin-top:.2rem}
table{width:100%;border-collapse:collapse;background:#0d1420}
th{text-align:left;padding:.5rem .6rem;border-bottom:2px solid #1e293b;color:#64748b;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
td{padding:.45rem .6rem;border-bottom:1px solid #1a2332;font-size:.9rem}
tr:hover td{background:rgba(148,163,184,.04)}
table.inner{border:none}
table.inner td{border:none;padding:.15rem .4rem;font-size:.85rem}
.logtable td{font-family:ui-monospace,monospace;font-size:.78rem;padding:.3rem .6rem}
"""


def html_doc(body_html):
    return (
        "<!DOCTYPE html>\n<html lang='es'>\n<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>\n"
        f"<meta http-equiv='refresh' content='{REFRESH_SEC}'>\n"
        "<title>Grid Bot WETH/USDC · Dashboard</title>\n"
        "<style>" + CSS + "</style>\n</head>\n<body>" + body_html + "</body>\n</html>"
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silenciar ruido
        pass

    def do_GET(self):
        if self.path.split("?")[0] != "/":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404")
            return
        try:
            html = render().encode("utf-8")
        except Exception as e:
            html = (f"<h1>Error renderizando dashboard</h1><pre>{html_mod.escape(str(e))}</pre>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✅ Dashboard SSR en http://127.0.0.1:{PORT} (auto-refresh {REFRESH_SEC}s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹️  Parando dashboard")
        server.shutdown()


if __name__ == "__main__":
    main()
