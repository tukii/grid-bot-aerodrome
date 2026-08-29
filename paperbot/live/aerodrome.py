"""Live trading module (Base / Aerodrome Slipstream).

SECURITY:
- The private key is read from .env (never hard-coded, never logged).
- `live.enabled` must be true in config.yaml AND the env flag LIVE_TRADING=1
  must be set; otherwise every call is a dry-run (no real tx).
- Before any swap, on-chain verification that the router's factory() matches
  the pool's factory() is performed (phishing protection).

VERIFIED on-chain (2026-08):
- SwapRouter: 0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F  (factory()==0xf8f2eB..., same as pool)
- QuoterV2:   0x514c8B5f54112481E28028F1166Bd78501089259
- Slipstream uses tickSpacing (int24), NOT fee (uint24). Pool tickSpacing=50.
- Pool getSwapFee = 266 (0.0266%)
"""
import logging
import time
from dataclasses import dataclass

from web3 import Web3

from paperbot.config import ENV_PATH, load_config

logger = logging.getLogger("paperbot.live")

SWAP_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "int24", "name": "tickSpacing", "type": "int24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

WETH_ABI = [
    {
        "constant": False,
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _build_fee_params(bot: "AerodromeLive") -> dict:
    """Parámetros de gas para build_transaction (EIP-1559, fix BAJA).

    Si la red soporta maxFeePerGas (Base sí: baseFeePerGas en el bloque
    "latest"), construye la tx con maxFeePerGas/maxPriorityFeePerGas en lugar
    de gasPrice legacy. El cap configurado (live.max_gas_gwei) es el límite
    ESTRICTO de maxFeePerGas — nunca se supera. maxPriorityFeePerGas se capea
    al mínimo(0.001 gwei, maxFee).

    Si la red NO soporta EIP-1559 -> gasPrice legacy con el mismo cap
    (comportamiento anterior intacto, cae bien en redes legacy).
    """
    return bot._build_fee_params()


@dataclass
class SwapResult:
    dry_run: bool
    ok: bool
    tx_hash: str | None
    expected_out: int | None
    actual_out: int | None
    message: str
    gas_used: int | None = None
    receipt_status: int | None = None


class ApprovalStatus:
    EXISTS = "exists"
    SENT = "sent"
    FAILED = "failed"


class NonceManager:
    """Tracks local nonce to avoid races between approve + swap txs.

    Uses a single counter seeded from the chain, incremented locally after
    each successfully *sent* transaction, and re-synced from chain if it
    falls behind (e.g. after a crash).
    """

    def __init__(self, w3, address: str):
        self.w3 = w3
        self.address = Web3.to_checksum_address(address)
        self._nonce = None

    def sync(self):
        self._nonce = self.w3.eth.get_transaction_count(self.address, "pending")

    def next(self) -> int:
        if self._nonce is None:
            self.sync()
        n = self._nonce
        self._nonce += 1
        return n

    def mark_sent(self, nonce_used: int):
        # If a tx was sent externally with a higher nonce, don't regress.
        if self._nonce is not None and nonce_used >= self._nonce:
            self._nonce = nonce_used + 1

    def resync_if_behind(self):
        try:
            chain = self.w3.eth.get_transaction_count(self.address, "pending")
        except Exception:
            return
        if self._nonce is not None and chain > self._nonce:
            self._nonce = chain


class AerodromeLive:
    def __init__(self, rpc_url: str | None = None):
        cfg = load_config()
        self.cfg = cfg
        live = cfg["live"]
        self.rpc_url = rpc_url or cfg["network"]["rpc_url"]
        self.rpc_fallback = cfg["network"].get("rpc_url_fallback")
        self.rpc_fallback2 = cfg["network"].get("rpc_url_fallback2")
        self.w3 = self._connect_with_fallback(
            self.rpc_url, self.rpc_fallback, self.rpc_fallback2)
        self.chain_id = cfg["network"]["chain_id"]
        self.router = Web3.to_checksum_address(live["router_address"])
        self.quoter = Web3.to_checksum_address(live["quoter_address"])
        self.verified_factory = Web3.to_checksum_address(live["verified_factory"])
        self.tick_spacing = live["tick_spacing"]
        self.slippage = live["slippage_pct"] / 100.0
        self.dry_run_forced = live["dry_run"]
        self.max_spend_usd = live["max_spend_usd"]
        self.max_gas_gwei = float(live.get("max_gas_gwei", 0.1))
        self._eip1559: bool | None = None  # cache detección EIP-1559 (fix BAJA)
        self.min_confirmations = int(live.get("min_confirmations", 1))
        self.pool_addr = Web3.to_checksum_address(cfg["pool"]["address"])
        # Base token + quote token: any ERC20 pair
        self.base_token = Web3.to_checksum_address(cfg["pool"]["base_token_address"])
        self.usdc = Web3.to_checksum_address(cfg["pool"]["quote_token_address"])
        self.weth = Web3.to_checksum_address(cfg["pool"].get("weth_address", "0x4200000000000000000000000000000000000006"))
        self.base_decimals = int(cfg["pool"].get("base_token_decimals", 18))
        self.quote_decimals = int(cfg["pool"].get("quote_token_decimals", 6))
        self._read_decimals()

        self._router_contract = self.w3.eth.contract(address=self.router, abi=SWAP_ROUTER_ABI)
        self._quoter_contract = self.w3.eth.contract(address=self.quoter, abi=QUOTER_ABI)
        self._factory_verify_ts = 0.0
        self._factory_verify_ok = False
        # NonceManager persistente por proceso: se reutiliza entre approve+swap,
        # rebalance y wraps. resync_if_behind() antes de cada build evita reutilizar
        # nonces tras conmutación de RPC (la cuenta "pending" de un nodo puede ir
        # atrasada). Se crea perezosamente en el primer uso (la clave no se conoce
        # hasta get_account, que requiere PRIVATE_KEY del .env).
        self._nonce_manager = None
        self._nonce_manager_addr = None

    def _read_decimals(self):
        """Read decimals from chain; fall back to config values."""
        try:
            c = self.w3.eth.contract(address=self.base_token, abi=ERC20_ABI)
            self.base_decimals = c.functions.decimals().call()
        except Exception:
            pass
        try:
            c = self.w3.eth.contract(address=self.usdc, abi=ERC20_ABI)
            self.quote_decimals = c.functions.decimals().call()
        except Exception:
            pass

    @staticmethod
    def _connect_with_fallback(primary: str, fallback: str | None, fallback2: str | None = None):
        for url in [primary, fallback, fallback2, "https://mainnet.base.org"]:
            if not url:
                continue
            for attempt in range(3):
                try:
                    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
                    if w3.is_connected():
                        return w3
                except Exception:
                    pass
                time.sleep(1)
        raise RuntimeError("Could not connect to any Base RPC")

    # ---- key management ----
    def get_account(self):
        env = _load_env()
        key = env.get("PRIVATE_KEY", "").strip()
        if not key:
            raise RuntimeError("PRIVATE_KEY not found in .env")
        return self.w3.eth.account.from_key(key)

    def get_nonce_manager(self, account) -> NonceManager:
        """Devuelve el NonceManager persistente del proceso (uno por cuenta).

        Si el NonceManager aún no existe (o la cuenta cambió, p.ej. rotación
        de activo) lo crea. Si la instancia previa quedó vinculada a otra
        cuenta, se descarta para no reutilizar nonces de otra wallet.
        """
        addr = Web3.to_checksum_address(account.address)
        if self._nonce_manager is None or self._nonce_manager_addr != addr:
            self._nonce_manager = NonceManager(self.w3, addr)
            self._nonce_manager_addr = addr
        return self._nonce_manager

    # ---- security verification (cached 5 min) ----
    def verify_on_chain(self, force: bool = False) -> bool:
        """Confirm router.factory() == verified_factory == pool.factory()."""
        now = time.time()
        if not force and self._factory_verify_ok and (now - self._factory_verify_ts) < 300:
            return True
        try:
            router_factory = self._router_contract.functions.factory().call()
            pool_abi = [
                {
                    "constant": True,
                    "inputs": [],
                    "name": "factory",
                    "outputs": [{"name": "", "type": "address"}],
                    "stateMutability": "view",
                    "type": "function",
                }
            ]
            pool_factory = self.w3.eth.contract(address=self.pool_addr, abi=pool_abi).functions.factory().call()
            ok = (
                router_factory.lower() == self.verified_factory.lower()
                and pool_factory.lower() == self.verified_factory.lower()
            )
            self._factory_verify_ok = ok
            self._factory_verify_ts = now
            logger.info("on-chain verify: router_factory=%s pool_factory=%s ok=%s",
                        router_factory, pool_factory, ok)
            return ok
        except Exception as e:
            logger.error("on-chain verify failed: %s", e)
            # Retry once on fallback RPC before giving up
            try:
                if self.rpc_fallback:
                    w3 = Web3(Web3.HTTPProvider(self.rpc_fallback, request_kwargs={"timeout": 20}))
                    router_factory = w3.eth.contract(
                        address=self.router, abi=SWAP_ROUTER_ABI
                    ).functions.factory().call()
                    pool_abi = [
                        {
                            "constant": True,
                            "inputs": [],
                            "name": "factory",
                            "outputs": [{"name": "", "type": "address"}],
                            "stateMutability": "view",
                            "type": "function",
                        }
                    ]
                    pool_factory = w3.eth.contract(
                        address=self.pool_addr, abi=pool_abi
                    ).functions.factory().call()
                    ok = (
                        router_factory.lower() == self.verified_factory.lower()
                        and pool_factory.lower() == self.verified_factory.lower()
                    )
                    self._factory_verify_ok = ok
                    self._factory_verify_ts = now
                    logger.info("on-chain verify (fallback): ok=%s", ok)
                    return ok
            except Exception as e2:
                logger.error("on-chain verify fallback failed: %s", e2)
            return False

    # ---- quotes ----
    def quote_swap(self, token_in: str, amount_in_raw: int, token_out: str) -> int | None:
        token_in = Web3.to_checksum_address(token_in)
        token_out = Web3.to_checksum_address(token_out)
        params = {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amountIn": amount_in_raw,
            "tickSpacing": self.tick_spacing,
            "sqrtPriceLimitX96": 0,
        }
        def _q():
            result = self._quoter_contract.functions.quoteExactInputSingle(params).call()
            return result[0]  # amountOut
        try:
            return self._call_with_retry(_q)
        except Exception as e:
            logger.error("quote failed: %s", e)
            return None

    # ---- balance helpers ----
    def _switch_rpc(self, index: int):
        urls = [u for u in [self.rpc_url, self.rpc_fallback, self.rpc_fallback2, "https://mainnet.base.org"] if u]
        url = urls[index % len(urls)]
        try:
            w3f = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
            if w3f.is_connected():
                self.w3 = w3f
                self._router_contract = self.w3.eth.contract(address=self.router, abi=SWAP_ROUTER_ABI)
                self._quoter_contract = self.w3.eth.contract(address=self.quoter, abi=QUOTER_ABI)
                logger.warning("switched RPC -> %s", url)
                return True
        except Exception as e:
            logger.error("switch to %s failed: %s", url, e)
        return False

    def _call_with_retry(self, fn, retries: int = 4):
        """Run fn() with transient-error retries, switching RPC each attempt."""
        last_err = None
        switch_idx = 0
        for attempt in range(retries):
            try:
                return fn()
            except Exception as e:
                last_err = e
                msg = str(e)
                if any(s in msg for s in ("429", "408", "503", "timeout", "timed out", "Too Many", "usage limit")):
                    if attempt >= 1:
                        if self._switch_rpc(switch_idx):
                            switch_idx += 1
                    time.sleep(1.5 ** attempt)
                    continue
                raise
        raise last_err

    def token_balance(self, token: str, account: str) -> int:
        token = Web3.to_checksum_address(token)
        def _b():
            c = self.w3.eth.contract(address=token, abi=ERC20_ABI)
            return c.functions.balanceOf(Web3.to_checksum_address(account)).call()
        return self._call_with_retry(_b)

    def eth_balance(self, account: str) -> int:
        def _b():
            return self.w3.eth.get_balance(Web3.to_checksum_address(account))
        return self._call_with_retry(_b)

    def allowance(self, token: str, account: str) -> int:
        token = Web3.to_checksum_address(token)
        def _b():
            c = self.w3.eth.contract(address=token, abi=ERC20_ABI)
            return c.functions.allowance(Web3.to_checksum_address(account), self.router).call()
        return self._call_with_retry(_b)

    # ---- gas (EIP-1559) ----
    @property
    def eip1559_supported(self) -> bool:
        """Base es EIP-1559: el bloque "latest" expone baseFeePerGas.

        Detección viva (un RPC), cacheada: si el último bloque trae
        baseFeePerGas > 0, las txs con maxFeePerGas/maxPriorityFeePerGas son
        aceptadas. En caso de error (RPC apagado) devuelve False -> gasPrice
        legacy como antes.
        """
        if self._eip1559 is None:
            self._eip1559 = False
            try:
                blk = self.w3.eth.get_block("latest")
                self._eip1559 = bool(blk.get("baseFeePerGas"))
            except Exception:
                self._eip1559 = False
        return self._eip1559

    def _gas_price(self) -> int:
        """Gas price capped by max_gas_gwei. Falls back to network gas price.

        EIP-1559 (fix BAJA): si la red lo soporta, devuelve el precio LEGACY
        equivalente al cap (maxFeePerGas = cap, maxPriorityFeePerGas = 0.001
        gwei) — las txs se construyen con maxFeePerGas/maxPriorityFeePerGas y
        el VALIDADOR del cap aplica igual. Este método se mantiene para
        compatibilidad (tests y rutas legacy).
        """
        gp = self.w3.eth.gas_price
        cap = int(self.max_gas_gwei * 1e9)
        if self.max_gas_gwei > 0 and gp > cap:
            logger.warning("gas price %.4f gwei > cap %.4f, using cap", gp / 1e9, self.max_gas_gwei)
            return cap
        return gp

    def _build_fee_params(self) -> dict:
        """Parámetros de gas para build_transaction.

        EIP-1559 (fix BAJA) si la red lo soporta: usa maxFeePerGas (cap) y
        maxPriorityFeePerGas (=0.001 gwei, razonable en Base). Así la tx NO
        queda "underpriced" con gasPrice legacy cuando la red sube: el cap
        de maxFeePerGas = self.max_gas_gwei es el límite estricto y el
        minero puede reclamar hasta ese máximo sin que la tx muera lenta.

        Seguridad:
          - maxFeePerGas NUNCA supera el cap configurado (max_gas_gwei).
          - maxPriorityFeePerGas se capea al mínimo(max_priority, maxFee).
          - Si la red NO soporta EIP-1559 (baseFeePerGas ausente) -> gasPrice
            legacy con el mismo cap (comportamiento anterior intacto).
        """
        cap = int(self.max_gas_gwei * 1e9)
        if self.eip1559_supported:
            base_fee = int(self.w3.eth.get_block("latest")["baseFeePerGas"])
            tip = int(1e6)  # 0.001 gwei (Base: suficiente, base fee ~0.005 gwei)
            tip = min(tip, cap)
            max_fee = max(base_fee + tip, tip)
            max_fee = min(max_fee, cap)
            if max_fee <= 0:
                # Red sin precio (anomalía): usa legacy con cap como salvaguarda
                return {"gasPrice": cap or self.w3.eth.gas_price}
            logger.debug("EIP-1559 fees: baseFee=%s tip=%s maxFee=%s (cap=%s)",
                         base_fee, tip, max_fee, cap)
            return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip}
        return {"gasPrice": self._gas_price()}

    def _estimate_gas(self, tx: dict) -> int:
        try:
            est = self.w3.eth.estimate_gas({
                "from": tx.get("from"),
                "to": tx.get("to"),
                "data": tx.get("data"),
                "value": tx.get("value", 0),
            })
            return int(est * 1.2)  # buffer
        except Exception:
            return tx.get("gas", 250000)

    # ---- approvals ----
    def ensure_approval(self, token: str, amount_raw: int, account, nonce_mgr=None) -> str:
        """Ensure the router can spend `amount_raw` of `token`.

        Usa approve INFINITO (2^256-1): una sola tx de approve y el router
        puede gastar siempre, eliminando el approve por swap (ahorro ~$0.0008/swap,
        condición obligatoria de la economía del grid a $4.30).
        Seguridad: el router está verificado on-chain (factory match); el approve
        infinito es el estándar en DeFi (Uniswap, Aerodrome, etc.).

        Returns ApprovalStatus.EXISTS / SENT / FAILED.
        """
        token = Web3.to_checksum_address(token)
        c = self.w3.eth.contract(address=token, abi=ERC20_ABI)
        current = c.functions.allowance(account.address, self.router).call()
        if current >= amount_raw:
            return ApprovalStatus.EXISTS
        # Approve infinito si el allowance actual es < amount_raw
        if current > 0:
            # Primero a 0 (requerido por algunos tokens), luego a máx
            try:
                tx0 = c.functions.approve(self.router, 0).build_transaction({
                    "from": account.address,
                    "nonce": nonce_mgr.next(),
                    "gas": 60000,
                    **_build_fee_params(self),
                    "chainId": self.chain_id,
                })
                tx0["gas"] = self._estimate_gas(tx0)
                self._send(tx0, account, "approve_reset", nonce_mgr=nonce_mgr)
            except Exception as e:
                logger.warning("approve reset a 0 falló (no crítico): %s", e)
        MAX_UINT = (1 << 256) - 1
        nonce_mgr = nonce_mgr or self.get_nonce_manager(account)
        nonce_mgr.resync_if_behind()
        tx = c.functions.approve(self.router, MAX_UINT).build_transaction({
            "from": account.address,
            "nonce": nonce_mgr.next(),
            "gas": 60000,
            **_build_fee_params(self),
            "chainId": self.chain_id,
        })
        tx["gas"] = self._estimate_gas(tx)
        ok, _, _ = self._send(tx, account, "approve", nonce_mgr=nonce_mgr)
        return ApprovalStatus.SENT if ok else ApprovalStatus.FAILED

    # ---- swap ----
    def swap_exact_in(self, token_in: str, token_out: str, amount_in_raw: int,
                      account=None, dry_run: bool | None = None,
                      min_out_override: int | None = None) -> SwapResult:
        """Swap exact input.

        min_out_override: mínimo de salida en raw units (token_out) fijado por
        el llamador — p.ej. el trader lo ancla al precio del NIVEL de grid
        esperado (no al quote vivo) para que un gap de 5-25% revierta la tx en
        lugar de ejecutar a precio de mercado. None => 0.997 x quote (comporta-
        miento original, p.ej. unwind como market sell).
        """
        dry_run = self.dry_run_forced if dry_run is None else dry_run
        token_in = Web3.to_checksum_address(token_in)
        token_out = Web3.to_checksum_address(token_out)

        if not self.verify_on_chain():
            return SwapResult(dry_run, False, None, None, None,
                              "on-chain factory verification FAILED; aborting")

        quote_out = self.quote_swap(token_in, amount_in_raw, token_out)
        if quote_out is None:
            return SwapResult(dry_run, False, None, None, None, "quote failed")

        min_out = min_out_override if min_out_override is not None else int(quote_out * (1 - self.slippage))
        deadline = self.w3.eth.get_block("latest")["timestamp"] + 300

        if dry_run:
            return SwapResult(True, True, None, quote_out, None,
                              f"DRY-RUN: swap {amount_in_raw} in -> {quote_out} out (min {min_out})")

        if account is None:
            account = self.get_account()

        nonce_mgr = self.get_nonce_manager(account)
        nonce_mgr.resync_if_behind()

        # Ensure approval for the input token (WETH or USDC). Share nonce_mgr.
        approval = self.ensure_approval(token_in, amount_in_raw, account, nonce_mgr)
        if approval == ApprovalStatus.FAILED:
            return SwapResult(False, False, None, quote_out, None,
                              "approval FAILED; aborting swap")
        if approval == ApprovalStatus.SENT:
            logger.info("approval sent; waiting for receipt before swap")
            time.sleep(3)
            self.w3.eth.get_transaction_count(account.address, "pending")

        params = {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "tickSpacing": self.tick_spacing,
            "recipient": account.address,
            "deadline": deadline,
            "amountIn": amount_in_raw,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }
        tx = self._router_contract.functions.exactInputSingle(params).build_transaction({
            "from": account.address,
            "nonce": nonce_mgr.next(),
            "gas": 250000,
            **_build_fee_params(self),
            "chainId": self.chain_id,
        })
        tx["gas"] = self._estimate_gas(tx)

        if token_in.lower() == self.weth.lower():
            bal = self.token_balance(self.weth, account.address)
            if amount_in_raw > bal:
                return SwapResult(False, False, None, quote_out, None, "insufficient WETH balance")

        ok, status, tx_hash = self._send(tx, account, "exactInputSingle", nonce_mgr=nonce_mgr)
        if not ok:
            return SwapResult(False, False, None, quote_out, None, f"swap failed: {status}")
        return SwapResult(False, True, tx_hash, quote_out, None,
                          "swap confirmed", receipt_status=status)

    def _send(self, tx, account, label, nonce_mgr=None) -> tuple[bool, int | None, str | None]:
        """Send a raw tx, wait for receipt, verify status==1.

        Returns (ok, receipt_status, tx_hash).
        """
        raw = None
        try:
            signed = account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        except Exception as e:
            logger.error("%s sign failed: %s", label, e)
            return False, None, None

        last_err = None
        for attempt in range(3):
            try:
                tx_hash = self.w3.eth.send_raw_transaction(raw)
                if nonce_mgr:
                    nonce_mgr.mark_sent(tx["nonce"])
                logger.info("%s tx sent: %s", label, tx_hash.hex())
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                if receipt["status"] == 1:
                    logger.info("%s confirmed (gas=%s)", label, receipt["gasUsed"])
                    return True, receipt["status"], tx_hash.hex()
                logger.error("%s REVERTED (status=%s), gasUsed=%s", label,
                             receipt["status"], receipt["gasUsed"])
                return False, receipt["status"], tx_hash.hex()
            except Exception as e:
                last_err = e
                msg = str(e)
                # Transient errors worth retrying
                if any(s in msg for s in ("429", "408", "503", "underpriced", "timeout", "timed out")):
                    time.sleep(2 ** attempt)
                    continue
                # "nonce too low" = desincronización de nonce, no transitorio; no reintenta
                logger.error("%s failed (attempt %d): %s", label, attempt + 1, e)
                return False, None, None
        logger.error("%s failed after retries: %s", label, last_err)
        return False, None, None

    # ---- ETH <-> WETH wrap/unwrap ----
    def wrap_eth(self, amount_wei: int, account=None, dry_run: bool | None = None) -> SwapResult:
        dry_run = self.dry_run_forced if dry_run is None else dry_run
        if account is None:
            account = self.get_account()
        if dry_run:
            return SwapResult(True, True, None, amount_wei, None,
                              f"DRY-RUN: wrap {amount_wei} wei ETH -> WETH")
        weth = self.w3.eth.contract(address=self.weth, abi=WETH_ABI)
        bal = self.eth_balance(account.address)
        if amount_wei > bal:
            return SwapResult(False, False, None, amount_wei, None, "insufficient native ETH")
        nonce_mgr = self.get_nonce_manager(account)
        nonce_mgr.resync_if_behind()
        tx = weth.functions.deposit().build_transaction({
            "from": account.address,
            "value": amount_wei,
            "nonce": nonce_mgr.next(),
            "gas": 60000,
            **_build_fee_params(self),
            "chainId": self.chain_id,
        })
        tx["gas"] = self._estimate_gas(tx)
        ok, status, _ = self._send(tx, account, "wrap ETH->WETH", nonce_mgr=nonce_mgr)
        if not ok:
            return SwapResult(False, False, None, amount_wei, None, f"wrap failed: {status}")
        return SwapResult(False, True, None, amount_wei, None, "wrapped", receipt_status=status)

    def unwrap_weth(self, amount_wei: int, account=None, dry_run: bool | None = None) -> SwapResult:
        dry_run = self.dry_run_forced if dry_run is None else dry_run
        if account is None:
            account = self.get_account()
        if dry_run:
            return SwapResult(True, True, None, amount_wei, None,
                              f"DRY-RUN: unwrap {amount_wei} WETH -> ETH")
        weth = self.w3.eth.contract(address=self.weth, abi=WETH_ABI)
        bal = self.token_balance(self.weth, account.address)
        if amount_wei > bal:
            return SwapResult(False, False, None, amount_wei, None, "insufficient WETH")
        nonce_mgr = self.get_nonce_manager(account)
        nonce_mgr.resync_if_behind()
        tx = weth.functions.withdraw(amount_wei).build_transaction({
            "from": account.address,
            "value": 0,
            "nonce": nonce_mgr.next(),
            "gas": 60000,
            **_build_fee_params(self),
            "chainId": self.chain_id,
        })
        tx["gas"] = self._estimate_gas(tx)
        ok, status, _ = self._send(tx, account, "unwrap WETH->ETH", nonce_mgr=nonce_mgr)
        if not ok:
            return SwapResult(False, False, None, amount_wei, None, f"unwrap failed: {status}")
        return SwapResult(False, True, None, amount_wei, None, "unwrapped", receipt_status=status)
