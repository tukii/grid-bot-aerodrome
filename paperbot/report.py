from paperbot.config import load_config
from paperbot.paper.store import Store


def print_report(db_path: str | None = None):
    cfg = load_config()
    db_path = db_path or cfg["paper"]["db_path"]
    store = Store(db_path)
    stats = store.stats()
    last = store.last_tick()

    print("=" * 56)
    print("GRID BOT - REPORT")
    print("=" * 56)
    if last:
        print(f"Ultimo tick : {last['ts']}")
        print(f"Precio      : ${last['price']:.4f}")
        print(f"Cash        : ${last['cash']:.4f}")
        print(f"Posicion    : ${last['position_usd']:.4f}")
        print(f"Total       : ${last['total_usd']:.4f}")
    print(f"Operaciones : {stats['n']}  (compras={stats['buys']} ventas={stats['sells']})")
    print(f"Comisiones  : ${stats['fees']:.4f}")
    print("-" * 56)
    trades = store.recent_trades(10)
    if trades:
        print("Ultimas operaciones:")
        for t in trades:
            print(f"  {t['ts'][:19]}  {t['side']:<4} @ ${t['price']:.4f}  "
                  f"size=${t['size_usd']:.4f} fee=${t['fee_usd'] + t['gas_usd']:.4f}")
    store.close()


def print_backtest(res, timeframe: str, symbol: str):
    print("=" * 56)
    print(f"BACKTEST {symbol}  ({timeframe})")
    print("=" * 56)
    print(f"Capital inicial : ${res.initial_total_usd:.2f}")
    print(f"Capital final   : ${res.final_total_usd:.2f}")
    print(f"PnL             : ${res.pnl_usd:.2f}  ({res.pnl_pct:+.2f}%)")
    print(f"Operaciones     : {len(res.trades)}  (compras={res.n_buys} ventas={res.n_sells})")
    print(f"Win rate        : {res.win_rate_pct:.1f}%")
    print(f"Max drawdown    : {res.max_drawdown_pct:.2f}%")
    print(f"Comisiones      : ${res.total_fees_usd:.4f}")
    print("=" * 56)
