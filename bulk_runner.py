import pandas as pd
import time
from scraper import BrickLinkScraper
from pricing_engine import PriceAnalyzer
import os

def run_bulk_scan(csv_file):
    if not os.path.exists(csv_file):
        print(f"❌ Error: File {csv_file} not found.")
        return

    # 1. טעינת הסטים מהקובץ
    print(f"📂 Loading sets from {csv_file}...")
    df = pd.read_csv(csv_file)
    
    # ניקוי ה-IDs (מוריד את ה-1- אם קיים)
    set_ids = df['Number'].str.split('-').str[0].tolist()
    total = len(set_ids)
    
    print(f"🚀 Found {total} sets. Starting bulk scan...\n")
    
    scraper = BrickLinkScraper()
    
    for i, set_id in enumerate(set_ids, 1):
        print(f"[{i}/{total}] 🔍 Processing Set: {set_id}")
        
        try:
            # א. סריקת הסט עצמו
            set_data = scraper.scrape(set_id, item_type='S')
            if "error" in set_data:
                print(f"   ⚠️ Skipping set {set_id}: {set_data['error']}")
                continue
            
            # ב. ניתוח מחיר (להצגה בטרמינל)
            analysis = PriceAnalyzer(set_data).analyze()
            price = analysis.get('new', {}).get('market_price', 0)
            print(f"   💰 Market Price: {price:.2f} ILS")
            
            # ג. מציאת דמויות וסריקת המחירים שלהן (אוטומטי!)
            minifigs = scraper.get_minifigs_in_set(set_id)
            if minifigs:
                print(f"   👥 Found {len(minifigs)} minifigs. Scoping prices...")
                for mf in minifigs:
                    mf_id = mf['id']
                    # סורק מחיר לכל דמות בנפרד
                    scraper.scrape(mf_id, item_type='M')
            
            print(f"   ✅ Done with {set_id}\n")
            
            # המתנה קטנה כדי לא להיחסם
            time.sleep(2)
            
        except Exception as e:
            print(f"   ❌ Error processing {set_id}: {e}")
            continue

    print("=======================================")
    print("🎊 BULK SCAN COMPLETED!")
    print("You can now open the dashboard to see the results.")
    print("=======================================")

if __name__ == "__main__":
    # וודא ששם הקובץ תואם לקובץ שהעלית
    run_bulk_scan("BrickEconomy-Sets(2).csv")