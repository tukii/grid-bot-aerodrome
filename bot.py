#!/usr/bin/env python3
import argparse
import logging
import sys

from paperbot.config import DATA_DIR, load_config
from paperbot.data import dexscreener, geckoterminal
from paperbot.engine import Engine
from paperbot.paper.store import Store
from paperbot.paper.trader import PaperTrader
from paperbot.report import print_backtest, print_report
from paperbot.strategies.grid import Grid

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def cmd_fetch(args):
    cfg = load_config()
    df = geckoterminal.fetch_ohlcv(cfg["pool"]["address"], args.timeframe, limit=args.limit)
    # FIX BAJA #1: defensive sort_index() at save point — guarantees CSV is
    # always ascending (oldest first) regardless of API return order.
    df = df.sort_index()
    cache = DATA_DIR / f"ohlcv_{args.timeframe}.csv"
    df.to_csv(cache)
    print(f"Descargadas {len(df)} velas {args.timeframe} -> {cache}")


def cmd_backtest(args):
    cfg = load_config()
    pool = cfg["pool"]["address"]
    tf = args.timeframe
    cache = DATA_DIR / f"ohlcv_{tf}.csv"
    if cache.exists() and not args.refresh:
        import pandas as pd
        df = pd.read_csv(cache, index_col="timestamp", parse_dates=True)
    else:
        df = geckoterminal.fetch_ohlcv(pool, tf, limit=args.limit)
        df.to_csv(cache)
    # GeckoTerminal devuelve velas en orden DESCENDENTE (nuevo->viejo).
    # El motor asume orden cronologico (viejo->nuevo); si esta invertido,
    # el backtest simula el tiempo hacia atras. Reversa aqui.
    if len(df) > 1 and df.index[0] > df.index[-1]:
        df = df.sort_index()
    anchor = args.anchor or cfg["grid"]["anchor_price"]
    grid = Grid(anchor=anchor, spacing_pct=args.spacing, range_pct=args.range)
    sim = cfg["simulation"]
    engine = Engine(
        grid=grid,
        capital_usd=sim["initial_capital_usd"],
        taker_fee_pct=sim["taker_fee_pct"],
        slippage_pct=sim["slippage_pct"],
        gas_usd=sim["gas_cost_usd"],
        min_trade_usd=sim["min_trade_usd"],
        floating=args.floating,
        reanchor_pct=cfg["grid"].get("drift_reanchor_pct", 25.0),
        setup_cost_usd=args.setup_cost,
    )
    res = engine.run(df)
    print_backtest(res, tf, f"{cfg['pool']['base_token']}/{cfg['pool']['quote_token']}")


def cmd_run(args):
    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                        filename=cfg["paper"]["log_file"], filemode="a")
    store = Store(cfg["paper"]["db_path"])
    trader = PaperTrader(store, poll_seconds=args.poll)
    trader.run()


def cmd_status(args):
    cfg = load_config()
    print_report(args.db or cfg["paper"]["db_path"])


def cmd_balance(args):
    from paperbot.data.chain import BaseClient
    cfg = load_config()
    client = BaseClient()
    if not client.is_connected():
        print("No se pudo conectar al RPC de Base")
        sys.exit(1)
    addr = args.address or input("Direccion publica (0x...): ").strip()
    bal = client.get_balance(addr)
    print(f"Blockchain: Base (chain_id={cfg['network']['chain_id']})")
    print(f"Balance de {addr}: {bal:.6f} ETH  (~${bal * args.eth_price:.2f})")


def cmd_live(args):
    import logging
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                        filename="data/live.log", filemode="a")
    from paperbot.live.trader import LiveGridTrader
    from paperbot.paper.store import Store
    cfg = load_config()
    live = cfg["live"]
    # HALT FLAG: si el supervisor marcó halted=true (stop-loss duro), el bot NO
    # debe operar aunque systemd lo reintente. Sale con exit 0 sin tocar nada.
    try:
        store = Store(live["db_path"])
        halted = store.get_meta("halted")
        if halted and halted.strip().lower() in ("1", "true", "yes"):
            print("HALTED: grid-bot fue parado por stop-loss (meta 'halted' en DB). "
                  "No se opera. Limpia la meta 'halted' manualmente para reanudar.")
            logging.getLogger("paperbot.live").warning(
                "HALTED flag presente en DB -> saliendo sin operar (exit 0)")
            sys.exit(0)
        store.close()
    except Exception as e:
        logging.getLogger("paperbot.live").error("check halted falló (no bloquea): %s", e)
    if not live["enabled"] and not args.dry_run:
        print("ERROR: live.enabled=false en config.yaml. Usa --dry-run o activa live.enabled=true.")
        sys.exit(1)
    store = Store(live["db_path"])
    trader = LiveGridTrader(store, poll_seconds=args.poll, dry_run=args.dry_run or None)
    if not trader.bot.w3.is_connected():
        print("No conectado al RPC de Base")
        sys.exit(1)
    if not trader.bot.verify_on_chain(force=True):
        print("FALLO la verificacion on-chain del router. Abortando.")
        sys.exit(1)
    if not args.dry_run and not trader.check_pool_safety():
        print("REJECT: pool no supera guardarrailes de liquidez/volumen. Abortando.")
        sys.exit(1)
    status = trader.account_status()
    sym = cfg["pool"]["base_token"]
    print(f"LIVE bot: {'DRY-RUN' if trader.dry_run else 'REAL'} | wallet={status['address']}")
    print(f"  ETH={status['eth']:.6f} {sym}={status['base']:.6f} USDC={status['usdc']:.4f}")
    print(f"  stop-loss {live['stop_loss_pct']}% | poll {trader.poll_seconds}s")
    trader.run()


def cmd_setup(args):
    from paperbot.live.aerodrome import AerodromeLive
    cfg = load_config()
    bot = AerodromeLive()
    print("== SETUP / PRODUCCION ==")
    print(f"RPC conectado: {bot.w3.is_connected()}")
    if not bot.w3.is_connected():
        sys.exit(1)
    print(f"Chain ID: {bot.w3.eth.chain_id}")
    print(f"Router: {bot.router}")
    print(f"Quoter: {bot.quoter}")
    print(f"Pool: {bot.pool_addr}")
    if not bot.verify_on_chain():
        print("!!! FALLO verificacion on-chain (router/pool factory mismatch)")
        sys.exit(1)
    print("OK: router y pool comparten la misma factory (verificacion on-chain)")
    # key presence
    from paperbot.live.aerodrome import _load_env
    env = _load_env()
    key = env.get("PRIVATE_KEY", "")
    if key:
        acc = bot.w3.eth.account.from_key(key)
        print(f"Wallet: {acc.address}")
        eth = bot.eth_balance(acc.address)
        base = bot.token_balance(bot.base_token, acc.address)
        usdc = bot.token_balance(bot.usdc, acc.address)
        sym = cfg["pool"]["base_token"]
        print(f"  ETH native: {bot.w3.from_wei(eth, 'ether'):.6f}")
        print(f"  {sym}: {base / 10 ** bot.base_decimals:.6f}")
        print(f"  USDC: {usdc / 10 ** bot.quote_decimals:.6f}")
        # dry-run quote
        q = bot.quote_swap(bot.base_token, 10 ** bot.base_decimals, bot.usdc)
        print(f"  Quoter (1 {sym}->USDC): {q / 10 ** bot.quote_decimals:.4f} USDC" if q else "  Quoter: FALLO")
    else:
        print("WARNING: PRIVATE_KEY no definida en .env (modo solo lectura)")


def cmd_prep(args):
    from paperbot.live.aerodrome import AerodromeLive
    cfg = load_config()
    bot = AerodromeLive()
    if not bot.w3.is_connected():
        print("No conectado al RPC de Base")
        sys.exit(1)
    if not bot.verify_on_chain():
        print("FALLO verificacion on-chain del router. Abortando.")
        sys.exit(1)
    from paperbot.live.aerodrome import _load_env
    key = _load_env().get("PRIVATE_KEY", "")
    if not key:
        print("ERROR: PRIVATE_KEY no definida en .env")
        sys.exit(1)
    acc = bot.w3.eth.account.from_key(key)
    native = bot.eth_balance(acc.address)
    weth = bot.token_balance(bot.weth, acc.address)
    usdc = bot.token_balance(bot.usdc, acc.address)
    print("== PREPARAR CUENTA ==")
    print(f"Wallet: {acc.address}")
    print(f"ETH nativo : {bot.w3.from_wei(native, 'ether'):.6f} ETH")
    print(f"WETH       : {bot.w3.from_wei(weth, 'ether'):.6f} WETH")
    print(f"USDC       : {usdc / 1e6:.4f} USDC")
    # reserve some ETH for gas
    gas_reserve = bot.w3.to_wei("0.0003", "ether")  # ~$0.55 for ~30 swaps
    wrapable = native - gas_reserve
    if wrapable > 0:
        print(f"Wrapable   : {bot.w3.from_wei(wrapable, 'ether'):.6f} ETH (tras reservar {bot.w3.from_wei(gas_reserve, 'ether')} para gas)")
    else:
        print("No hay ETH suficiente para wrap (necesitas gas).")
    if args.wrap_all:
        if wrapable <= 0:
            print("Nada que wrap. Abortando.")
            sys.exit(1)
        r = bot.wrap_eth(wrapable, account=acc, dry_run=False)
        print(f"Wrap: {'OK' if r.ok else 'FALLO'} {r.message}")
    if args.deploy:
        # Desplegar al 50/50: wrap ETH->WETH, luego convertir a USDC/WETH para 50/50
        print("== DESPLEGAR AL 50/50 ==")
        if wrapable <= 0:
            print("No hay ETH para wrap. Abortando deploy.")
            sys.exit(1)
        r = bot.wrap_eth(wrapable, account=acc, dry_run=False)
        print(f"Wrap ETH->WETH: {'OK' if r.ok else 'FALLO'} {r.message}")
        if not r.ok:
            sys.exit(1)
        from paperbot.data.price import fetch_price
        price = fetch_price(cfg["pool"]["address"]) or 1883.0
        # recompute balances after wrap
        weth = bot.token_balance(bot.weth, acc.address) / 1e18
        usdc = bot.token_balance(bot.usdc, acc.address) / 1e6
        total_usd = weth * price + usdc
        target_half_usd = total_usd / 2
        weth_usd = weth * price
        print(f"Tras wrap: WETH=${weth_usd:.2f} USDC=${usdc:.2f} Total=${total_usd:.2f}")
        if weth_usd > target_half_usd:
            # too much WETH -> sell some for USDC
            sell_usd = weth_usd - target_half_usd
            sell_wei = int(sell_usd / price * 1e18)
            r = bot.swap_exact_in(bot.weth, bot.usdc, sell_wei, account=acc, dry_run=False)
            print(f"Vender WETH->USDC: {'OK' if r.ok else 'FALLO'} {r.message}")
        else:
            # too little WETH -> buy WETH with USDC
            buy_usd = target_half_usd - weth_usd
            usdc_use = int(buy_usd * 1e6)
            usdc_bal = bot.token_balance(bot.usdc, acc.address)
            usdc_use = min(usdc_use, usdc_bal)
            if usdc_use > 0:
                r = bot.swap_exact_in(bot.usdc, bot.weth, usdc_use, account=acc, dry_run=False)
                print(f"Comprar WETH<-USDC: {'OK' if r.ok else 'FALLO'} {r.message}")
        # final state
        weth = bot.token_balance(bot.weth, acc.address) / 1e18
        usdc = bot.token_balance(bot.usdc, acc.address) / 1e6
        weth_usd = weth * price
        print(f"Final: WETH={weth:.6f} (~${weth_usd:.2f}) USDC={usdc:.4f} Total=${weth_usd + usdc:.2f}")


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="bot", description="Grid bot on Base (paper/live)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="Descargar OHLCV de GeckoTerminal")
    p.add_argument("--timeframe", default="h1", choices=["m1", "m5", "m15", "h1", "h4", "h12", "d1"])
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("backtest", help="Backtest del grid")
    p.add_argument("--timeframe", default="h1", choices=["m1", "m5", "m15", "h1", "h4", "h12", "d1"])
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--spacing", type=float, default=cfg["grid"]["spacing_pct"])
    p.add_argument("--range", type=float, default=cfg["grid"]["range_pct"])
    p.add_argument("--anchor", type=float, default=None)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--floating", action="store_true", help="Usa grid flotante (re-anchor)")
    p.add_argument("--setup-cost", type=float, default=0.0, help="Coste inicial (approve+wrap)")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("run", help="Ejecutar paper trader en vivo")
    p.add_argument("--poll", type=int, default=cfg["paper"]["poll_seconds"])
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="Estado del paper bot")
    p.add_argument("--db", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("balance", help="Consultar balance en Base (solo lectura)")
    p.add_argument("--address", default=None)
    p.add_argument("--eth-price", type=float, default=2000.0, help="ETH price for USD estimate (default $2000)")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("setup", help="Validar configuracion live + dry-run")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("live", help="Ejecutar bot live (requiere live.enabled=true)")
    p.add_argument("--poll", type=int, default=cfg["live"]["poll_seconds"])
    p.add_argument("--dry-run", action="store_true", help="Fuerza dry-run sin enviar tx")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("prep", help="Preparar cuenta: ver balances y hacer wrap ETH->WETH")
    p.add_argument("--wrap-all", action="store_true",
                   help="Wrap todo el ETH nativo a WETH (ejecuta tx real, requiere confirmacion)")
    p.add_argument("--deploy", action="store_true",
                   help="Desplegar al 50/50: wrap ETH->WETH + convertir a USDC/WETH")
    p.set_defaults(func=cmd_prep)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
