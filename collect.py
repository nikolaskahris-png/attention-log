#!/usr/bin/env python3
"""attention-log collector. Stdlib only, no keys. One run = 10 HTTP calls.

Appends one gzip member to data/YYYY-MM-DD.jsonl.gz (UTC day). Concatenated
gzip members are a valid gzip file: `zcat data/2026-11-26.jsonl.gz` reads all.
Appending (not rewriting) keeps git deltas small: only the new bytes change.
"""
import gzip, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

UA = "attention-log/1.0 (public research collector)"
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?duration="
CG = "https://api.coingecko.com/api/v3/search/trending"
# (source key, url, pause in seconds BEFORE the call)
SOURCES = [
    ("ds_boosts_latest", DS + "/token-boosts/latest/v1", 0),
    ("ds_boosts_top", DS + "/token-boosts/top/v1", 1),
    ("ds_profiles_latest", DS + "/token-profiles/latest/v1", 1),
    ("ds_takeovers_latest", DS + "/community-takeovers/latest/v1", 1),
    ("ds_ads_latest", DS + "/ads/latest/v1", 1),
    ("gt_trending_5m", GT + "5m", 2),
    ("cg_trending", CG, 4),            # different host: sits between GT calls to space them
    ("gt_trending_1h", GT + "1h", 4),  # GT free tier ~10 calls/min -> keep >= 8 s between GT calls
    ("gt_trending_6h", GT + "6h", 8),
    ("gt_trending_24h", GT + "24h", 8),
]
RETRY_WAIT = (25, 45)  # waits after 1st and 2nd failure -> max 3 attempts (retries spend rate budget too)


def fetch(url):
    """Returns (data|None, http_status|None, attempts, error|None)."""
    last = (None, "unknown")
    attempt = 0
    for attempt in range(1, len(RETRY_WAIT) + 2):
        wait = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8")), r.status, attempt, None
        except urllib.error.HTTPError as e:
            last = (e.code, f"HTTP {e.code}")
            if e.code not in (429, 500, 502, 503, 504):
                return None, e.code, attempt, last[1]
            ra = e.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = min(int(ra), 60)
        except Exception as e:  # network, timeout, bad json
            last = (None, f"{type(e).__name__}: {e}"[:120])
        if attempt <= len(RETRY_WAIT):
            time.sleep(wait or RETRY_WAIT[attempt - 1])
    return None, last[0], attempt, last[1]


def num(x, nd=2):
    """float rounded to nd decimals (nd=None: 6 significant digits, for prices)."""
    try:
        if x is None:
            return None
        f = float(x)
        return float("%.6g" % f) if nd is None else round(f, nd)
    except (TypeError, ValueError):
        return None


def money(s):  # "$1,363,636,462" -> float
    if not isinstance(s, str):
        return num(s)
    return num(s.replace("$", "").replace(",", "").strip())


def norm_ds(data):
    rows = []
    for i, x in enumerate(data if isinstance(data, list) else [], 1):
        r = {"r": i, "c": x.get("chainId"), "a": x.get("tokenAddress")}
        for k, o in (("totalAmount", "boost_total"), ("amount", "boost_amt"), ("cto", "cto"),
                     ("updatedAt", "updated"), ("claimDate", "claim"), ("date", "ad_date"),
                     ("type", "ad_type"), ("impressions", "ad_impr"), ("durationHours", "ad_hours")):
            if x.get(k) is not None:
                r[o] = x[k]
        rows.append(r)
    return rows


def norm_gt(data):
    rows = []
    for i, p in enumerate((data or {}).get("data", []), 1):
        a = p.get("attributes", {})
        rel = p.get("relationships", {})

        def tid(k):
            return ((rel.get(k) or {}).get("data") or {}).get("id", "")

        v, pc, tx = a.get("volume_usd") or {}, a.get("price_change_percentage") or {}, a.get("transactions") or {}
        rows.append({
            "r": i, "c": "solana", "a": tid("base_token").removeprefix("solana_") or None,
            "pool": a.get("address"), "name": a.get("name"),
            "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
            "quote": tid("quote_token").removeprefix("solana_") or None,
            "px": num(a.get("base_token_price_usd"), None), "liq": num(a.get("reserve_in_usd")),
            "mcap": num(a.get("market_cap_usd")), "fdv": num(a.get("fdv_usd")),
            "vol_m5": num(v.get("m5")), "vol_h1": num(v.get("h1")),
            "vol_h6": num(v.get("h6")), "vol_h24": num(v.get("h24")),
            "pc_h1": num(pc.get("h1")), "pc_h24": num(pc.get("h24")),
            "buys_h1": (tx.get("h1") or {}).get("buys"), "sells_h1": (tx.get("h1") or {}).get("sells"),
            "created": a.get("pool_created_at"),
        })
    return rows


def norm_cg(data):
    rows = []
    for i, c in enumerate((data or {}).get("coins", []), 1):
        it = c.get("item", {})
        d = it.get("data") or {}
        rows.append({
            "r": i, "a": it.get("id"), "sym": it.get("symbol"), "name": it.get("name"),
            "cg_rank": it.get("market_cap_rank"), "px": num(d.get("price"), None),
            "mcap": money(d.get("market_cap")), "vol_h24": money(d.get("total_volume")),
        })
    return rows


def normalize(src, data):
    if src.startswith("ds_"):
        return norm_ds(data)
    if src.startswith("gt_"):
        return norm_gt(data)
    return norm_cg(data)


def iso_ms(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def main_pair(pairs, chain, addr):
    """Main pair = highest liquidity.usd among pairs where the token is the base token."""
    same = (lambda x: x == addr) if chain == "solana" else (lambda x: (x or "").lower() == addr.lower())
    mine = [p for p in pairs if same((p.get("baseToken") or {}).get("address"))]
    if not mine:
        return None, 0
    return max(mine, key=lambda p: num((p.get("liquidity") or {}).get("usd")) or 0), len(mine)


def enrich_row(chain, addr, pairs):
    p, n = main_pair(pairs, chain, addr)
    r = {"r": None, "c": chain, "a": addr, "np": n}
    if p:
        v, tx, liq = p.get("volume") or {}, p.get("txns") or {}, p.get("liquidity") or {}
        r.update({
            "pool": p.get("pairAddress"), "dex": p.get("dexId"), "sym": (p.get("baseToken") or {}).get("symbol"),
            "quote": (p.get("quoteToken") or {}).get("symbol"),
            "px": num(p.get("priceUsd"), None), "liq": num(liq.get("usd")),
            "mcap": num(p.get("marketCap")), "fdv": num(p.get("fdv")),
            "vol_m5": num(v.get("m5")), "vol_h1": num(v.get("h1")), "vol_h24": num(v.get("h24")),
            "buys_m5": (tx.get("m5") or {}).get("buys"), "sells_m5": (tx.get("m5") or {}).get("sells"),
            "buys_h1": (tx.get("h1") or {}).get("buys"), "sells_h1": (tx.get("h1") or {}).get("sells"),
            "created": iso_ms(p.get("pairCreatedAt")),
        })
    r.pop("r")
    return r


def enrich(lines):
    """Unique (chain, address) of the 5 DexScreener endpoints -> /tokens/v1/{chain}/{a,b,...} (max 30, 300 rpm)."""
    uniq = {}
    for l in lines:
        if l["s"].startswith("ds_") and l.get("c") and l.get("a"):
            uniq.setdefault(l["c"], {})[l["a"]] = 1
    out, meta = [], []
    for chain, addrs in uniq.items():
        addrs = list(addrs)
        for i in range(0, len(addrs), 30):
            batch = addrs[i:i + 30]
            time.sleep(0.4)
            s0 = time.time()
            data, status, attempts, err = fetch(f"{DS}/tokens/v1/{chain}/{','.join(batch)}")
            m = {"src": f"ds_tokens:{chain}", "status": status, "n": len(batch), "tries": attempts,
                 "ms": int((time.time() - s0) * 1000)}
            if err:
                m["err"] = err
            else:
                by = {}
                for pr in data if isinstance(data, list) else []:
                    by.setdefault(((pr.get("baseToken") or {}).get("address") or "").lower(), []).append(pr)
                for a in batch:
                    out.append(enrich_row(chain, a, by.get(a.lower(), [])))
            meta.append(m)
            print(json.dumps(m), flush=True)
    return out, meta


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    os.makedirs(out_dir, exist_ok=True)
    t0 = datetime.now(timezone.utc)
    ts = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines, meta, ok = [], [], 0
    for src, url, pause in SOURCES:
        time.sleep(pause)
        s = time.time()
        data, status, attempts, err = fetch(url)
        rows = normalize(src, data) if data is not None else []
        for r in rows:
            lines.append({"t": ts, "s": src, **{k: v for k, v in r.items() if v is not None}})
        ok += data is not None
        m = {"src": src, "status": status, "n": len(rows), "tries": attempts, "ms": int((time.time() - s) * 1000)}
        if err:
            m["err"] = err
        meta.append(m)
        print(json.dumps(m), flush=True)
    erows, emeta = enrich(lines)
    for r in erows:
        lines.append({"t": ts, "s": "ds_enrich", **{k: v for k, v in r.items() if v is not None}})
    n_rows = len(lines)
    lines.append({"t": ts, "s": "_run", "ok": ok, "of": len(SOURCES),
                  "enr_ok": sum("err" not in m for m in emeta), "enr_of": len(emeta), "calls": meta + emeta})
    path = os.path.join(out_dir, t0.strftime("%Y-%m-%d") + ".jsonl.gz")
    payload = ("\n".join(json.dumps(l, separators=(",", ":"), ensure_ascii=False) for l in lines) + "\n").encode("utf-8")
    with gzip.GzipFile(path, "ab", compresslevel=9) as f:  # 'ab' = new member appended
        f.write(payload)
    print(f"{ts} sources_ok={ok}/{len(SOURCES)} rows={n_rows} raw={len(payload)}B -> {path}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
