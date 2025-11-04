import os, requests, pandas as pd
from datetime import datetime
import numpy as np

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def telegram(msg):
    if TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            print("Telegram hatası:", e)
    else:
        print("TOKEN/CHAT_ID eksik:", msg)

def mexc_symbols():
    try:
        r = requests.get("https://futures.mexc.com/api/v1/contract/detail", timeout=10)
        data = r.json().get("data", [])
        return [d["symbol"] for d in data if "symbol" in d]
    except Exception as e:
        telegram(f"❌ Sembol listesi hatası: {e}")
        return []

def get_kline(symbol, interval="15m", limit=60):
    try:
        url = f"https://futures.mexc.com/api/v1/contract/kline?symbol={symbol}&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=10)
        data = r.json().get("data", [])
        df = pd.DataFrame(data, columns=["t","o","h","l","c","v"])
        df["c"] = df["c"].astype(float)
        df["v"] = df["v"].astype(float)
        return df
    except Exception as e:
        print(f"{symbol} hata: {e}")
        return None

def analyze(symbol, df):
    if df is None or len(df) < 20: return None
    close, vol = df["c"], df["v"]
    ratio = vol.iloc[-1] / (vol.rolling(10).mean().iloc[-2] + 1e-9)
    z = (np.log(vol.iloc[-1] + 1e-9) - np.log(vol.rolling(10).mean().iloc[-2] + 1e-9)) / 0.5
    trend_up = close.iloc[-1] > close.rolling(20).mean().iloc[-1]
    rsi = 100 - (100 / (1 + (close.diff().clip(lower=0).mean() / abs(close.diff().clip(upper=0)).mean())))
    if ratio > 1.15 and z > 0.8:
        if trend_up and rsi > 50:
            return f"🟢 BUY {symbol} | Hacim x{ratio:.2f} z:{z:.2f} RSI:{rsi:.1f}"
        elif not trend_up and rsi < 50:
            return f"🔴 SELL {symbol} | Hacim x{ratio:.2f} z:{z:.2f} RSI:{rsi:.1f}"
    return None

def main():
    telegram(f"🚀 Yeni tarama başladı: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    syms = mexc_symbols()
    telegram(f"📊 Toplam coin: {len(syms)}")

    found = []
    for s in syms[:100]:  # test için ilk 100 coin
        df = get_kline(s, "15m")
        res = analyze(s, df)
        if res: found.append(res)

    if found:
        msg = "📈 Sinyaller:\n" + "\n".join(found)
    else:
        msg = "ℹ️ Şu an sinyal yok."
    telegram(msg)
    telegram("✅ Tarama tamamlandı.")

if __name__ == "__main__":
    main()
