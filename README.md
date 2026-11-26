# attention-log

Public log of "attention" signals, collected every 10 minutes by GitHub Actions. No API keys.

One run = 10 HTTP calls, one gzip member appended to `data/YYYY-MM-DD.jsonl.gz` (UTC day).
Read it with `zcat data/2026-11-26.jsonl.gz` (concatenated gzip members are one valid gzip file).

## Sources (`s`)

| s | endpoint |
|---|---|
| ds_boosts_latest / ds_boosts_top | DexScreener `/token-boosts/latest/v1`, `/token-boosts/top/v1` |
| ds_profiles_latest | DexScreener `/token-profiles/latest/v1` |
| ds_takeovers_latest | DexScreener `/community-takeovers/latest/v1` |
| ds_ads_latest | DexScreener `/ads/latest/v1` |
| gt_trending_5m / 1h / 6h / 24h | GeckoTerminal trending pools, Solana |
| cg_trending | CoinGecko `/search/trending` (coins only) |

## Row fields

`t` run time UTC · `s` source · `r` rank (1 = first in the response) · `c` chain · `a` token address
(CoinGecko: coin id) · `pool` · `name`/`sym` · `px` USD price · `liq` pool liquidity USD · `mcap` · `fdv` ·
`vol_m5/h1/h6/h24` USD · `pc_h1/h24` % change · `buys_h1/sells_h1` · `created` pool creation time ·
DexScreener extras: `boost_total`, `boost_amt`, `cto`, `updated`, `claim`, `ad_*`.

**Absent key = not provided by that source.** DexScreener boost/profile/takeover/ad endpoints carry no
liquidity, volume or market cap; those exist only in the `gt_*` and `cg_trending` rows.

## Gaps

Every run ends with one `{"s":"_run"}` row listing, per call, the HTTP status, rows returned, attempts
and milliseconds. A source that failed (e.g. GeckoTerminal 429) has `n: 0` there: a missing snapshot is
a recorded gap, not silence. GitHub cron is best effort, so runs can start late or be skipped; count `_run`
rows per day to measure real coverage.
