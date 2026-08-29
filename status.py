#!/usr/bin/env python3
"""Monitoreo rapido del bot live (dry-run o real).

Lee balances REALES on-chain (WETH + USDC + ETH) y los reconcilia con el
estado persistido del bot.
"""
import sys

sys.path.insert(0, ".")

from paperbot.paper.store import Store  # noqa: E402
from paperbot.config import load_config  # noqa: E402
from paperbot.live.aerodrome import AerodromeLive  # noqa: E402
from paperbot.live.trader import _usd_value  # noqa: E402


def main():
    cfg = load_config()
    db = cfg["live"]["db_path"]
    store = Store(db)
    print("=" * 58)
    print("ESTADO BOT LIVE (on-chain real)")
    print("=" * 58)

    # Estado persistido
    lp = store.get_meta("last_price")
    lt = store.get_meta("last_total")
    pk = store.get_meta("peak_equity")
    dd = store.get_meta("drawdown_pct")
    status = store.get_meta("status")

    # Reconciliación on-chain
    bot = AerodromeLive()
    acc = bot.get_account()
    sym = cfg["pool"]["base_token"]
    try:
        base_usd, usdc_usd, total = _usd_value(bot, acc)
        eth = float(bot.w3.from_wei(bot.eth_balance(acc.address), "ether"))
        print(f"Precio {sym}     : ${lp or 'n/a'}")
        print(f"Total on-chain  : ${total:.4f}  (persistido: ${lt or 'n/a'})")
        print(f"  {sym}          : ${base_usd:.4f}")
        print(f"  USDC          : ${usdc_usd:.4f}")
        print(f"  ETH nativo    : {eth:.6f}")
        print(f"Pico equity     : ${pk or 'n/a'}")
        print(f"Drawdown        : {dd or 'n/a'}%")
        print(f"Estado bot      : {status or 'n/a'}")
        print(f"Wallet          : {acc.address}")
    except Exception as e:
        print(f"Error leyendo on-chain: {e}")

    trades = store.recent_trades(10)
    if trades:
        print(f"Trades recientes ({len(trades)}):")
        for t in trades:
            mark = "OK " if t["filled"] else "REJ"
            tx = (t["tx_hash"] or "")[:12]
            print(f"  [{mark}] {t['ts'][:19]} {t['side']:<4} @ ${t['price']:.2f} size=${t['size_usd']:.4f} {tx}")
    else:
        print("Trades: 0 (aun sin cruzar niveles del grid)")
    print("=" * 58)
    store.close()


if __name__ == "__main__":
    main()
