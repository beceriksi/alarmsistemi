import os, time, requests, pandas as pd
from datetime import datetime, timezone

# === Ayarlar ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

COINGECKO = "https://api.coingecko.com/api/v3"
OKX = "https://www.okx.com"
THRESHOLD = float(os.getenv("THRESHOLD", "2000000"))   # 2 milyon USDT
TOP_N = int(os.getenv("TOP_N", "100"))                  # ilk 100 coin
BAR = "3m"                                              # 3 dakikalık mum
CHECK_DELAY = 0.15                                      # rate limit

# === Yardımcılar ===
def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code == 200: return r.json()
    except: pass
    return None

def okxget(path, params=None):
    try:
        r = requests.get(OKX+path, params=params, timeout=12)
        if r.status_code == 200:
            j = r.json()
            if j.get("code") == "0":
                return j.get("data")
    except: pass
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except: pass

# === Coin listesi ===
def top100_symbols():
    data = jget(f"{COINGECKO}/coins/markets",
                {"vs_currency": "usd", "order": "market_cap_desc", "per_page": TOP_N, "page": 1})
    if not data: return []
    return [x["symbol"].upper() for x in data if "symbol" in x]

def okx_pairs(bases):
    tickers = okxget("/api/v5/market/tickers", {"instType": "SPOT"}) or []
    mapping = {}
    for t in tickers:
        inst = t.get("instId", "")
        if "-" not in inst: continue
        base, quote = inst.split("-", 1)
        if quote.upper() in ("USDT", "USD"):
            mapping[base.upper()] = inst
    return [mapping[b] for b in bases if b in mapping]

# === Kline çek ===
def get_turnover(inst, bar="3m"):
    d = okxget("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": 2})
    if not d: return 0.0
    try:
        return float(d[0][6])  # volCcy = quote ccy hacmi (USDT)
    except:
        return 0.0

# === Ana ===
def main():
    bases = top100_symbols()
    pairs = okx_pairs(bases)
    if not pairs:
        telegram(f"⛔ {ts()} — OKX'ten parite listesi alınamadı.")
        return

    hits = []
    for p in pairs:
        try:
            v = get_turnover(p, BAR)
            if v >= THRESHOLD:
                hits.append((p, v))
        except: pass
        time.sleep(CHECK_DELAY)

    if not hits:
        print(f"{ts()} — Büyük alım yok.")
        return

    hits.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"🐳 *OKX Whale Flow Radar*",
        f"⏱ {ts()}",
        f"📊 Zaman dilimi: {BAR}",
        f"💰 Eşik: ≥ {THRESHOLD:,.0f} USDT (tekrarlayan alımlar dahil)\n"
    ]
    for inst, val in hits:
        tag = "🚨" if val >= THRESHOLD * 2 else "💵"
        lines.append(f"{tag} {inst} — {val:,.0f} USDT / {BAR}")

    telegram("\n".join(lines))

if __name__ == "__main__":
    main()
