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
CANDLE_LIMIT = 200
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

# Fiyat yapısı
STRUCT_LOOKBACK = 20          # MSB ve FVG için bakılacak mum sayısı
ZONE_BUFFER = 0.002           # %0.2 marj ile bölge (FVG/MSB değerlendirmesinde)

# Strateji modu: 4 koşuldan en az 3'ü
MIN_CONDITIONS_STRICT = 3

# --- Ek güvenlik parametreleri (senin zarardan bıktığın kısımlar için) ---
MAX_STRUCTURE_DISTANCE = 0.01     # MSB/FVG seviyesinden max %1 uzaklık
MAX_WHALE_DISTANCE = 0.008        # Whale fiyatından max %0.8 uzaklık
MAX_WHALE_AGE_MIN = 240           # Whale işlemi max 240 dakika (4H) eski olabilir


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ------------ HTTP Yardımcıları ------------

def jget_okx(path, params=None, retries=3, timeout=10):
    url = f"{OKX_BASE}{path}"
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "0" and j.get("data"):
                    return j["data"]
        except Exception:
            time.sleep(0.5)
    return None


def jget_json(url, params=None, retries=3, timeout=10):
    """Genel amaçlı JSON GET (CoinGecko vs)"""
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


# ------------ CoinGecko MCAP Haritası ------------

def load_mcap_map(max_pages: int = 2):
    """
    CoinGecko /coins/markets → symbol -> market_cap map
    En yüksek mcap'i olan symbol kazanır (aynı sembolü kullananlar için).
    """
    mcap_map = {}
    for page in range(1, max_pages + 1):
        data = jget_json(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
            },
        )
        if not data:
            break
        for row in data:
            sym = str(row.get("symbol", "")).upper()
            mc = row.get("market_cap") or 0
            if not sym or not mc:
                continue
            if sym not in mcap_map or mc > mcap_map[sym]:
                mcap_map[sym] = mc
    return mcap_map


def classify_mcap(base: str, mcap_map: dict):
    """
    HIGH / MID / LOW / MICRO sınıflandırması
    """
    mc = mcap_map.get(base.upper())
    if mc is None:
        return "UNKNOWN"
    if mc >= 10_000_000_000:
        return "HIGH"
    if mc >= 1_000_000_000:
        return "MID"
    if mc >= 100_000_000:
        return "LOW"
    return "MICRO"


def whale_thresholds(mcap_class: str):
    """
    MCAP sınıfına göre S/M/X whale eşikleri
    S: orta, M: büyük, X: süper whale
    """
    if mcap_class == "HIGH":
        # BTC, ETH, BNB, SOL, XRP...
        return 500_000, 1_000_000, 1_500_000
    elif mcap_class == "MID":
        # AVAX, LINK, TON, ARB, SUI, HBAR...
        return 200_000, 400_000, 800_000
    elif mcap_class == "LOW":
        # 100M–1B arası
        return 100_000, 200_000, 400_000
    else:
        # MICRO / UNKNOWN → biraz daha düşük
        return 80_000, 150_000, 300_000


def net_delta_thresholds(mcap_class: str):
    """
    Net delta eşikleri (MCAP'e göre ölçekli)
    """
    if mcap_class == "HIGH":
        return 200_000, -200_000
    elif mcap_class == "MID":
        return 100_000, -100_000
    elif mcap_class == "LOW":
        return 50_000, -50_000
    else:
        return 30_000, -30_000


def mcap_nice_label(mcap_class: str):
    if mcap_class == "HIGH":
        return "🟦 High-cap"
    if mcap_class == "MID":
        return "🟧 Mid-cap"
    if mcap_class == "LOW":
        return "🟨 Low-cap"
    if mcap_class == "MICRO":
        return "🟥 Micro-cap"
    return "⬜ Unknown-cap"


# ------------ OKX Yardımcıları ------------

def get_spot_usdt_top_symbols(limit=TOP_LIMIT):
    """
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk 150'yi alır.
    instId formatı: BTC-USDT, HBAR-USDT vs.
    """
    data = jget_okx("/api/v5/market/tickers", {"instType": "SPOT"})
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


def get_candles(inst_id, bar=BAR, limit=CANDLE_LIMIT):
    data = jget_okx("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": limit})
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
    data = jget_okx("/api/v5/market/trades", {"instId": inst_id, "limit": limit})
    return data or []


def get_orderbook(inst_id, depth=ORDERBOOK_DEPTH):
    data = jget_okx("/api/v5/market/books", {"instId": inst_id, "sz": depth})
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

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def analyze_trades_orderflow(trades, medium_thr, whale_thr, super_thr):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - S / M / X seviyesinde en büyük buy whale
    - S / M / X seviyesinde en büyük sell whale
    """
    buy_notional = 0.0
    sell_notional = 0.0
    best_buy = None
    best_sell = None

    for t in trades:
        try:
            px = float(t.get("px"))
            sz = float(t.get("sz"))
            side = t.get("side", "").lower()
        except Exception:
            continue

        notional = px * abs(sz)

        # Whale tier belirle
        tier = None
        if notional >= super_thr:
            tier = "X"
        elif notional >= whale_thr:
            tier = "M"
        elif notional >= medium_thr:
            tier = "S"

        if side == "buy":
            buy_notional += notional
            if tier:
                if (best_buy is None) or (notional > best_buy["usd"]):
                    best_buy = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "tier": tier,
                        "ts": t.get("ts"),
                    }
        elif side == "sell":
            sell_notional += notional
            if tier:
                if (best_sell is None) or (notional > best_sell["usd"]):
                    best_sell = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "tier": tier,
                        "ts": t.get("ts"),
                    }

    net_delta = buy_notional - sell_notional

    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_delta": net_delta,
        "buy_whale": best_buy,
        "sell_whale": best_sell,
        "has_buy_whale": best_buy is not None,
        "has_sell_whale": best_sell is not None,
    }


def tier_nice_label(tier: str):
    if tier == "S":
        return "S (Orta whale)"
    if tier == "M":
        return "M (Büyük whale)"
    if tier == "X":
        return "X (Süper whale)"
    return "-"


# ---- Market Structure Break (MSB) ----

def detect_bullish_msb(candles, lookback=STRUCT_LOOKBACK):
    """
    Basit bullish MSB:
    - Son lookback içindeki en yüksek kapanış alınır
    - Son mum bu seviyenin %0.1 üstünde kapanmışsa bullish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = max(closes)
    last_close = candles[-1]["close"]

    if last_close > level * 1.001:
        return True, level
    return False, level


def detect_bearish_msb(candles, lookback=STRUCT_LOOKBACK):
    """
    Basit bearish MSB:
    - Son lookback içindeki en düşük kapanış alınır
    - Son mum bu seviyenin %0.1 altına kırdıysa bearish break
    """
    if len(candles) < lookback + 2:
        return False, None

    closes = [c["close"] for c in candles[-(lookback + 1):-1]]
    level = min(closes)
    last_close = candles[-1]["close"]

    if last_close < level * 0.999:
        return True, level
    return False, level


# ---- FVG (Fair Value Gap) Tespiti ----

def find_recent_fvg(candles, lookback=STRUCT_LOOKBACK):
    """
    Basitleştirilmiş FVG:
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
    Son mum için FVG rejection kontrolü:
    - Bullish: son mum fitili FVG içine girip, FVG üstünden kapanmışsa (veya en azından içinde yukarı yönlü)
    - Bearish: son mum fitili FVG içine girip, FVG altından kapanmışsa (veya içinde aşağı yönlü)
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

    # mum FVG bölgesine değmiş mi?
    touched = not (high < z_low or low > z_high)
    if not touched:
        return False

    # bullish FVG rejection
    if fvg["type"] == "bullish":
        if close > op and close > (z_low * (1 + ZONE_BUFFER / 2)):
            return True
        return False

    # bearish FVG rejection
    if fvg["type"] == "bearish":
        if close < op and close < (z_high * (1 - ZONE_BUFFER / 2)):
            return True
        return False

    return False


# ------------ Yardımcı: Whale yaşı (dakika) ------------

def whale_age_minutes(whale, last_candle_ts_ms):
    """
    Whale işleminin son mum zamanına göre kaç dakika önce olduğunu döndürür.
    """
    if not whale:
        return None
    ts_val = whale.get("ts")
    if not ts_val:
        return None
    try:
        w_ts = int(ts_val)
        diff_ms = last_candle_ts_ms - w_ts
        return diff_ms / 1000 / 60
    except Exception:
        return None


# ------------ Sembol Analizi (LONG + SHORT) ------------

def analyze_symbol(inst_id, mcap_map):
    """
    Tek coin için:
    - MCAP sınıfı → HIGH/MID/LOW/MICRO
    - FVG + MSB yapısı
    - Orderflow + S/M/X whale + orderbook + net delta ile filtre
    Hem LONG hem SHORT sinyalleri döndürür.

    Bu versiyonda:
    - MSB/FVG seviyesinden çok uzaklaşmış sinyaller elenir
    - Whale fiyatından çok uzaklaşmış sinyaller elenir
    - Whale işlemi çok eskiyse (4H+) sinyal elenir
    Böylece tepeden/dipten geç gelen sinyaller büyük oranda süzülür.
    """
    candles = get_candles(inst_id)
    if len(candles) < STRUCT_LOOKBACK + 3:
        return []

    last = candles[-1]
    last_close = last["close"]
    last_ts = last["ts"]

    base = inst_id.split("-")[0]
    mcap_class = classify_mcap(base, mcap_map)
    medium_thr, whale_thr, super_thr = whale_thresholds(mcap_class)
    nd_pos_thr, nd_neg_thr = net_delta_thresholds(mcap_class)

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades, medium_thr, whale_thr, super_thr)
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

    # --- EK GÜVENLİK 1: Yapı (MSB / FVG) seviyesinden çok uzaksa LONG/SHORT iptal ---

    if structure_long:
        ref_level = None
        # Önce MSB seviyesi
        if bullish_msb and bull_level:
            ref_level = bull_level
        # MSB yoksa, FVG seviyesinin ortasını referans al
        elif bullish_fvg_reject and fvg and fvg["type"] == "bullish":
            ref_level = (fvg["low"] + fvg["high"]) / 2.0

        if ref_level:
            dist = abs(last_close - ref_level) / ref_level
            if dist > MAX_STRUCTURE_DISTANCE:
                # Fiyat yapıya göre çok yürümüş → geç sinyal → iptal
                structure_long = False

    if structure_short:
        ref_level_s = None
        if bearish_msb and bear_level:
            ref_level_s = bear_level
        elif bearish_fvg_reject and fvg and fvg["type"] == "bearish":
            ref_level_s = (fvg["low"] + fvg["high"]) / 2.0

        if ref_level_s:
            dist_s = abs(last_close - ref_level_s) / ref_level_s
            if dist_s > MAX_STRUCTURE_DISTANCE:
                # Fiyat yapıya göre çok uzak → tepeden/dipten işlem açma
                structure_short = False

    signals = []

    # ---------- LONG ---------
    if structure_long:
        cond_struct = True

        # Net delta şartı
        cond_delta = of["net_delta"] >= nd_pos_thr

        # Orderbook baskısı şartı
        cond_ob = bid_n > ask_n * 1.3

        # Whale şartı + EK GÜVENLİK (fiyat yakınlığı + tazelik)
        w_buy = of["buy_whale"]
        cond_whale = False
        if w_buy:
            # Whale fiyatına uzaklık
            try:
                whale_px = float(w_buy["px"])
                whale_dist = abs(last_close - whale_px) / whale_px
            except Exception:
                whale_dist = None

            # Whale yaşı (dakika)
            age_min = whale_age_minutes(w_buy, last_ts)

            near_enough = (whale_dist is not None and whale_dist <= MAX_WHALE_DISTANCE)
            fresh_enough = (age_min is None) or (age_min <= MAX_WHALE_AGE_MIN)

            if near_enough and fresh_enough:
                cond_whale = True

        conds = [cond_struct, cond_delta, cond_ob, cond_whale]
        true_count = sum(conds)

        if true_count >= MIN_CONDITIONS_STRICT:
            confidence = int((true_count / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "LONG",
                "last_close": last_close,
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence,
                "structure": {
                    "bull_msb": bullish_msb,
                    "bull_level": bull_level,
                    "bull_fvg_reject": bullish_fvg_reject,
                    "mcap_class": mcap_class,
                },
            }
            signals.append(signal)

    # ---------- SHORT ---------
    if structure_short:
        cond_struct_s = True

        # Net delta şartı
        cond_delta_s = of["net_delta"] <= nd_neg_thr

        # Orderbook baskısı şartı
        cond_ob_s = ask_n > bid_n * 1.3

        # Whale şartı + EK GÜVENLİK (fiyat yakınlığı + tazelik)
        w_sell = of["sell_whale"]
        cond_whale_s = False
        if w_sell:
            try:
                whale_px_s = float(w_sell["px"])
                whale_dist_s = abs(last_close - whale_px_s) / whale_px_s
            except Exception:
                whale_dist_s = None

            age_min_s = whale_age_minutes(w_sell, last_ts)

            near_enough_s = (whale_dist_s is not None and whale_dist_s <= MAX_WHALE_DISTANCE)
            fresh_enough_s = (age_min_s is None) or (age_min_s <= MAX_WHALE_AGE_MIN)

            if near_enough_s and fresh_enough_s:
                cond_whale_s = True

        conds_s = [cond_struct_s, cond_delta_s, cond_ob_s, cond_whale_s]
        true_count_s = sum(conds_s)

        if true_count_s >= MIN_CONDITIONS_STRICT:
            confidence_s = int((true_count_s / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "SHORT",
                "last_close": last_close,
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence_s,
                "structure": {
                    "bear_msb": bearish_msb,
                    "bear_level": bear_level,
                    "bear_fvg_reject": bearish_fvg_reject,
                    "mcap_class": mcap_class,
                },
            }
            signals.append(signal)

    return signals


# ------------ BTC & ETH Piyasa Özeti ------------

def get_trend_summary(inst_id, mcap_map):
    candles = get_candles(inst_id)
    if len(candles) < 50:
        return None

    closes = [c["close"] for c in candles]
    last = closes[-1]

    ema200 = ema(closes, 200) if len(closes) >= 200 else None
    ema_fast = ema(closes, 12)
    ema_slow = ema(closes, 26)

    macd = None
    if ema_fast is not None and ema_slow is not None:
        macd = ema_fast - ema_slow

    base = inst_id.split("-")[0]
    mcap_class = classify_mcap(base, mcap_map)
    medium_thr, whale_thr, super_thr = whale_thresholds(mcap_class)

    trades = get_trades(inst_id)
    of = analyze_trades_orderflow(trades, medium_thr, whale_thr, super_thr) if trades else None

    # Trend yorumu
    if ema200 is None:
        trend_txt = "Veri az"
    else:
        if last > ema200 * 1.01:
            trend_txt = "Yukarı"
        elif last < ema200 * 0.99:
            trend_txt = "Aşağı"
        else:
            trend_txt = "Yatay"

    # Momentum yorumu
    if macd is None:
        mom_txt = "Bilinmiyor"
    else:
        if macd > 0:
            mom_txt = "Pozitif"
        elif macd < 0:
            mom_txt = "Negatif"
        else:
            mom_txt = "Düz"

    whale_txt = "Veri yok"
    delta_txt = "Veri yok"

    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        w = of["buy_whale"]
        if w:
            whale_txt = f"Whale: {tier_nice_label(w['tier'])} ~${w['usd']:,.0f}"
        else:
            whale_txt = "Anlamlı BUY whale yok"

    return {
        "inst_id": inst_id,
        "last": last,
        "trend": trend_txt,
        "momentum": mom_txt,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
        "mcap_class": mcap_class,
    }


# ------------ Telegram Mesajı ------------

def build_telegram_message(btc_info, eth_info, signals):
    lines = []
    lines.append(f"*📊 Piyasa Trendi (4H – OKX)*")

    if btc_info:
        lines.append(f"\n*BTC-USDT* {mcap_nice_label(btc_info['mcap_class'])}")
        lines.append(f"- Fiyat: `{btc_info['last']:.2f}`")
        lines.append(f"- Trend: *{btc_info['trend']}*")
        lines.append(f"- Momentum: *{btc_info['momentum']}*")
        lines.append(f"- {btc_info['delta_txt']}")
        lines.append(f"- {btc_info['whale_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT* {mcap_nice_label(eth_info['mcap_class'])}")
        lines.append(f"- Fiyat: `{eth_info['last']:.2f}`")
        lines.append(f"- Trend: *{eth_info['trend']}*")
        lines.append(f"- Momentum: *{eth_info['momentum']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")

    lines.append(f"\n*🚀 4H Giriş Sinyalleri (Top {TOP_LIMIT} USDT Spot)*")

    if not signals:
        lines.append("_Bu taramada sinyal yok._")
        lines.append(
            "\nWhale kodları: `S=orta`, `M=büyük`, `X=süper` (coin MCAP'ine göre hesaplanır)"
        )
        lines.append(f"\n_Zaman:_ `{ts()}`")
        return "\n".join(lines)

    big_whale_seen = False

    for s in signals:
        of = s["orderflow"]
        book = s["orderbook"]
        mcap_class = s["structure"].get("mcap_class", "UNKNOWN")

        if s["side"] == "LONG":
            w = of["buy_whale"]
            struct_txt = []
            if s["structure"].get("bull_msb"):
                struct_txt.append("Bullish MSB")
            if s["structure"].get("bull_fvg_reject"):
                struct_txt.append("Bullish FVG retest")
        else:
            w = of["sell_whale"]
            struct_txt = []
            if s["structure"].get("bear_msb"):
                struct_txt.append("Bearish MSB")
            if s["structure"].get("bear_fvg_reject"):
                struct_txt.append("Bearish FVG retest")

        struct_str = ", ".join(struct_txt) if struct_txt else "Yapı: N/A"

        lines.append(f"\n*{s['inst_id']} ({s['side']})* {mcap_nice_label(mcap_class)}")
        lines.append(f"- Kapanış: `{s['last_close']:.4f}`")
        lines.append(f"- Yapı: {struct_str}")
        lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
        lines.append(
            f"- Orderbook (Bid/Ask notional): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
        )
        lines.append(f"- Güven puanı: *%{s['confidence']}*")

        if w:
            lines.append(
                f"- Whale: {tier_nice_label(w['tier'])} ~`${w['usd']:,.0f}` @ {w['px']:.4f}"
            )
            big_whale_seen = True
        else:
            lines.append(f"- Whale: Yok (bu coinde anlamlı S/M/X trade yok)")

    if big_whale_seen:
        lines.append(
            "\nWhale kodları: `S=orta`, `M=büyük`, `X=süper` — seviyeler coin'in piyasa değerine göre hesaplanır."
        )
    else:
        lines.append(
            "\nWhale yoksa bile yapı + delta + orderbook birlikte sinyal üretiyor. Kademeli giriş düşünülmeli."
        )

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


# ------------ MAIN ------------

def main():
    print(f"[{ts()}] Bot çalışıyor...")

    # MCAP haritası (CoinGecko)
    print("CoinGecko market cap verisi çekiliyor...")
    mcap_map = load_mcap_map()
    print(f"MCAP haritası yüklendi. Sembol sayısı: {len(mcap_map)}")

    # BTC & ETH piyasa özeti
    btc_info = get_trend_summary("BTC-USDT", mcap_map)
    eth_info = get_trend_summary("ETH-USDT", mcap_map)

    # Top 150 USDT spot listesi (OKX hacme göre)
    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return

    print(f"{len(symbols)} sembol taranıyor...")

    all_signals = []
    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            sigs = analyze_symbol(inst_id, mcap_map)
            if sigs:
                for s in sigs:
                    print(
                        f"  → Sinyal bulundu: {inst_id} ({s['side']})  Güven %{s['confidence']}"
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
