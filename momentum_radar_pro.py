import os, time, requests, json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# --- Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Endpoints
MEXC_FAPI = "https://contract.mexc.com"
BINANCE = "https://api.binance.com"
COINGECKO = "https://api.coingecko.com/api/v3/global"

# --- Utils
def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None, retries=3, timeout=12):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
        except: time.sleep(0.4)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except: pass

def ema(x,n): return x.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / (dn.ewm(alpha=1/n, adjust=False).mean() + 1e-12)
    return 100 - (100/(1+rs))

def adx(df, n=14):
    up = df['high'].diff(); dn = -df['low'].diff()
    plus = np.where((up>dn)&(up>0), up, 0.0)
    minus = np.where((dn>up)&(dn>0), dn, 0.0)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift()).abs()
    tr3 = (df['low'] - df['close'].shift()).abs()
    tr = pd.DataFrame({'a':tr1,'b':tr2,'c':tr3}).max(axis=1)
    atr = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus).ewm(alpha=1/n, adjust=False).mean() / (atr + 1e-12)
    minus_di = 100 * pd.Series(minus).ewm(alpha=1/n, adjust=False).mean() / (atr + 1e-12)
    dx = ((plus_di - minus_di).abs() / ((plus_di + minus_di) + 1e-12)) * 100
    return dx.ewm(alpha=1/n, adjust=False).mean()

def btc_note():
    d = jget(f"{BINANCE}/api/v3/klines", {"symbol":"BTCUSDT","interval":"1h","limit":120})
    if not d: return "BTC: veri yok"
    df = pd.DataFrame(d, columns=["t","o","h","l","c","v","ct","x1","x2","x3","x4","x5"]).astype(float)
    c = df["c"]; e20 = ema(c,20).iloc[-1]; e50 = ema(c,50).iloc[-1]; r = float(rsi(c,14).iloc[-1])
    state = "GÜÇLÜ" if (e20>e50 and r>50) else ("ZAYIF" if (e20<e50 and r<50) else "NÖTR")
    return f"BTC(1H): {state}"

def usdt_d_note():
    g = jget(COINGECKO)
    try:
        usdt = float(g["data"]["market_cap_percentage"]["usdt"])
        tag = " (riskten kaçış)" if usdt>=7 else (" (risk alımı)" if usdt<=5 else "")
        return f"USDT.D: {usdt:.1f}%{tag}"
    except:
        return "USDT.D: veri yok"

def mexc_symbols():
    d = jget(f"{MEXC_FAPI}/api/v1/contract/detail")
    if not d or "data" not in d: return []
    return [s["symbol"] for s in d["data"] if s.get("quoteCoin")=="USDT"]

def klines_mexc(sym, interval="15m", limit=120):
    d = jget(f"{MEXC_FAPI}/api/v1/contract/kline/{sym}", {"interval": interval, "limit": limit})
    if not d or "data" not in d: return None
    df = pd.DataFrame(d["data"], columns=["ts","open","high","low","close","volume","turnover"]).astype(
        {"open":"float64","high":"float64","low":"float64","close":"float64","volume":"float64","turnover":"float64"}
    )
    return df

def funding(sym):
    d = jget(f"{MEXC_FAPI}/api/v1/contract/funding_rate", {"symbol": sym})
    try: return float(d["data"]["fundingRate"])
    except: return None

# --- Pro: Hacim tetik (engelleyici değil; sinyal için ana koşul)
def volume_ok(df, n=10, ratio_min=1.15, z_min=0.8, ramp_min=1.3):
    t = df['turnover'].astype(float)
    if len(t) < max(3, n+2): return False, {"ratio":1.0,"z":0.0,"ramp":1.0}
    base_ema = t.ewm(span=n, adjust=False).mean()
    ratio = float(t.iloc[-1] / (base_ema.iloc[-2] + 1e-12))
    roll = t.rolling(n)
    mu = np.log((roll.median().iloc[-1] or 1e-12) + 1e-12)
    sd = np.log((roll.std().iloc[-1] or 1e-12) + 1e-12)
    z = (np.log(t.iloc[-1] + 1e-12) - mu) / (sd + 1e-12)
    ramp = float(t.iloc[-3:].sum() / ((roll.mean().iloc[-1] * 3) + 1e-12))

    # Dinamik sıkılaştırma (çok likitlerde)
    if t.iloc[-1] > 5_000_000:
        ratio_min = max(ratio_min, 1.25); z_min = max(z_min, 1.0); ramp_min = max(ramp_min, 1.4)

    ok = (ratio >= ratio_min) or (z >= z_min) or (ramp >= ramp_min)
    return ok, {"ratio":ratio, "z":z, "ramp":ramp}

# --- Pro: Likidite bölgesi (uyarı metni; sinyali engellemez)
def liq_zone_warn(df, look=50, tol=0.005):
    h50 = float(df["high"].tail(look).max())
    l50 = float(df["low"].tail(look).min())
    p = float(df["close"].iloc[-1])
    up_near = (abs(p - h50) / h50) <= tol
    dn_near = (abs(p - l50) / h50) <= tol
    if up_near: return "⚠️ Likidite (üst bölge)"
    if dn_near: return "⚠️ Likidite (alt bölge)"
    return None

# --- Pro: Momentum notu (ADX + RSI delta; engellemez)
def momentum_note(df):
    hlc = pd.DataFrame({"high":df["high"], "low":df["low"], "close":df["close"]})
    adx_v = float(adx(hlc,14).iloc[-1]) if len(df) > 20 else 0.0
    r = rsi(df["close"],14)
    r_delta = float(r.iloc[-1] - r.iloc[-3]) if len(r) >= 3 else 0.0
    tag = "Güçlü" if adx_v>=20 else ("Orta" if adx_v>=10 else "Zayıf")
    drift = "↑" if r_delta>0 else ("↓" if r_delta<0 else "→")
    return f"Momentum: {tag} (ADX:{adx_v:.0f}, RSIΔ:{r_delta:+.1f}{drift})"

# --- Anti-spam cache
CACHE = "sent_15m_cache.csv"
def load_cache():
    if not os.path.exists(CACHE): return {}
    df = pd.read_csv(CACHE)
    df["time"] = pd.to_datetime(df["time"])
    now = datetime.utcnow()
    df = df[df["time"] > now - timedelta(minutes=60)]  # 60 dk cooldown
    return {row["sym"]: row["time"] for _, row in df.iterrows()}
def save_cache(d):
    pd.DataFrame(list(d.items()), columns=["sym","time"]).to_csv(CACHE, index=False)

def analyze(sym):
    df = klines_mexc(sym, "15m", 120)
    if df is None or len(df) < 40: return None, "short"

    # likidite tabanı
    if float(df["turnover"].iloc[-1]) < 200_000: return None, "lowliq"

    # fiyat değişimi (erken yakalama; aşırı hareketleri atla)
    c = df["close"]
    chg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
    if abs(chg) > 3.0: return None, "moved"

    # ana hacim tetik
    v_ok, v = volume_ok(df, n=10, ratio_min=1.15, z_min=0.8, ramp_min=1.3)
    if not v_ok: return None, "lowvol"

    # trend & rsi (yön tayini)
    e20, e50 = ema(c,20).iloc[-1], ema(c,50).iloc[-1]
    rr = float(rsi(c,14).iloc[-1])
    trend_up = e20 > e50
    side = "BUY" if (trend_up and rr>50) else ("SELL" if ((not trend_up) and rr<50) else None)
    if side is None: return None, "side"

    # Uyarılar (ENGELLEMEZ)
    liq_warn = liq_zone_warn(df, look=50, tol=0.005)
    m_note = momentum_note(df)
    fr = funding(sym)
    fr_txt = f" | Funding:+{fr:.3f}" if (fr is not None and fr>0.01) else (f" | Funding:{fr:.3f}" if (fr is not None and fr<-0.01) else "")

    # Sinyal satırı
    line = (f"{sym} | 15m | {side} | Trend:{'↑' if trend_up else '↓'} | RSI:{rr:.1f} | "
            f"Hacim x{v['ratio']:.2f} z:{v['z']:.2f} ramp:{v['ramp']:.2f} | "
            f"{m_note}{fr_txt}")
    if liq_warn: line += f" | {liq_warn}"
    return line, None

def main():
    head = f"🚨 *15m Momentum Alarm (Pro)*\n⏱ {ts()}\n{btc_note()} | {usdt_d_note()}"
    syms = mexc_symbols()
    if not syms:
        telegram("⚠️ MEXC sembol listesi alınamadı."); return

    sent = load_cache()
    new_sent = sent.copy()
    alerts = []

    for i, s in enumerate(syms):
        if s in sent: 
            continue
        try:
            line, flag = analyze(s)
            if line:
                alerts.append(f"- {line}")
                new_sent[s] = datetime.utcnow()
        except: pass
        if i % 15 == 0: time.sleep(0.25)

    save_cache(new_sent)

    if alerts:
        text = head + "\n" + "\n".join(alerts[:20]) + f"\n\n📊 Toplam {len(alerts)} uyarı"
        telegram(text)
    else:
        print("15m: yeni uyarı yok.", ts())

if __name__ == "__main__":
    main()
