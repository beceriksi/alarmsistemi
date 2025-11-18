import os
import time
import requests
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"
COINGECKO = "https://api.coingecko.com/api/v3"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Parametreler ---
TOP_LIMIT = 150               # En çok hacimli 150 spot USDT coini
CANDLE_LIMIT_4H = 200
CANDLE_LIMIT_1H = 120
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

# Yapı / filtreler
STRUCT_LOOKBACK = 30          # 4H MSB/FVG için bakılacak mum sayısı
BREAK_BUFFER = 0.0025         # ~%0.25 üzeri/altı kırılım
ZONE_BUFFER = 0.002           # %0.2 marj ile FVG retest
ORDERBOOK_IMB_RATIO = 1.3     # Bid/Ask notional dengesizliği oranı

# Trend sınırları
EMA_UP = 1.01                 # Fiyat > ema200 * 1.01 → up
EMA_DOWN = 0.99               # Fiyat < ema200 * 0.99 → down


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def jget_raw(url, params=None, retries=3, timeout=10):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.5)
    return None


def okx_get(path, params=None, retries=3, timeout=10):
    url = path if path.startswith("http") else OKX_BASE + path
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and j.get("code") == "0":
                    return j.get("data")
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


# ------------ CoinGecko: Marketcap Segmentleri ------------

def load_mcap_segments():
    """
    CoinGecko'dan marketcap'e göre segment haritası çıkarır.
    high / mid / low
    """
    segments = {}
    data = jget_raw(
        f"{COINGECKO}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        },
    ) or []

    for row in data:
        sym = str(row.get("symbol", "")).upper()
        mcap = row.get("market_cap") or 0
        if not sym:
            continue
        if mcap >= 10_000_000_000:
            seg = "high"
        elif mcap >= 1_000_000_000:
            seg = "mid"
        else:
            seg = "low"
        # aynı sembol birden fazla chain'de olabilir; en büyük mcap'i kullan
        if sym not in segments:
            segments[sym] = (mcap, seg)
        else:
            if mcap > segments[sym][0]:
                segments[sym] = (mcap, seg)

    # sadece segment string lazım
    return {k: v[1] for k, v in segments.items()}


def get_segment_for_symbol(inst_id, seg_map):
    base = inst_id.split("-")[0].upper()
    return seg_map.get(base, "mid")


def whale_thresholds(segment):
    """
    S / M / X sınıfları için notional sınırları.
    """
    if segment == "high":
        return {
            "S": 500_000,    # orta whale
            "M": 1_000_000,  # whale
            "X": 1_500_000,  # süper whale
        }
    elif segment == "mid":
        return {
            "S": 200_000,
            "M": 400_000,
            "X": 800_000,
        }
    else:  # low
        return {
            "S": 80_000,
            "M": 150_000,
            "X": 300_000,
        }


def delta_thresholds(segment):
    """
    Net delta için segment bazlı eşik.
    """
    if segment == "high":
        return 300_000, -300_000
    elif segment == "mid":
        return 150_000, -150_000
    else:
        return 50_000, -50_000


# ------------ OKX Yardımcıları ------------

def get_spot_usdt_top_symbols(limit=TOP_LIMIT):
    """
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk N'i alır.
    instId formatı: BTC-USDT, HBAR-USDT vs.
    """
    data = okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
    if not data:
        return []

    rows = []
    for d in data:
        inst_id = d.get("instId", "")
        if not inst_id.endswith("-USDT"):
            continue
        volCcy24h = d.get("volCcy24h")  # quote currency volume
        try:
            vol_quote = float(volCcy24h)
        except Exception:
            vol_quote = 0.0
        rows.append((inst_id, vol_quote))

    rows.sort(key=lambda x: x[1], reverse=True)
    symbols = [r[0] for r in rows[:limit]]
    return symbols


def get_candles(inst_id, bar, limit):
    data = okx_get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": limit})
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


def get_trades(inst_id, limit=TRADES_LIMIT):
    data = okx_get("/api/v5/market/trades", {"instId": inst_id, "limit": limit})
    return data or []


def get_orderbook(inst_id, depth=ORDERBOOK_DEPTH):
    data = okx_get("/api/v5/market/books", {"instId": inst_id, "sz": depth})
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


# ------------ Teknik Hesaplar / Yapı ------------

def ema_list(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def analyze_trades_orderflow(trades, segment):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - En büyük buy whale (S/M/X)
    - En büyük sell whale (S/M/X)
    """
    thr = whale_thresholds(segment)

    buy_notional = 0.0
    sell_notional = 0.0
    top_buy = None
    top_sell = None

    for t in trades:
        try:
            px = float(t.get("px"))
            sz = float(t.get("sz"))
            side = t.get("side", "").lower()
        except Exception:
            continue

        notional = px * abs(sz)
        if side == "buy":
            buy_notional += notional
            cls = None
            if notional >= thr["X"]:
                cls = "X"
            elif notional >= thr["M"]:
                cls = "M"
            elif notional >= thr["S"]:
                cls = "S"
            if cls:
                if (top_buy is None) or (notional > top_buy["usd"]):
                    top_buy = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "class": cls,
                        "side": "buy",
                        "ts": t.get("ts"),
                    }
        elif side == "sell":
            sell_notional += notional
            cls = None
            if notional >= thr["X"]:
                cls = "X"
            elif notional >= thr["M"]:
                cls = "M"
            elif notional >= thr["S"]:
                cls = "S"
            if cls:
                if (top_sell is None) or (notional > top_sell["usd"]):
                    top_sell = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "class": cls,
                        "side": "sell",
                        "ts": t.get("ts"),
                    }

    net_delta = buy_notional - sell_notional
    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_delta": net_delta,
        "buy_whale": top_buy,
        "sell_whale": top_sell,
        "has_buy_whale": top_buy is not None,
        "has_sell_whale": top_sell is not None,
        "segment": segment,
    }


# ---- Market Structure Break (4H) ----

def detect_bullish_msb(candles, lookback=STRUCT_LOOKBACK):
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = max(closes)
    last_close = candles[-1]["close"]

    if last_close > level * (1 + BREAK_BUFFER):
        return True, level
    return False, level


def detect_bearish_msb(candles, lookback=STRUCT_LOOKBACK):
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = min(closes)
    last_close = candles[-1]["close"]

    if last_close < level * (1 - BREAK_BUFFER):
        return True, level
    return False, level


# ---- Basit FVG (4H) ----

def find_recent_fvg(candles, lookback=STRUCT_LOOKBACK):
    """
    Basit FVG:
      Bullish: high(i-2) < low(i)
      Bearish: low(i-2) > high(i)
    """
    n = len(candles)
    if n < 3:
        return None

    start = max(2, n - lookback)
    last_fvg = None

    for i in range(start, n):
        c1 = candles[i - 2]
        c3 = candles[i]

        if c1["high"] < c3["low"]:
            last_fvg = {
                "type": "bullish",
                "low": c1["high"],
                "high": c3["low"],
            }

        if c1["low"] > c3["high"]:
            last_fvg = {
                "type": "bearish",
                "low": c3["high"],
                "high": c1["low"],
            }

    return last_fvg


def check_fvg_rejection(candles, fvg):
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


# -------- 1H Onay (trend + son durum) --------

def confirm_long_1h(candles_1h):
    if len(candles_1h) < 60:
        return False
    closes = [c["close"] for c in candles_1h]
    ema20 = ema_list(closes, 20)
    ema50 = ema_list(closes, 50)
    last = closes[-1]

    if ema20 is None or ema50 is None:
        return False

    # Trend yukarı + fiyat EMA50 üstünde
    if ema20 > ema50 and last > ema50:
        # Son 3 mumda % -2'den fazla ters hareket olmaması
        if len(closes) >= 4:
            ref = closes[-4]
            change = (last / ref) - 1.0
            if change < -0.02:
                return False
        return True
    return False


def confirm_short_1h(candles_1h):
    if len(candles_1h) < 60:
        return False
    closes = [c["close"] for c in candles_1h]
    ema20 = ema_list(closes, 20)
    ema50 = ema_list(closes, 50)
    last = closes[-1]

    if ema20 is None or ema50 is None:
        return False

    # Trend aşağı + fiyat EMA50 altında
    if ema20 < ema50 and last < ema50:
        if len(closes) >= 4:
            ref = closes[-4]
            change = (last / ref) - 1.0
            if change > 0.02:
                return False
        return True
    return False


# ------------ BTC & ETH Trend Özeti ------------

def get_trend_summary(inst_id, segment="high"):
    candles = get_candles(inst_id, bar="4H", limit=CANDLE_LIMIT_4H)
    if len(candles) < 50:
        return None

    closes = [c["close"] for c in candles]
    last = closes[-1]

    ema50 = ema_list(closes, 50)
    ema200 = ema_list(closes, 200)

    if ema200 is None:
        trend_txt = "Veri az"
        trend_dir = "range"
    else:
        if last > ema200 * EMA_UP:
            trend_txt = "Yukarı"
            trend_dir = "up"
        elif last < ema200 * EMA_DOWN:
            trend_txt = "Aşağı"
            trend_dir = "down"
        else:
            trend_txt = "Yatay"
            trend_dir = "range"

    # Basit momentum: ema50 vs ema200
    if ema50 is None or ema200 is None:
        mom_txt = "Bilinmiyor"
    else:
        if ema50 > ema200:
            mom_txt = "Pozitif"
        elif ema50 < ema200:
            mom_txt = "Negatif"
        else:
            mom_txt = "Düz"

    trades = get_trades(inst_id)
    of = analyze_trades_orderflow(trades, segment) if trades else None

    whale_txt = "Veri yok"
    delta_txt = "Veri yok"

    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        w = of["buy_whale"]
        if w:
            whale_txt = f"Whale BUY: {w['class']} ~${w['usd']:,.0f}"
        else:
            whale_txt = "Whale BUY yok"

    return {
        "inst_id": inst_id,
        "last": last,
        "trend": trend_txt,
        "trend_dir": trend_dir,
        "momentum": mom_txt,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
    }


# ------------ Sembol Analizi (4H + 1H) ------------

def analyze_symbol(inst_id, segment, btc_trend_dir):
    """
    Tek coin için:
    - 4H MSB + FVG yapısı
    - 1H trend onayı
    - Orderflow + whale + orderbook filtreleri
    Hem LONG hem SHORT sinyalleri döndürür.
    """
    candles_4h = get_candles(inst_id, bar="4H", limit=CANDLE_LIMIT_4H)
    if len(candles_4h) < STRUCT_LOOKBACK + 5:
        return []

    last_4h = candles_4h[-1]
    closes_4h = [c["close"] for c in candles_4h]
    ema50_4h = ema_list(closes_4h, 50)
    ema200_4h = ema_list(closes_4h, 200)

    pair_trend = "range"
    if ema200_4h is not None:
        if last_4h["close"] > ema200_4h * EMA_UP:
            pair_trend = "up"
        elif last_4h["close"] < ema200_4h * EMA_DOWN:
            pair_trend = "down"

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades, segment)
    book = get_orderbook(inst_id)
    if not book:
        return []

    bid_n = book["bid_notional"]
    ask_n = book["ask_notional"]

    fvg = find_recent_fvg(candles_4h)
    bullish_msb, bull_level = detect_bullish_msb(candles_4h)
    bearish_msb, bear_level = detect_bearish_msb(candles_4h)

    bullish_fvg_reject = False
    bearish_fvg_reject = False
    if fvg:
        rej = check_fvg_rejection(candles_4h, fvg)
        if rej and fvg["type"] == "bullish":
            bullish_fvg_reject = True
        if rej and fvg["type"] == "bearish":
            bearish_fvg_reject = True

    structure_long_4h = bullish_msb or bullish_fvg_reject
    structure_short_4h = bearish_msb or bearish_fvg_reject

    # 1H onay
    candles_1h = get_candles(inst_id, bar="1H", limit=CANDLE_LIMIT_1H)
    if not candles_1h:
        return []

    confirm_long = confirm_long_1h(candles_1h)
    confirm_short = confirm_short_1h(candles_1h)

    structure_long = structure_long_4h and confirm_long
    structure_short = structure_short_4h and confirm_short

    delta_pos_thr, delta_neg_thr = delta_thresholds(segment)

    signals = []

    # ---------- LONG ---------
    if structure_long:
        # Global trend filtresi: BTC DOWN iken agresif LONG istemiyoruz
        if btc_trend_dir != "down" and pair_trend != "down":
            cond_struct = True
            cond_delta = of["net_delta"] >= delta_pos_thr
            cond_ob = bid_n > ask_n * ORDERBOOK_IMB_RATIO
            cond_whale = of["has_buy_whale"]

            conds = [cond_struct, cond_delta, cond_ob, cond_whale]
            true_count = sum(conds)

            if true_count >= 3:
                confidence = int((true_count / 4) * 100)
                signal = {
                    "inst_id": inst_id,
                    "side": "LONG",
                    "last_close": last_4h["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "confidence": confidence,
                    "structure": {
                        "bull_msb": bullish_msb,
                        "bull_level": bull_level,
                        "bull_fvg_reject": bullish_fvg_reject,
                    },
                    "segment": segment,
                    "pair_trend": pair_trend,
                }
                signals.append(signal)

    # ---------- SHORT ---------
    if structure_short:
        # Global trend filtresi: SHORT sadece BTC trend "down" iken
        if btc_trend_dir == "down" and pair_trend == "down":
            cond_struct_s = True
            cond_delta_s = of["net_delta"] <= delta_neg_thr
            cond_ob_s = ask_n > bid_n * ORDERBOOK_IMB_RATIO
            cond_whale_s = of["has_sell_whale"]

            conds_s = [cond_struct_s, cond_delta_s, cond_ob_s, cond_whale_s]
            true_count_s = sum(conds_s)

            if true_count_s >= 3:
                confidence_s = int((true_count_s / 4) * 100)
                signal = {
                    "inst_id": inst_id,
                    "side": "SHORT",
                    "last_close": last_4h["close"],
                    "orderflow": of,
                    "orderbook": book,
                    "confidence": confidence_s,
                    "structure": {
                        "bear_msb": bearish_msb,
                        "bear_level": bear_level,
                        "bear_fvg_reject": bearish_fvg_reject,
                    },
                    "segment": segment,
                    "pair_trend": pair_trend,
                }
                signals.append(signal)

    return signals


# ------------ Telegram Mesajı ------------

def whale_explanation(of, side, segment):
    thresholds = whale_thresholds(segment)
    label_map = {
        "S": "S (orta whale)",
        "M": "M (büyük whale)",
        "X": "X (süper whale)",
    }
    if side == "LONG":
        w = of["buy_whale"]
    else:
        w = of["sell_whale"]

    if not w:
        return "Whale yok"

    cls = w.get("class")
    cls_label = label_map.get(cls, cls)
    base_txt = f"{cls_label} ~${w['usd']:,.0f} @ {w['px']:.4f}"

    if segment == "high":
        seg_txt = "High-cap coin için bu hacim güçlü ama tek başına trendi çevirmek zorunda değil."
    elif segment == "mid":
        seg_txt = "Mid-cap coinlerde bu büyüklük, yön değiştirebilecek seviyede ciddi bir işlem."
    else:
        seg_txt = "Low-cap coinlerde bu hacim grafiği tek başına bile sert oynatabilir."

    return base_txt + " — " + seg_txt


def build_telegram_message(btc_info, eth_info, signals):
    lines = []
    lines.append(f"*📊 Piyasa Trendi (4H + 1H Onay – OKX)*")

    if btc_info:
        lines.append(f"\n*BTC-USDT*")
        lines.append(f"- Fiyat: `{btc_info['last']:.2f}`")
        lines.append(f"- Trend: *{btc_info['trend']}*")
        lines.append(f"- Momentum: *{btc_info['momentum']}*")
        lines.append(f"- {btc_info['delta_txt']}")
        lines.append(f"- {btc_info['whale_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT*")
        lines.append(f"- Fiyat: `{eth_info['last']:.2f}`")
        lines.append(f"- Trend: *{eth_info['trend']}*")
        lines.append(f"- Momentum: *{eth_info['momentum']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")

    lines.append(f"\n*🚀 4H Sinyaller (Top {TOP_LIMIT} USDT Spot, 1H Onaylı)*")
    if not signals:
        lines.append("_Bu taramada sinyal yok._")
    else:
        for s in signals:
            of = s["orderflow"]
            book = s["orderbook"]
            seg = s["segment"]

            seg_label = {"high": "High-cap", "mid": "Mid-cap", "low": "Low-cap"}.get(seg, seg)
            whale_str = whale_explanation(of, s["side"], seg)

            struct_txt = []
            if s["side"] == "LONG":
                if s["structure"].get("bull_msb"):
                    struct_txt.append("Bullish MSB")
                if s["structure"].get("bull_fvg_reject"):
                    struct_txt.append("Bullish FVG retest")
            else:
                if s["structure"].get("bear_msb"):
                    struct_txt.append("Bearish MSB")
                if s["structure"].get("bear_fvg_reject"):
                    struct_txt.append("Bearish FVG retest")

            struct_str = ", ".join(struct_txt) if struct_txt else "Yapı: N/A"

            lines.append(f"\n*{s['inst_id']} ({s['side']})*")
            lines.append(f"- Segment: `{seg_label}`")
            lines.append(f"- 4H kapanış: `{s['last_close']:.4f}`")
            lines.append(f"- 4H Yapı: {struct_str}")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(f"- Whale: {whale_str}")
            lines.append(
                f"- Orderbook (Bid/Ask notional): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
            )
            lines.append(f"- Çift trendi (4H): `{s['pair_trend']}`")
            lines.append(f"- Güven puanı: *%{s['confidence']}*")

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


# ------------ MAIN ------------

def main():
    print(f"[{ts()}] Bot çalışıyor...")

    seg_map = load_mcap_segments()

    btc_info = get_trend_summary("BTC-USDT", segment="high")
    eth_info = get_trend_summary("ETH-USDT", segment="high")

    btc_trend_dir = btc_info["trend_dir"] if btc_info else "range"

    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return

    print(f"{len(symbols)} sembol taranıyor...")

    all_signals = []
    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            segment = get_segment_for_symbol(inst_id, seg_map)
            sigs = analyze_symbol(inst_id, segment, btc_trend_dir)
            if sigs:
                for s in sigs:
                    print(
                        f"  → Sinyal bulundu: {inst_id} ({s['side']})  Segment:{segment}  Güven %{s['confidence']}"
                    )
                all_signals.extend(sigs)
        except Exception as e:
            print(f"  {inst_id} analiz hatası:", e)
        time.sleep(0.2)  # çok hızlı istek atıp ban yememek için küçük bekleme

    if not all_signals:
        print("Bu turda sinyal yok. Telegram'a mesaj gönderilmeyecek.")
        return

    msg = build_telegram_message(btc_info, eth_info, all_signals)
    telegram(msg)
    print("✅ Telegram'a sinyal mesajı gönderildi.")


if __name__ == "__main__":
    main()
