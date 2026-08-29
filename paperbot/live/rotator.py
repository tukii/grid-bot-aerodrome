"""Rotación automática de activos.

Escanea pools de Base, filtra por guardarraíles y evalúa la volatilidad
diaria real. Si encuentra un token con volatilidad >= `min_ratio` veces la
del activo actual, recomienda migrar.

NO ejecuta la migración por sí mismo — devuelve candidatos; el trader decide.
"""
import logging
import time
from dataclasses import dataclass

import requests

from paperbot.config import load_config

logger = logging.getLogger("paperbot.rotator")

GT_BASE = "https://api.geckoterminal.com/api/v2"


@dataclass
class Candidate:
    pool_address: str
    symbol: str
    base_token: str
    quote_token: str
    dex: str
    liquidity_usd: float
    volume_usd_24h: float
    price_usd: float
    volatility_daily: float      # std of daily returns
    avg_daily_move: float        # mean |daily return|
    tick_spacing: int = 1
    pool_fee: int = 100
    days_history: int = 0

    def __repr__(self):
        return (f"Candidate({self.symbol}, liq=${self.liquidity_usd:,.0f}, "
                f"vol=${self.volume_usd_24h:,.0f}, vol_daily={self.volatility_daily:.2%}, "
                f"move={self.avg_daily_move:.2%})")


class AssetRotator:
    def __init__(self, min_liquidity_usd=250_000, min_volume_usd=500_000,
                 min_price_usd=0.001, min_history_days=3, min_vol_ratio=1.5,
                 exclude_symbols=("WETH", "USDC", "cbBTC", "AERO")):
        cfg = load_config()
        self.min_liquidity = cfg["live"].get("min_liquidity_usd", min_liquidity_usd)
        self.min_volume = cfg["live"].get("min_volume_usd", min_volume_usd)
        self.min_price = min_price_usd
        self.min_history_days = min_history_days
        self.min_vol_ratio = cfg["live"].get("min_vol_ratio", min_vol_ratio)
        self.exclude = set(exclude_symbols)

    # ---- scanning ----
    def scan_pools(self, limit: int = 30) -> list[Candidate]:
        """Fetch trending pools, filter by liquidity/volume guard-rails."""
        pool_ids = self._fetch_pool_ids(limit)
        candidates = []
        for pid in pool_ids:
            try:
                info = self._pool_info(pid)
                if not info:
                    continue
                if info["liquidity"] < self.min_liquidity:
                    continue
                if info["volume_24h"] < self.min_volume:
                    continue
                if info["price"] < self.min_price:
                    continue
                if info["base_symbol"] in self.exclude:
                    continue
                candidates.append(info)
            except Exception as e:
                logger.debug("skip pool %s: %s", pid, e)
        return candidates

    def _fetch_pool_ids(self, limit: int) -> list[str]:
        ids = set()
        for endpoint in ["/networks/base/trending_pools",
                         "/networks/base/new_pools"]:
            try:
                r = requests.get(GT_BASE + endpoint, params={"limit": limit}, timeout=15)
                if r.status_code == 200:
                    for p in r.json().get("data", []):
                        ids.add(p["id"].split("_")[-1])
            except Exception as e:
                logger.debug("scan endpoint %s failed: %s", endpoint, e)
        return list(ids)[:limit]

    def _pool_info(self, pool_address: str) -> dict | None:
        try:
            url = f"{GT_BASE}/networks/base/pools/{pool_address}"
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                return None
            d = r.json()["data"]
            attrs = d["attributes"]
            rels = d["relationships"]
            vol = attrs.get("volume_usd")
            if isinstance(vol, dict):
                vol = vol.get("h24", 0)
            base_id = rels["base_token"]["data"]["id"].split("_")[-1]
            quote_id = rels["quote_token"]["data"]["id"].split("_")[-1]
            return {
                "pool_address": pool_address,
                "base_symbol": attrs.get("base_token_symbol") or attrs["name"].split("/")[0].strip(),
                "base_token": base_id,
                "quote_token": quote_id,
                "dex": rels["dex"]["data"]["id"],
                "liquidity": float(attrs.get("reserve_in_usd") or 0),
                "volume_24h": float(vol or 0),
                "price": float(attrs.get("base_token_price_usd") or 0),
            }
        except Exception as e:
            logger.debug("pool info %s failed: %s", pool_address, e)
            return None

    # ---- volatility ----
    def volatility(self, pool_address: str, tf: str = "h4",
                   window_days: int = 7) -> tuple[float, float] | None:
        """Return (std_of_daily_returns, mean_abs_daily_move) or None."""
        try:
            from paperbot.data.geckoterminal import fetch_ohlcv
            df = fetch_ohlcv(pool_address, tf, limit=200)
            # aggregate h4 -> daily
            daily = df["close"].resample("1D").last().dropna()
            ret = daily.pct_change().dropna()
            if len(ret) < self.min_history_days:
                return None
            return float(ret.std()), float(ret.abs().mean())
        except Exception as e:
            logger.debug("volatility %s failed: %s", pool_address, e)
            return None

    def evaluate(self, current_pool: str, current_symbol: str,
                 current_vol: float, max_vol_checks: int = 12) -> Candidate | None:
        """Scan and return the best candidate beating current volatility.

        Returns None if no candidate is meaningfully better.
        """
        candidates = self.scan_pools()
        # prioritize by volume, cap volatility checks to avoid rate limits
        candidates.sort(key=lambda c: c["volume_24h"], reverse=True)
        candidates = candidates[:max_vol_checks]
        scored = []
        for c in candidates:
            if c["pool_address"].lower() == current_pool.lower():
                continue
            if c["base_symbol"] == current_symbol:
                continue
            vol = self.volatility(c["pool_address"])
            if vol is None:
                continue
            cand = Candidate(
                pool_address=c["pool_address"],
                symbol=c["base_symbol"],
                base_token=c["base_token"],
                quote_token=c["quote_token"],
                dex=c["dex"],
                liquidity_usd=c["liquidity"],
                volume_usd_24h=c["volume_24h"],
                price_usd=c["price"],
                volatility_daily=vol[0],
                avg_daily_move=vol[1],
            )
            scored.append(cand)

        if not scored:
            return None
        # best = highest volatility that passes ratio
        best = max(scored, key=lambda c: c.volatility_daily)
        if current_vol > 0 and best.volatility_daily >= current_vol * self.min_vol_ratio:
            logger.info("candidate %s vol %.2f%% >= current %.2f%% x%.1f",
                        best.symbol, best.volatility_daily * 100,
                        current_vol * 100, self.min_vol_ratio)
            return best
        logger.info("no candidate beats current vol (best: %s %.2f%% vs current %.2f%%)",
                    best.symbol, best.volatility_daily * 100, current_vol * 100)
        return None


def resolve_router_for_pool(pool_address: str, w3=None) -> dict | None:
    """Resolve router/quoter/factory/tickSpacing/fee for a pool on-chain.

    VERIFIED: Only resolves pools from KNOWN Aerodrome factories.
    Returns None for any unknown factory (safety against malicious pools).
    """
    try:
        from web3 import Web3
        cfg = load_config()
        if w3 is None:
            w3 = Web3(Web3.HTTPProvider(cfg["network"]["rpc_url"], request_kwargs={"timeout": 25}))
            if not w3.is_connected():
                w3 = Web3(Web3.HTTPProvider(cfg["network"]["rpc_url_fallback"], request_kwargs={"timeout": 25}))
        pool = Web3.to_checksum_address(pool_address)
        fabi = [{"constant": True, "inputs": [], "name": "factory",
                 "outputs": [{"name": "", "type": "address"}],
                 "stateMutability": "view", "type": "function"}]
        pc = w3.eth.contract(address=pool, abi=fabi)
        factory = pc.functions.factory().call()
        tabi = [{"constant": True, "inputs": [], "name": "tickSpacing",
                 "outputs": [{"name": "", "type": "int24"}],
                 "stateMutability": "view", "type": "function"}]
        ts = w3.eth.contract(address=pool, abi=tabi).functions.tickSpacing().call()
        getfee = [{"constant": True, "inputs": [{"name": "pool", "type": "address"}],
                   "name": "getSwapFee", "outputs": [{"name": "", "type": "uint24"}],
                   "stateMutability": "view", "type": "function"}]
        fc = w3.eth.contract(address=Web3.to_checksum_address(factory), abi=getfee)
        fee = fc.functions.getSwapFee(pool).call()
        KNOWN = {
            "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": (
                "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
                "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"),
            "0xf8f2eb4940cfe7d13603dddd87f123820fc061ef": (
                "0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F",
                "0x514c8B5f54112481E28028F1166Bd78501089259"),
        }
        router, quoter = KNOWN.get(factory.lower(), (None, None))
        if router is None:
            logger.warning("factory %s not in KNOWN registry; cannot route", factory)
            return None
        # Verify quote_token is USDC (our bot only works with USDC-quoted pools)
        quote_abi = [{"constant": True, "inputs": [], "name": "token0",
                      "outputs": [{"name": "", "type": "address"}],
                      "stateMutability": "view", "type": "function"},
                     {"constant": True, "inputs": [], "name": "token1",
                      "outputs": [{"name": "", "type": "address"}],
                      "stateMutability": "view", "type": "function"}]
        pc_abi = w3.eth.contract(address=pool, abi=quote_abi)
        token0 = pc_abi.functions.token0().call()
        token1 = pc_abi.functions.token1().call()
        usdc_addr = cfg["pool"]["quote_token_address"]
        if token0.lower() == usdc_addr.lower():
            base_addr = token1
        elif token1.lower() == usdc_addr.lower():
            base_addr = token0
        else:
            logger.warning("pool %s has no USDC side (token0=%s token1=%s); cannot route",
                           pool_address, token0, token1)
            return None
        return {
            "router": router,
            "quoter": quoter,
            "verified_factory": factory,
            "tick_spacing": ts,
            "pool_fee": fee,
        }
    except Exception as e:
        logger.error("resolve_router_for_pool %s failed: %s", pool_address, e)
        return None
