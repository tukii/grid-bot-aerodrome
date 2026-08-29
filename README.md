# Grid Bot en Base (Aerodrome Slipstream)

Bot de trading de grid autónomo que opera el par **WETH/USDC** en el pool
Slipstream de Aerodrome (Base, chain 8453). Diseñado para una wallet quemable
con ~5$ de capital.

## Estado actual

- **Paper bot** (`bot.py run`): simulación continua en segundo plano, registra
  precios y trades ficticios en `data/paper.db`.
- **Live bot** (`bot.py live`): ejecución real o dry-run con la wallet quemable.

## Comandos

```bash
python bot.py backtest --timeframe d1    # Backtest del grid con datos históricos
python bot.py backtest --timeframe d1 --floating   # Backtest con grid flotante
python bot.py backtest --timeframe d1 --setup-cost 0.02  # Con coste inicial approve+wrap
python bot.py run                        # Paper trading en vivo (loop)
python bot.py live --dry-run             # Live en modo seco (no ejecuta tx)
python bot.py live                       # Live REAL (requiere live.enabled=true)
python bot.py setup                      # Validar config + balances
python bot.py prep --deploy              # Wrap ETH->WETH + rebalance 50/50
python bot.py status                     # Estado del paper bot
python status.py                         # Estado del live bot (on-chain real)
```

## Seguridad

- La clave privada está en `.env` (permisos 600), SOLO de la wallet quemable.
- Verificación on-chain del router (`factory()` coincide con el de la pool)
  antes de cada swap (protección anti-phishing), con caché de 5 min.
- Cada transacción espera su receipt y verifica `status==1` antes de considerarse
  exitosa. Trades revertidos se registran como NO ejecutados.
- Nonce management centralizado para evitar race conditions entre approve y swap.
- Reintentos con rotación entre 3 RPC públicos ante rate limits / timeouts.
- Stop-loss: si el total cae `stop_loss_pct`% desde el pico → unwrap todo a ETH
  y detener.
- `max_spend_usd`: tope duro de capital desplegado.
- Grid state persistido en SQLite: sobrevive reinicios sin re-ejecutar órdenes.

## Funcionalidad

- Re-anchoring automático del grid si el precio se aleja >25% del anchor.
- Rebalanceo automático 50/50 si la asignación se desvía >15%.
- Precio con fallback: DexScreener → GeckoTerminal → Quoter on-chain.
- Alertas Telegram opcionales (`alerts.telegram_bot_token`, `alerts.telegram_chat_id`).
- `status.py` reconcilia balances on-chain reales (WETH+USDC+ETH).

## Configuración clave (`config.yaml`)

| Parámetro | Valor | Significado |
|---|---|---|
| `pool.address` | `0x4e392fbf...` | Pool WETH/USDC **0.008% low-fee** Slipstream |
| `grid.spacing_pct` | 6.0 | Distancia entre niveles del grid |
| `grid.range_pct` | 30.0 | Rango ±30% alrededor del anchor |
| `live.router_address` | `0x698Cb2b6...` | SwapRouter Slipstream (verificado) |
| `live.quoter_address` | `0x514c8B5f...` | QuoterV2 (verificado) |
| `live.tick_spacing` | 1 | tickSpacing del pool low-fee |
| `live.stop_loss_pct` | 10.0 | Stop-loss % desde pico |
| `live.max_spend_usd` | 5.0 | Tope de capital |

## Costes por round-trip (comprar + vender)

| Coste | Valor |
|---|---|
| Gas (Base, 2 swaps) | ~$0.004 (55%) |
| Fee DEX (0.008%, 2 swaps) | ~$0.001 (45%) |
| **Total** | **~$0.005** |

Con spacing 6%, cada round-trip renta ~$0.30 bruto, así que el coste es ~1.7% del beneficio bruto. El pool low-fee (0.008% vs 0.033%) y el spacing amplio reducen los costes ~35-40% frente a la config inicial.

## Backtest (referencia)

- 116 días diarios, spacing 6%, range 30%, fee 0.008%: **+10.41%** (vs +7.31% con la config antigua)
- 42 días horario: **-3.77%** — las comisiones y el gas pesan en plazos cortos.
- El resultado real depende del mercado; hay riesgo de pérdida (limitada por el stop-loss).
