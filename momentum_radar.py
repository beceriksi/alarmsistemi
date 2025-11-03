import os, time, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# --- Telegram Secrets ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Endpoints ---
MEXC_FAPI = "https://contract.mexc.com"
BINANCE = "https://api.binance.com"

# --- Utility ---
def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None, retries=3, timeout=10):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
        except: time.sleep(0.5)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except: pass

# --- Market Filters ---
def btc_strength():
    d = jget(f"{BINANCE}/api/v3/klines", {"symbol":"BTCUSDT","interval":"1h","limit":100})
    if not d: return "NÖTR"
    df = pd.DataFrame(d, columns=["t","o","h","l","c","v","ct","x1","x2","x3","x4","x5"]).astype(float)
    c = df["c"]; ema20 = c.ewm(span=20).mean().iloc[-1]; ema50 = c.ewm(span=50).mean().iloc[-1]
    return "GÜÇLÜ" if ema20>ema50 else "ZAYIF"

# --- Data ---
def mexc_symbols():
    d = jget(f"{MEXC_FAPI}/api/v1/contract/detail")
    if not d or "data" not in d: return []
    return [s["symbol"] for s in d["data"] if s.get("quoteCoin")=="USDT"]

def klines_mexc(sym, interval="15m", limit=100):
    d = jget(f"{MEXC_FAPI}/api/v1/contract/kline/{sym}", {"interval": interval, "limit": limit})
    if not d or "data" not in d: return None
    df = pd.DataFrame(d["data"], columns=["ts","open","high","low","close","volume","turnover"]).astype(
        {"open":"float64","high":"float64","low":"float64","close":"float64","volume":"float64","turnover":"float64"}
    )
    return df

# --- Core Logic ---
def detect_momentum(sym):
    df = klines_mexc(sym, "15m", 100)
    if df is None or len(df)<40: return None, "short"

    # Hacim oranı
    vol_now = df["volume"].iloc[-1]
    vol_base = df["volume"].iloc[-21:-1].mean()
    ratio = vol_now / (vol_base + 1e-12)
    if ratio < 1.5: return None, "lowvol"

    # Fiyat değişimi
    c = df["close"]
    chg = (c.iloc[-1] / c.iloc[-2] - 1) * 100
    if abs(chg) > 3 and abs(chg) < 8:
        # zaten hareket başlamış, uyarı geç
        return None, "moved"

    if abs(chg) <= 3:
        line = f"{sym} | Hacim x{ratio:.2f} | Fiyat {chg:+.2f}% | Hacim ısınması tespit edildi 🔥"
        return line, None

    return None, None

# --- Memory (anti-spam) ---
CACHE_FILE = "sent_cache.csv"

def load_sent():
    if not os.path.exists(CACHE_FILE):
        return {}
    df = pd.read_csv(CACHE_FILE)
    df["time"] = pd.to_datetime(df["time"])
    now = datetime.utcnow()
    df = df[df["time"] > now - timedelta(hours=2)]  # 2 saat sınırı
    return {row["sym"]: row["time"] for _, row in df.iterrows()}

def save_sent(sent_dict):
    df = pd.DataFrame(list(sent_dict.items()), columns=["sym","time"])
    df.to_csv(CACHE_FILE, index=False)

# --- Main ---
def main():
    btc_status = btc_strength()
    if btc_status == "ZAYIF":
        print("Piyasa zayıf, tarama atlandı.")
        return

    syms = mexc_symbols()
    if not syms:
        telegram("⚠️ MEXC sembol listesi alınamadı.")
        return

    sent = load_sent()
    new_sent = sent.copy()
    alerts = []

    for i, s in enumerate(syms):
        if s in sent: continue  # 2 saat içinde uyarı gönderme
        try:
            line, flag = detect_momentum(s)
            if line:
                alerts.append(f"- {line}")
                new_sent[s] = datetime.utcnow()
        except: pass
        if i % 15 == 0: time.sleep(0.25)

    save_sent(new_sent)

    if alerts:
        text = ["🚨 *Momentum Radar*",
                f"⏱ {ts()}",
                f"BTC: {btc_status}",
                "\n".join(alerts[:15]),
                f"\n📊 Toplam {len(alerts)} erken uyarı"]
        telegram("\n".join(text))
    else:
        print("Yeni momentum sinyali yok.")

if __name__ == "__main__":
    main()
