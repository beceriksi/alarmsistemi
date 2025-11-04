import os, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone

# --- Ayarlar ---
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN"); CHAT_ID=os.getenv("CHAT_ID")
MEXC="https://futures.mexc.com"

# --- Yardımcılar ---
def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None, retries=3, timeout=12):
    for _ in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
        except: time.sleep(0.5)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"})
    except: pass

# --- Teknik analiz ---
def ema(x,n): return x.ewm(span=n, adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs=up.ewm(alpha=1/n, adjust=False).mean()/(dn.ewm(alpha=1/n, adjust=False).mean()+1e-12)
    return 100-(100/(1+rs))
def adx(df,n=14):
    up=df['high'].diff(); dn=-df['low'].diff()
    plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    tr1=df['high']-df['low']; tr2=(df['high']-df['close'].shift()).abs(); tr3=(df['low']-df['close'].shift()).abs()
    tr=pd.DataFrame({'a':tr1,'b':tr2,'c':tr3}).max(axis=1)
    atr=tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di=100*pd.Series(plus).ewm(alpha=1/n, adjust=False).mean()/(atr+1e-12)
    minus_di=100*pd.Series(minus).ewm(alpha=1/n, adjust=False).mean()/(atr+1e-12)
    dx=((plus_di-minus_di).abs()/((plus_di+minus_di)+1e-12))*100
    return dx.ewm(alpha=1/n, adjust=False).mean()

def volume_spike(df, n=15, r=1.20):
    t=df['v'].astype(float)
    base_ema=t.ewm(span=n, adjust=False).mean()
    ratio=float(t.iloc[-1]/(base_ema.iloc[-2]+1e-12))
    return ratio>=r, ratio

def mexc_symbols():
    d=jget(f"{MEXC}/api/v1/contract/detail")
    if not d or "data" not in d: return []
    return [s["symbol"] for s in d["data"] if s.get("quoteCoin")=="USDT"]

def klines(sym, interval, limit=200):
    d=jget(f"{MEXC}/api/v1/contract/kline",{"symbol":sym,"interval":interval,"limit":limit})
    if not d or "data" not in d: return None
    try:
        df=pd.DataFrame(d["data"],columns=["ts","open","high","low","close","v"]).astype(
            {"open":"float64","high":"float64","low":"float64","close":"float64","v":"float64"})
        df=df.rename(columns={"close":"c"})
        return df
    except: return None

# --- Analiz ---
def analyze(sym, interval):
    df=klines(sym, interval, 200)
    if df is None or len(df)<60: return None
    if float(df["v"].iloc[-1])<150_000: return None
    c,h,l=df['c'],df['high'],df['low']

    rr=float(rsi(c,14).iloc[-1])
    e20,e50=ema(c,20).iloc[-1], ema(c,50).iloc[-1]
    trend_up=e20>e50
    adx_val=float(adx(pd.DataFrame({'high':h,'low':l,'close':c}),14).iloc[-1])
    v_ok,ratio=volume_spike(df)
    if not v_ok: return None

    side=None
    if trend_up and rr>50: side="BUY"
    elif (not trend_up) and rr<50: side="SELL"
    else: return None

    return f"- {sym} | {interval.upper()} | {side} | RSI:{rr:.1f} | ADX:{adx_val:.0f} | Hacim x{ratio:.2f}"

# --- Ana ---
def main():
    syms=mexc_symbols()
    if not syms: telegram("⚠️ Sembol listesi alınamadı (MEXC)."); return

    signals=[]
    for s in syms:
        for tf in ["15m","1h","4h","1d"]:
            try:
                res=analyze(s, tf)
                if res: signals.append(res)
            except: pass
        time.sleep(0.05)

    if signals:
        msg=f"⚡ *Çoklu Zaman Dilimi Sinyalleri*\n⏱ {ts()}\n\n" + "\n".join(signals[:60])
        telegram(msg)
    else:
        print("ℹ️ Sinyal yok (sessiz geçildi).")

if __name__=="__main__": main()
