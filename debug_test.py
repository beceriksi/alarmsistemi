import os, requests, pandas as pd
from datetime import datetime

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
        print("TOKEN veya CHAT_ID eksik:", msg)

def mexc_symbols():
    try:
        r = requests.get("https://futures.mexc.com/api/v1/contract/detail", timeout=10)
        data = r.json().get("data", [])
        return [d["symbol"] for d in data if "symbol" in d]
    except Exception as e:
        telegram(f"❌ Sembol listesi hatası: {e}")
        return []

def get_kline(symbol):
    try:
        url = f"https://futures.mexc.com/api/v1/contract/kline/{symbol}?interval=15m&limit=20"
        r = requests.get(url, timeout=10)
        data = r.json().get("data", [])
        df = pd.DataFrame(data, columns=["t","o","h","l","c","v"])
        df["v"] = df["v"].astype(float)
        return df
    except Exception as e:
        telegram(f"⚠️ {symbol} verisi alınamadı: {e}")
        return None

def main():
    telegram(f"✅ Bot test başlatıldı: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    syms = mexc_symbols()
    telegram(f"📊 Coin sayısı: {len(syms)}")

    if not syms:
        telegram("❌ Coin listesi boş! MEXC API yanıt vermiyor.")
        return

    for sym in syms[:3]:
        df = get_kline(sym)
        if df is None or len(df) < 5:
            telegram(f"⚠️ {sym}: veri yok veya kısa.")
            continue
        vol_now = df["v"].iloc[-1]
        vol_avg = df["v"].rolling(10).mean().iloc[-2]
        telegram(f"🧪 {sym} | son: {vol_now:.2f} ort: {vol_avg:.2f}")

    telegram("✅ Test tamamlandı.")

if __name__ == "__main__":
    main()
