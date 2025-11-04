from main_scan import get_kline, analyze, mexc_symbols, telegram
from datetime import datetime

def main():
    telegram(f"⏱ 1H tarama başladı: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    syms = mexc_symbols()
    found = []
    for s in syms[:100]:
        df = get_kline(s, "1h")
        res = analyze(s, df)
        if res: found.append(res)
    if found:
        telegram("📊 1H Sinyaller:\n" + "\n".join(found))
    else:
        telegram("ℹ️ 1H sinyal yok.")
    telegram("✅ 1H tarama tamamlandı.")
if __name__ == "__main__":
    main()
