from main_scan import get_kline, analyze, mexc_symbols, telegram
from datetime import datetime

def main():
    telegram(f"⏱ Günlük tarama başladı: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    syms = mexc_symbols()
    found = []
    for s in syms[:100]:
        df = get_kline(s, "1d")
        res = analyze(s, df)
        if res: found.append(res)
    if found:
        telegram("📊 Günlük Sinyaller:\n" + "\n".join(found))
    else:
        telegram("ℹ️ Günlük sinyal yok.")
    telegram("✅ Günlük tarama tamamlandı.")
if __name__ == "__main__":
    main()
