import os
import time
import requests
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Parametreler ---
TOP_LIMIT = 150               # En çok hacimli 150 spot USDT coini
BAR = "4H"                    # 4 saatlik sistem
CANDLE_LIMIT = 220
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

STRUCT_LOOKBACK = 20          # MSB ve FVG için bakılacak mum sayısı
MSB_BREAK_PCT = 0.0025        # MSB kırılım eşiği ~ %0.25
ZONE_BUFFER = 0.002           # %0.2 marj (FVG değerlendirmesinde)

MIN_CONDITIONS_LONG = 3       # LONG için 4 şarttan min kaç tanesi
MIN_CONDITIONS_SHORT = 3      # SHORT için


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ------------ HTTP Yardımcıları ------------

def jget(url, params=None, retries=3, timeout=10):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and j.get("code") == "0" and j.get("data"):
                    return j["data"]
        except Exception:
            time.sleep(0.5)
    return None


def jget_raw(url, params=None, retries=3, timeout=10):
    """CoinGecko gibi 'code' alanı olmayan JSON endpoint'ler için."""
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.5)
    return None


def telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠ TELEGRAM_TOKEN veya CHAT_ID yok, mesaj gönderemem.")
        print("--- Mesaj içeriği ---")
        print(msg)
        print("---------------------")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram hata:", r.text)
    except Exception as e:
        print("Telegram exception:", e)


# ------------ OKX Yardımcıları ------------

def get_spot_usdt_top_symbols(limit=TOP_LIMIT):
    """
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk N'i alır.
    instId formatı: BTC-USDT, HBAR-USDT vs.
    """
    url = f"{OKX_BASE}/api/v5/market/tickers"
    params = {"instType": "SPOT"}
    data = jget(url, params=params)
    if not data:
        return []

    rows = []
    for d in data:
        inst_id = d.get("instId", "")
        if not inst_id.endswith("-USDT"):
            continue
        volCcy24h = d.get("volCcy24h")
        try:
            vol_quote = float(volCcy24h)
        except Exception:
            vol_quote = 0.0
        rows.append((inst_id, vol_quote))

    rows.sort(key=lambda x: x[1], reverse=True)
    symbols = [r[0] for r in rows[:limit]]
    return symbols


def get_candles(inst_id, bar=BAR, limit=CANDLE_LIMIT):
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": limit}
    data = jget(url, params=params)
    if not data:
        return []

    # OKX en yeni mum en üstte verir → kronolojik sıraya çevirelim
    data = list(reversed(data))

    candles = []
    for row in data:
        # [ts, o, h, l, c, vol, volCcy, ...]
        try:
            ts_ms = int(row[0])
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
        except Exception:
            continue
        candles.append(
            {
                "ts": ts_ms,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            }
        )
    return candles


def get_trades(inst_id, limit=TRADES_LIMIT, max_age_sec=900):
    """
    Son 'limit' trade içinden sadece son max_age_sec (örn 15 dk) içindekileri kullan.
    """
    url = f"{OKX_BASE}/api/v5/market/trades"
    params = {"instId": inst_id, "limit": limit}
    data = jget(url, params=params)
    if not data:
        return []

    now_ms = int(time.time() * 1000)
    max_age_ms = max_age_sec * 1000

    recent = []
    for t in data:
        try:
            ts_ms = int(t.get("ts"))
        except Exception:
            continue
        if now_ms - ts_ms <= max_age_ms:
            recent.append(t)
    return recent


def get_orderbook(inst_id, depth=ORDERBOOK_DEPTH):
    url = f"{OKX_BASE}/api/v5/market/books"
    params = {"instId": inst_id, "sz": depth}
    data = jget(url, params=params)
    if not data:
        return None

    book = data[0]
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    def sum_notional(levels):
        total = 0.0
        for lvl in levels:
            try:
                px = float(lvl[0])
                sz = float(lvl[1])
                total += px * sz
            except Exception:
                continue
        return total

    bid_notional = sum_notional(bids)
    ask_notional = sum_notional(asks)

    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None

    return {
        "bid_notional": bid_notional,
        "ask_notional": ask_notional,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


# ------------ Marketcap & Whale Eşikleri ------------

def fetch_coingecko_mcaps(bases):
    """
    CoinGecko /coins/markets ile mcap al.
    Sembol üzerinden eşler (aynı sembol birden fazla coin olabilir, en yüksek mcapi alır).
    """
    bases_set = set(bases)
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": 1,
        "sparkline": "false",
    }
    data = jget_raw(url, params=params) or []
    result = {}

    for row in data:
        sym = str(row.get("symbol", "")).upper()
        mcap = row.get("market_cap")
        if not mcap or sym not in bases_set:
            continue
        # Aynı sembol birden fazla coine ait olabilir → en büyük mcap'i al
        if sym not in result or mcap > result[sym]:
            result[sym] = mcap

    return result


def classify_segment_and_thresholds(mcap):
    """
    mcap'e göre segment ve whale eşikleri döndürür.
    Segment:
      - HIGH: >= 10B
      - MID:  >= 1B
      - LOW:  < 1B veya bilinmiyorsa
    Eşikler:
      S, M, X (USD)
    """
    if mcap is None:
        segment = "LOW"
        S = 80_000
        M = 150_000
        X = 300_000
    else:
        if mcap >= 10_000_000_000:
            segment = "HIGH"
            S = 500_000
            M = 1_000_000
            X = 1_500_000
        elif mcap >= 1_000_000_000:
            segment = "MID"
            S = 200_000
            M = 400_000
            X = 800_000
        else:
            segment = "LOW"
            S = 80_000
            M = 150_000
            X = 300_000

    thresholds = {"S": S, "M": M, "X": X}
    return segment, thresholds


# ------------ Teknik Hesaplar / Trend ------------

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def classify_trend(candles):
    """
    4H trend sınıflandırma: UP / DOWN / RANGE (EMA50 & EMA200).
    """
    closes = [c["close"] for c in candles]
    if len(closes) < 60:
        return "RANGE"

    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    last = closes[-1]

    if ema200 is None or ema50 is None:
        return "RANGE"

    if last > ema200 * 1.01 and ema50 > ema200:
        return "UP"
    elif last < ema200 * 0.99 and ema50 < ema200:
        return "DOWN"
    else:
        return "RANGE"


# ------------ Orderflow & Whale Analizi ------------

def classify_whale_class(usd, thresholds):
    S = thresholds["S"]
    M = thresholds["M"]
    X = thresholds["X"]
    if usd >= X:
        return "X"
    elif usd >= M:
        return "M"
    elif usd >= S:
        return "S"
    return None


def analyze_trades_orderflow(trades, thresholds):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - En büyük buy whale (S/M/X)
    - En büyük sell whale (S/M/X)
    - En büyük X-class buy/sell whale (X veya None)
    """
    buy_notional = 0.0
    sell_notional = 0.0
    biggest_buy_whale = None
    biggest_sell_whale = None
    x_buy_whale = None
    x_sell_whale = None

    for t in trades:
        try:
            px = float(t.get("px"))
            sz = float(t.get("sz"))
            side = t.get("side", "").lower()
        except Exception:
            continue

        usd = px * abs(sz)
        if side == "buy":
            buy_notional += usd
            wcls = classify_whale_class(usd, thresholds)
            if wcls:
                if (biggest_buy_whale is None) or (usd > biggest_buy_whale["usd"]):
                    biggest_buy_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": usd,
                        "cls": wcls,
                        "side": side,
                        "ts": t.get("ts"),
                    }
                if wcls == "X":
                    if (x_buy_whale is None) or (usd > x_buy_whale["usd"]):
                        x_buy_whale = {
                            "px": px,
                            "sz": sz,
                            "usd": usd,
                            "cls": wcls,
                            "side": side,
                            "ts": t.get("ts"),
                        }

        elif side == "sell":
            sell_notional += usd
            wcls = classify_whale_class(usd, thresholds)
            if wcls:
                if (biggest_sell_whale is None) or (usd > biggest_sell_whale["usd"]):
                    biggest_sell_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": usd,
                        "cls": wcls,
                        "side": side,
                        "ts": t.get("ts"),
                    }
                if wcls == "X":
                    if (x_sell_whale is None) or (usd > x_sell_whale["usd"]):
                        x_sell_whale = {
                            "px": px,
                            "sz": sz,
                            "usd": usd,
                            "cls": wcls,
                            "side": side,
                            "ts": t.get("ts"),
                        }

    net_delta = buy_notional - sell_notional

    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_delta": net_delta,
        "buy_whale": biggest_buy_whale,
        "sell_whale": biggest_sell_whale,
        "has_buy_whale": biggest_buy_whale is not None,
        "has_sell_whale": biggest_sell_whale is not None,
        "x_buy_whale": x_buy_whale,
        "x_sell_whale": x_sell_whale,
        "has_x_buy": x_buy_whale is not None,
        "has_x_sell": x_sell_whale is not None,
        "thresholds": thresholds,
    }


# ---- Market Structure Break (MSB) ----

def detect_bullish_msb(candles, lookback=STRUCT_LOOKBACK):
    """
    Bullish MSB (sıkı):
    - Son lookback içindeki en yüksek kapanış alınır
    - Son mum bu seviyenin en az %0.25 üstünde kapanmışsa bullish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = max(closes)
    last_close = candles[-1]["close"]

    if last_close > level * (1 + MSB_BREAK_PCT):
        return True, level
    return False, level


def detect_bearish_msb(candles, lookback=STRUCT_LOOKBACK):
    """
    Bearish MSB (sıkı):
    - Son lookback içindeki en düşük kapanış alınır
    - Son mum bu seviyenin en az %0.25 altına kırdıysa bearish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = min(closes)
    last_close = candles[-1]["close"]

    if last_close < level * (1 - MSB_BREAK_PCT):
        return True, level
    return False, level


# ---- FVG (Fair Value Gap) Tespiti ----

def find_recent_fvg(candles, lookback=STRUCT_LOOKBACK):
    """
    Basit FVG:
    - i-2 ve i mumları arasında gap varsa:
      Bullish FVG: high(i-2) < low(i) → gap aşağıda, destek bölgesi
      Bearish FVG: low(i-2) > high(i) → gap yukarıda, direnç bölgesi
    Son lookback içinde en son görülen FVG'yi döndürür.
    """
    n = len(candles)
    if n < 3:
        return None

    start = max(2, n - lookback)
    last_fvg = None

    for i in range(start, n):
        c1 = candles[i - 2]
        c3 = candles[i]

        # Bullish FVG (gap aşağıda)
        if c1["high"] < c3["low"]:
            zone_low = c1["high"]
            zone_high = c3["low"]
            last_fvg = {
                "type": "bullish",
                "low": zone_low,
                "high": zone_high,
            }

        # Bearish FVG (gap yukarıda)
        if c1["low"] > c3["high"]:
            zone_low = c3["high"]
            zone_high = c1["low"]
            last_fvg = {
                "type": "bearish",
                "low": zone_low,
                "high": zone_high,
            }

    return last_fvg


def check_fvg_rejection(candles, fvg):
    """
    Son mum için FVG rejection:
    - Bullish: son mum fitili FVG içine girip, kapanış daha yukarı (destek)
    - Bearish: son mum fitili FVG içine girip, kapanış daha aşağı (direnç)
    """
    if not fvg or len(candles) < 1:
        return False

    last = candles[-1]
    low = last["low"]
    high = last["high"]
    close = last["close"]
    op = last["open"]

    z_low = fvg["low"]
    z_high = fvg["high"]

    touched = not (high < z_low or low > z_high)
    if not touched:
        return False

    if fvg["type"] == "bullish":
        if close > op and close > (z_low * (1 + ZONE_BUFFER / 2)):
            return True
        return False

    if fvg["type"] == "bearish":
        if close < op and close < (z_high * (1 - ZONE_BUFFER / 2)):
            return True
        return False

    return False


# ------------ Sembol Analizi (LONG + SHORT) ------------

def analyze_symbol(inst_id, btc_trend_global, base_info):
    """
    Tek coin için:
    - 4H trend (UP / DOWN / RANGE)
    - FVG + MSB yapısı
    - Orderflow + whale + orderbook (dinamik S/M/X eşikleri)
    SHORT: sadece kendi trendi DOWN ve BTC trend DOWN iken.
    BTC/ETH/XRP için SHORT üretme.
    """
    base = inst_id.split("-")[0]
    info = base_info.get(base, {})
    segment = info.get("segment", "LOW")
    thresholds = info.get("thresholds", {"S": 80_000, "M": 150_000, "X": 300_000})

    # Delta eşiklerini segmente göre ayarla
    if segment == "HIGH":
        NET_DELTA_MIN_POS = 300_000
        NET_DELTA_MIN_NEG = -300_000
        ORDERBOOK_IMB_RATIO = 1.4
    elif segment == "MID":
        NET_DELTA_MIN_POS = 150_000
        NET_DELTA_MIN_NEG = -150_000
        ORDERBOOK_IMB_RATIO = 1.4
    else:  # LOW
        NET_DELTA_MIN_POS = 80_000
        NET_DELTA_MIN_NEG = -80_000
        ORDERBOOK_IMB_RATIO = 1.3

    candles = get_candles(inst_id)
    if len(candles) < STRUCT_LOOKBACK + 3:
        return []

    last = candles[-1]
    trend = classify_trend(candles)

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades, thresholds)
    book = get_orderbook(inst_id)
    if not book:
        return []

    bid_n = book["bid_notional"]
    ask_n = book["ask_notional"]

    # Yapı: MSB + FVG
    bullish_msb, bull_level = detect_bullish_msb(candles)
    bearish_msb, bear_level = detect_bearish_msb(candles)
    fvg = find_recent_fvg(candles)

    bullish_fvg_reject = False
    bearish_fvg_reject = False
    if fvg:
        rej = check_fvg_rejection(candles, fvg)
        if rej and fvg["type"] == "bullish":
            bullish_fvg_reject = True
        if rej and fvg["type"] == "bearish":
            bearish_fvg_reject = True

    structure_long = bullish_msb or bullish_fvg_reject
    structure_short = bearish_msb or bearish_fvg_reject

    signals = []

    # ---------- LONG ---------
    if trend in ("UP", "RANGE") and structure_long:
        cond_struct = True
        cond_delta = of["net_delta"] >= NET_DELTA_MIN_POS
        cond_ob = bid_n > ask_n * ORDERBOOK_IMB_RATIO
        cond_whale = of["has_buy_whale"]

        conds = [cond_struct, cond_delta, cond_ob, cond_whale]
        true_count = sum(conds)

        if true_count >= MIN_CONDITIONS_LONG:
            confidence = int((true_count / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "LONG",
                "last_close": last["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence,
                "trend": trend,
                "segment": segment,
                "thresholds": thresholds,
                "structure": {
                    "bull_msb": bullish_msb,
                    "bull_level": bull_level,
                    "bull_fvg_reject": bullish_fvg_reject,
                },
            }
            signals.append(signal)

    # ---------- SHORT ---------
    # BTC/ETH/XRP gibi liderlerde short YOK
    if base in ("BTC", "ETH", "XRP"):
        return signals

    if trend == "DOWN" and btc_trend_global == "DOWN" and structure_short:
        cond_struct_s = True
        cond_delta_s = of["net_delta"] <= NET_DELTA_MIN_NEG
        cond_ob_s = ask_n > bid_n * ORDERBOOK_IMB_RATIO
        cond_whale_s = of["has_sell_whale"]

        conds_s = [cond_struct_s, cond_delta_s, cond_ob_s, cond_whale_s]
        true_count_s = sum(conds_s)

        if true_count_s >= MIN_CONDITIONS_SHORT:
            confidence_s = int((true_count_s / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "SHORT",
                "last_close": last["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence_s,
                "trend": trend,
                "segment": segment,
                "thresholds": thresholds,
                "structure": {
                    "bear_msb": bearish_msb,
                    "bear_level": bear_level,
                    "bear_fvg_reject": bearish_fvg_reject,
                },
            }
            signals.append(signal)

    return signals


# ------------ BTC & ETH Piyasa Özeti ------------

def get_trend_summary(inst_id, base_info):
    base = inst_id.split("-")[0]
    info = base_info.get(base, {})
    thresholds = info.get("thresholds", {"S": 80_000, "M": 150_000, "X": 300_000})
    segment = info.get("segment", "LOW")

    candles = get_candles(inst_id)
    if len(candles) < 60:
        return None

    closes = [c["close"] for c in candles]
    last = closes[-1]

    ema200 = ema(closes, 200) if len(closes) >= 200 else None
    ema50 = ema(closes, 50) if len(closes) >= 50 else None
    ema_fast = ema(closes, 12)
    ema_slow = ema(closes, 26)

    if ema_fast is not None and ema_slow is not None:
        macd = ema_fast - ema_slow
    else:
        macd = None

    if ema200 is None or ema50 is None:
        trend_txt = "Veri az"
        trend_code = "RANGE"
    else:
        if last > ema200 * 1.01 and ema50 > ema200:
            trend_txt = "Yukarı"
            trend_code = "UP"
        elif last < ema200 * 0.99 and ema50 < ema200:
            trend_txt = "Aşağı"
            trend_code = "DOWN"
        else:
            trend_txt = "Yatay"
            trend_code = "RANGE"

    if macd is None:
        mom_txt = "Bilinmiyor"
    else:
        if macd > 0:
            mom_txt = "Pozitif"
        elif macd < 0:
            mom_txt = "Negatif"
        else:
            mom_txt = "Düz"

    trades = get_trades(inst_id)
    of = analyze_trades_orderflow(trades, thresholds) if trades else None

    whale_txt = "Veri yok"
    delta_txt = "Veri yok"
    x_txt = ""
    segment_txt = ""

    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        w = of["buy_whale"]
        if w:
            whale_txt = f"{w['cls']} sınıfı BUY whale: ~${w['usd']:,.0f}"
        else:
            whale_txt = "S/M/X whale yok"

        if of["has_x_buy"] or of["has_x_sell"]:
            side = "BUY" if of["has_x_buy"] else "SELL"
            xw = of["x_buy_whale"] if of["has_x_buy"] else of["x_sell_whale"]
            x_txt = f"X whale ({side}): ~${xw['usd']:,.0f}"

        seg_word = "High-cap" if segment == "HIGH" else ("Mid-cap" if segment == "MID" else "Low-cap")
        S = thresholds["S"]
        M = thresholds["M"]
        X = thresholds["X"]
        segment_txt = f"{seg_word} → S≥{S:,.0f}, M≥{M:,.0f}, X≥{X:,.0f}"

    return {
        "inst_id": inst_id,
        "last": last,
        "trend": trend_txt,
        "trend_code": trend_code,
        "momentum": mom_txt,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
        "x_txt": x_txt,
        "segment_txt": segment_txt,
        "thresholds": thresholds,
        "segment": segment,
    }


# ------------ Telegram Mesajı ------------

def build_telegram_message(btc_info, eth_info, signals):
    lines = []
    lines.append(f"*📊 Piyasa Trendi (4H – OKX)*")

    if btc_info:
        lines.append(f"\n*BTC-USDT*")
        lines.append(f"- Fiyat: `{btc_info['last']:.2f}`")
        lines.append(f"- Trend: *{btc_info['trend']}*")
        lines.append(f"- Momentum: *{btc_info['momentum']}*")
        lines.append(f"- {btc_info['delta_txt']}")
        lines.append(f"- {btc_info['whale_txt']}")
        if btc_info["x_txt"]:
            lines.append(f"- {btc_info['x_txt']}")
        if btc_info["segment_txt"]:
            lines.append(f"- {btc_info['segment_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT*")
        lines.append(f"- Fiyat: `{eth_info['last']:.2f}`")
        lines.append(f"- Trend: *{eth_info['trend']}*")
        lines.append(f"- Momentum: *{eth_info['momentum']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")
        if eth_info["x_txt"]:
            lines.append(f"- {eth_info['x_txt']}")
        if eth_info["segment_txt"]:
            lines.append(f"- {eth_info['segment_txt']}")

    lines.append(f"\n*🚀 4H Giriş Sinyalleri (Top {TOP_LIMIT} USDT Spot)*")
    if not signals:
        lines.append("_Bu taramada sinyal yok._")
    else:
        x_whale_summary = []

        for s in signals:
            of = s["orderflow"]
            book = s["orderbook"]
            seg = s["segment"]
            thresholds = s["thresholds"]
            seg_word = "High-cap" if seg == "HIGH" else ("Mid-cap" if seg == "MID" else "Low-cap")
            S = thresholds["S"]
            M = thresholds["M"]
            X = thresholds["X"]

            struct_txt = []
            if s["side"] == "LONG":
                w = of["buy_whale"]
                if s["structure"].get("bull_msb"):
                    struct_txt.append("Bullish MSB")
                if s["structure"].get("bull_fvg_reject"):
                    struct_txt.append("Bullish FVG retest")
            else:
                w = of["sell_whale"]
                if s["structure"].get("bear_msb"):
                    struct_txt.append("Bearish MSB")
                if s["structure"].get("bear_fvg_reject"):
                    struct_txt.append("Bearish FVG retest")

            if w:
                whale_str = f"{w['cls']} whale ~${w['usd']:,.0f} @ {w['px']:.4f}"
            else:
                whale_str = "S/M/X whale yok"

            struct_str = ", ".join(struct_txt) if struct_txt else "Yapı: N/A"

            lines.append(f"\n*{s['inst_id']} ({s['side']})*")
            lines.append(f"- 4H Trend: `{s['trend']}` ({seg_word})")
            lines.append(f"- Kapanış: `{s['last_close']:.4f}`")
            lines.append(f"- Yapı: {struct_str}")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(f"- Whale: {whale_str}")
            lines.append(
                f"- Orderbook (Bid/Ask notional): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
            )
            lines.append(f"- Güven puanı: *%{s['confidence']}*")
            lines.append(
                f"- Whale eşikleri: S≥{S:,.0f}, M≥{M:,.0f}, X≥{X:,.0f}"
            )

            # X whale özeti
            if of["has_x_buy"] or of["has_x_sell"]:
                side = "BUY" if of["has_x_buy"] else "SELL"
                xw = of["x_buy_whale"] if of["has_x_buy"] else of["x_sell_whale"]
                lines.append(f"- 🐋 X whale ({side}): `${xw['usd']:,.0f}` @ {xw['px']:.4f}")
                x_whale_summary.append((s["inst_id"], s["side"], side, xw["usd"]))

        if x_whale_summary:
            lines.append("\n*🐋 X Whale Özeti*")
            for (inst, side, wside, usd) in x_whale_summary:
                lines.append(f"- {inst} ({side}) → {wside} X whale ~${usd:,.0f}")

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


# ------------ MAIN ------------

def main():
    print(f"[{ts()}] Bot çalışıyor...")

    # Top 150 USDT spot listesi
    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return

    bases = {s.split("-")[0] for s in symbols}
    bases.update(["BTC", "ETH"])  # trend özeti için

    # CoinGecko'dan marketcap'ler
    mcap_map = fetch_coingecko_mcaps(list(bases))

    # base_info: her base için (segment, thresholds)
    base_info = {}
    for b in bases:
        mcap = mcap_map.get(b)
        segment, thresholds = classify_segment_and_thresholds(mcap)
        base_info[b] = {
            "mcap": mcap,
            "segment": segment,
            "thresholds": thresholds,
        }

    # BTC & ETH piyasa özeti
    btc_info = get_trend_summary("BTC-USDT", base_info)
    eth_info = get_trend_summary("ETH-USDT", base_info)
    btc_trend_code = btc_info["trend_code"] if btc_info else "RANGE"

    print(f"{len(symbols)} sembol taranıyor...")

    all_signals = []
    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            sigs = analyze_symbol(inst_id, btc_trend_code, base_info)
            if sigs:
                for s in sigs:
                    print(
                        f"  → Sinyal bulundu: {inst_id} ({s['side']}) "
                        f"Trend:{s['trend']} Seg:{s['segment']} Güven %{s['confidence']}"
                    )
                all_signals.extend(sigs)
        except Exception as e:
            print(f"  {inst_id} analiz hatası:", e)
        time.sleep(0.15)  # çok hızlı istek atıp ban yememek için küçük bekleme

    if not all_signals:
        print("Bu turda sinyal yok. Telegram'a mesaj gönderilmeyecek.")
        return

    msg = build_telegram_message(btc_info, eth_info, all_signals)
    telegram(msg)
    print("✅ Telegram'a sinyal mesajı gönderildi.")


if __name__ == "__main__":
    main()
