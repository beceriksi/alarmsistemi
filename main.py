import os
import time
import requests
from datetime import datetime, timezone

OKX_BASE = "https://www.okx.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Parametreler ---
TOP_LIMIT = 150               # En çok hacimli 150 spot USDT coini
BAR = "4H"                    # 4 saatlik sistem
CANDLE_LIMIT = 200
TRADES_LIMIT = 200
ORDERBOOK_DEPTH = 20

# Whale eşikleri
WHALE_USDT_MIN = 100_000      # Normal whale (onay için)
BIG_WHALE_USDT = 500_000      # Ekstra uyarı vereceğimiz büyük whale eşiği

# Delta ve orderbook eşikleri
NET_DELTA_MIN_POS = 50_000    # Long için min net alış delta (USDT)
NET_DELTA_MIN_NEG = -50_000   # Short için min net satış delta (USDT)
ORDERBOOK_IMB_RATIO = 1.3     # Bid/Ask notional dengesizliği oranı

# Fiyat yapısı
STRUCT_LOOKBACK = 20          # MSB ve FVG için bakılacak mum sayısı
ZONE_BUFFER = 0.002           # %0.2 marj ile bölge (FVG/MSB değerlendirmesinde)

# Strateji modu: C (4 koşuldan en az 3'ü)
MIN_CONDITIONS_STRICT = 3


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def jget(url, params=None, retries=3, timeout=10):
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
    OKX SPOT tickers → USDT pariteleri içinden en yüksek 24h notional hacme göre ilk 150'yi alır.
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


def get_trades(inst_id, limit=TRADES_LIMIT):
    url = f"{OKX_BASE}/api/v5/market/trades"
    params = {"instId": inst_id, "limit": limit}
    data = jget(url, params=params)
    return data or []


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


# ------------ Teknik Hesaplar / Yapı ------------

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def analyze_trades_orderflow(trades):
    """
    Spot için:
    - Net notional delta (buy_notional - sell_notional)
    - En büyük buy whale (>=100k)
    - En büyük sell whale (>=100k)
    - 500k+ buy/sell whale uyarısı
    """
    buy_notional = 0.0
    sell_notional = 0.0
    biggest_buy_whale = None
    biggest_sell_whale = None
    big_buy_whale = None
    big_sell_whale = None

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
            if notional >= WHALE_USDT_MIN:
                if (biggest_buy_whale is None) or (notional > biggest_buy_whale["usd"]):
                    biggest_buy_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                    }
            if notional >= BIG_WHALE_USDT:
                if (big_buy_whale is None) or (notional > big_buy_whale["usd"]):
                    big_buy_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                    }

        elif side == "sell":
            sell_notional += notional
            if notional >= WHALE_USDT_MIN:
                if (biggest_sell_whale is None) or (notional > biggest_sell_whale["usd"]):
                    biggest_sell_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
                        "side": side,
                        "ts": t.get("ts"),
                    }
            if notional >= BIG_WHALE_USDT:
                if (big_sell_whale is None) or (notional > big_sell_whale["usd"]):
                    big_sell_whale = {
                        "px": px,
                        "sz": sz,
                        "usd": notional,
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
        "big_buy_whale": big_buy_whale,
        "big_sell_whale": big_sell_whale,
        "has_big_buy": big_buy_whale is not None,
        "has_big_sell": big_sell_whale is not None,
    }


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
        # destek gibi çalışsın: fitil içeri girsin, kapanış daha yukarı olsun
        if close > op and close > (z_low * (1 + ZONE_BUFFER / 2)):
            return True
        return False

    # bearish FVG rejection
    if fvg["type"] == "bearish":
        # direnç gibi çalışsın: fitil içeri girsin, kapanış daha aşağıda olsun
        if close < op and close < (z_high * (1 - ZONE_BUFFER / 2)):
            return True
        return False

    return False


# ------------ Sembol Analizi (LONG + SHORT) ------------

def analyze_symbol(inst_id):
    """
    Tek coin için:
    - FVG + MSB yapısı
    - Orderflow + whale + orderbook ile filtre
    Hem LONG hem SHORT sinyalleri döndürür.
    """
    candles = get_candles(inst_id)
    if len(candles) < STRUCT_LOOKBACK + 3:
        return []

    last = candles[-1]

    trades = get_trades(inst_id)
    if not trades:
        return []

    of = analyze_trades_orderflow(trades)
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
    if structure_long:
        cond_struct = True
        cond_delta = of["net_delta"] >= NET_DELTA_MIN_POS
        cond_ob = bid_n > ask_n * ORDERBOOK_IMB_RATIO
        cond_whale = of["has_buy_whale"]

        conds = [cond_struct, cond_delta, cond_ob, cond_whale]
        true_count = sum(conds)

        if true_count >= MIN_CONDITIONS_STRICT:
            confidence = int((true_count / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "LONG",
                "last_close": last["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence,
                "structure": {
                    "bull_msb": bullish_msb,
                    "bull_level": bull_level,
                    "bull_fvg_reject": bullish_fvg_reject,
                },
            }
            signals.append(signal)

    # ---------- SHORT ---------
    if structure_short:
        cond_struct_s = True
        cond_delta_s = of["net_delta"] <= NET_DELTA_MIN_NEG
        cond_ob_s = ask_n > bid_n * ORDERBOOK_IMB_RATIO
        cond_whale_s = of["has_sell_whale"]

        conds_s = [cond_struct_s, cond_delta_s, cond_ob_s, cond_whale_s]
        true_count_s = sum(conds_s)

        if true_count_s >= MIN_CONDITIONS_STRICT:
            confidence_s = int((true_count_s / 4) * 100)
            signal = {
                "inst_id": inst_id,
                "side": "SHORT",
                "last_close": last["close"],
                "orderflow": of,
                "orderbook": book,
                "confidence": confidence_s,
                "structure": {
                    "bear_msb": bearish_msb,
                    "bear_level": bear_level,
                    "bear_fvg_reject": bearish_fvg_reject,
                },
            }
            signals.append(signal)

    return signals


# ------------ BTC & ETH Piyasa Özeti ------------

def get_trend_summary(inst_id):
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

    trades = get_trades(inst_id)
    of = analyze_trades_orderflow(trades) if trades else None

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
    big_txt = ""

    if of:
        delta_txt = f"Net delta: {of['net_delta']:.0f} USDT"
        w = of["buy_whale"]
        if w:
            whale_txt = f"Buy whale: ~${w['usd']:,.0f}"
        else:
            whale_txt = "Büyük buy whale yok"

        if of["has_big_buy"] or of["has_big_sell"]:
            big_side = "BUY" if of["has_big_buy"] else "SELL"
            big_whale = of["big_buy_whale"] if of["has_big_buy"] else of["big_sell_whale"]
            big_txt = f"500k+ {big_side} whale: ~${big_whale['usd']:,.0f}"

    return {
        "inst_id": inst_id,
        "last": last,
        "trend": trend_txt,
        "momentum": mom_txt,
        "delta_txt": delta_txt,
        "whale_txt": whale_txt,
        "big_txt": big_txt,
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
        if btc_info["big_txt"]:
            lines.append(f"- {btc_info['big_txt']}")

    if eth_info:
        lines.append(f"\n*ETH-USDT*")
        lines.append(f"- Fiyat: `{eth_info['last']:.2f}`")
        lines.append(f"- Trend: *{eth_info['trend']}*")
        lines.append(f"- Momentum: *{eth_info['momentum']}*")
        lines.append(f"- {eth_info['delta_txt']}")
        lines.append(f"- {eth_info['whale_txt']}")
        if eth_info["big_txt"]:
            lines.append(f"- {eth_info['big_txt']}")

    lines.append(f"\n*🚀 4H Giriş Sinyalleri (Top {TOP_LIMIT} USDT Spot)*")
    if not signals:
        lines.append("_Bu taramada sinyal yok._")
    else:
        big_whale_signals = []

        for s in signals:
            of = s["orderflow"]
            book = s["orderbook"]

            if s["side"] == "LONG":
                w = of["buy_whale"]
                whale_str = "Yok"
                if w:
                    whale_str = f"BUY ~${w['usd']:,.0f} @ {w['px']:.4f}"
                struct_txt = []
                if s["structure"].get("bull_msb"):
                    struct_txt.append("Bullish MSB")
                if s["structure"].get("bull_fvg_reject"):
                    struct_txt.append("Bullish FVG retest")
            else:
                w = of["sell_whale"]
                whale_str = "Yok"
                if w:
                    whale_str = f"SELL ~${w['usd']:,.0f} @ {w['px']:.4f}"
                struct_txt = []
                if s["structure"].get("bear_msb"):
                    struct_txt.append("Bearish MSB")
                if s["structure"].get("bear_fvg_reject"):
                    struct_txt.append("Bearish FVG retest")

            struct_str = ", ".join(struct_txt) if struct_txt else "Yapı: N/A"

            lines.append(f"\n*{s['inst_id']} ({s['side']})*")
            lines.append(f"- Kapanış: `{s['last_close']:.4f}`")
            lines.append(f"- Yapı: {struct_str}")
            lines.append(f"- Net delta: `{of['net_delta']:.0f} USDT`")
            lines.append(f"- Whale: {whale_str}")
            lines.append(
                f"- Orderbook (Bid/Ask notional): `{book['bid_notional']:.0f} / {book['ask_notional']:.0f}`"
            )
            lines.append(f"- Güven puanı: *%{s['confidence']}*")

            # 500k+ whale uyarısı
            if of["has_big_buy"] or of["has_big_sell"]:
                side = "BUY" if of["has_big_buy"] else "SELL"
                big_w = of["big_buy_whale"] if of["has_big_buy"] else of["big_sell_whale"]
                lines.append(f"- 🐋 500k+ {side} whale: `${big_w['usd']:,.0f}` @ {big_w['px']:.4f}")
                big_whale_signals.append((s["inst_id"], s["side"], side, big_w["usd"]))

        # Alt kısımda ekstra özet
        if big_whale_signals:
            lines.append("\n*🐋 500k+ Whale Özeti*")
            for (inst, side, wside, usd) in big_whale_signals:
                lines.append(f"- {inst} ({side}) → {wside} whale ~${usd:,.0f}")

    lines.append(f"\n_Zaman:_ `{ts()}`")
    return "\n".join(lines)


# ------------ MAIN ------------

def main():
    print(f"[{ts()}] Bot çalışıyor...")

    # BTC & ETH piyasa özeti
    btc_info = get_trend_summary("BTC-USDT")
    eth_info = get_trend_summary("ETH-USDT")

    # Top 150 USDT spot listesi
    symbols = get_spot_usdt_top_symbols(limit=TOP_LIMIT)
    if not symbols:
        print("Top USDT listesi alınamadı.")
        return

    print(f"{len(symbols)} sembol taranıyor...")

    all_signals = []
    for i, inst_id in enumerate(symbols, start=1):
        print(f"[{i}/{len(symbols)}] {inst_id} analiz ediliyor...")
        try:
            sigs = analyze_symbol(inst_id)
            if sigs:
                for s in sigs:
                    print(f"  → Sinyal bulundu: {inst_id} ({s['side']})  Güven %{s['confidence']}")
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
