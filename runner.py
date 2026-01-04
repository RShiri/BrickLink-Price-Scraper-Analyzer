import argparse
import sys
import csv
import os
import time
from scraper import BrickLinkScraper
from pricing_engine import PriceAnalyzer

# --- CSV SETUP ---
def init_csvs():
    if not os.path.exists("sets_report.csv"):
        with open("sets_report.csv", "w", newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["ID", "Name", "Type", "Year", "Status", "Market Price", "Minifigs Value", "Profit vs Figs", "Rating"])
    
    if not os.path.exists("minifigs_report.csv"):
        with open("minifigs_report.csv", "w", newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["Parent Set", "Minifig ID", "Name", "Qty", "Cond", "Unit Price", "Total Value"])

def append_set_csv(data):
    with open("sets_report.csv", "a", newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(data)

def append_minifig_csv(data):
    with open("minifigs_report.csv", "a", newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(data)

# --- DISPLAY ---
def print_basic_report(item_id, item_name, results):
    cache_date = results['meta'].get('cache_date', 'Fresh Fetch')
    
    print(f"\n{'='*70}")
    print(f"BRICKLINK REPORT: {item_id} - {item_name}")
    print(f"Last Updated: {cache_date}")
    print(f"{'='*70}")
    
    for condition in ["new", "used"]:
        res = results[condition]
        math_logic = "Insufficient Data"
        if res['confidence'] == "HIGH": math_logic = "70% Sold + 30% Listed"
        elif res['confidence'] == "MEDIUM": math_logic = "50% Sold + 50% Listed"
        elif res['confidence'] == "LOW": math_logic = "100% Listings Anchor"

        print(f"\n--- {condition.upper()} ---")
        print(f"Market Price   : {res['market_price']:.2f} ILS")
        print(f"Typical Range  : {res['range'][0]:.2f} - {res['range'][1]:.2f} ILS")
        print(f"Confidence     : {res['confidence']}")
        print(f"Data Integrity : {res['stats']['sold']['final_count']} Sales | {res['stats']['stock']['final_count']} Listings")

def print_deep_dive(results):
    deep = results["deep_dive"]
    lifecycle = deep["lifecycle"]
    sniper = deep["sniper"]
    
    print(f"\n{'-'*70}")
    print(f"🔍 STEP 2: INVESTMENT ANALYSIS")
    print(f"{'-'*70}")
    print(f"📅 STATUS: {lifecycle['status']} (Released: {lifecycle['year']})")
    
    print(f"\n🎯 SNIPER OPPORTUNITY (New)")
    if sniper and sniper['rating'] != "NO LISTINGS":
        rating_color = "\033[92m" if "GOOD" in sniper['rating'] or "EXCELLENT" in sniper['rating'] else "\033[0m"
        reset_color = "\033[0m"
        print(f"   Deal Rating      : {rating_color}{sniper['rating']}{reset_color}")
        print(f"   Cheapest Listing : {sniper['price']:.2f} ILS")
        print(f"   Potential Profit : {sniper['profit_abs']:.2f} ILS (Margin: {sniper['margin_pct']}%)")
    else:
        print("   No valid listings found.")

def print_part_out(results):
    po = results.get("part_out", {})
    if not po: return
    print(f"\n{'-'*70}")
    print(f"🧩 STEP 4: PART OUT VALUE (POV) ESTIMATOR")
    print(f"{'-'*70}")
    rating_color = "\033[92m" if po['rating'] == "HIGH" else "\033[93m" if po['rating'] == "MEDIUM" else "\033[0m"
    reset_color = "\033[0m"
    print(f"📊 SPECS: {po['parts_count']} Parts | {po.get('minifigs_count', 0)} Minifigs | {po['weight_g']}g")
    print(f"💰 RATIOS: PPP: {po['ppp']:.2f} ILS | PPG: {po['ppg']:.2f} ILS")
    print(f"🚀 PART OUT RATING: {rating_color}{po['rating']}{reset_color} ({po['reason']})")
    print(f"{'='*70}\n")

# --- PROCESSORS ---

def get_market_price(item_id, item_type, force):
    scraper = BrickLinkScraper()
    raw = scraper.scrape(item_id, item_type=item_type, force=force)
    if "error" in raw: return None, raw
    engine = PriceAnalyzer(raw)
    return engine.analyze(), raw

def analyze_set_minifigs_dry(set_id, minifig_list, force):
    total_new = 0.0
    total_used = 0.0
    
    cache_checker = BrickLinkScraper()

    print(f"   🔎 Calculating market values for {len(minifig_list)} figures...\n")
    print(f"   {'ID':<10} {'Name':<35} {'Qty':<5} {'New (ea)':<12} {'Used (ea)':<12}")
    print(f"   {'-'*10} {'-'*35} {'-'*5} {'-'*12} {'-'*12}")

    for mf in minifig_list:
        source_label = "🌐 Web"
        if not force and cache_checker._is_cache_valid(mf['id']):
            source_label = "💾 Cache"
        
        sys.stdout.write(f"\r   ⏳ Processing {mf['id']}... [{source_label}]                     ")
        sys.stdout.flush()
        
        res, _ = get_market_price(mf['id'], 'M', force)
        
        if res:
            p_new = res['new']['market_price']
            p_used = res['used']['market_price']
            
            line_new = p_new * mf['qty']
            line_used = p_used * mf['qty']
            
            total_new += line_new
            total_used += line_used
            
            print(f"\r   {mf['id']:<10} {mf['name'][:35]:<35} {mf['qty']:<5} {p_new:<12.2f} {p_used:<12.2f}")
            
            append_minifig_csv([set_id, mf['id'], mf['name'], mf['qty'], "NEW", f"{p_new:.2f}", f"{line_new:.2f}"])
            append_minifig_csv([set_id, mf['id'], mf['name'], mf['qty'], "USED", f"{p_used:.2f}", f"{line_used:.2f}"])
        else:
            print(f"\r   ❌ Error fetching {mf['id']}                               ")

    return total_new, total_used

# --- MAIN ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("item_ids", nargs="+", help="List of Item IDs")
    parser.add_argument("--type", default="S", choices=["S", "M"])
    parser.add_argument("--force", action="store_true", help="Ignore cache and force fresh scrape")
    args = parser.parse_args()
    
    init_csvs()
    
    for i, current_id in enumerate(args.item_ids):
        
        if i > 0:
            print(f"\n\n{'#'*80}")
            print(f"{'#'*30}   NEXT ITEM   {'#'*35}")
            print(f"{'#'*80}\n")

        # Auto-detect logic
        itype = "S"
        if args.type.lower() in ["m", "minifig"]: 
            itype = "M"
        elif not current_id[0].isdigit():
            print(f"   ✨ Auto-detected Minifigure ID for {current_id}. Switching type to 'M'.")
            itype = "M"

        print(f"Preparing to fetch data for {current_id} ({itype})...")
        
        temp_scraper = BrickLinkScraper()
        if not args.force and temp_scraper._is_cache_valid(current_id):
            print(f"   💾 Found data in cache. Loading...")
        else:
            print(f"   🌐 Data not in cache (or forced). Downloading from BrickLink...")

        results, raw = get_market_price(current_id, itype, args.force)
        
        if not results:
            print(f"Error: {raw['error']}")
            continue

        meta = results["meta"]
        print_basic_report(current_id, meta['item_name'], results)
        print_deep_dive(results)
        
        # --- MINIFIGS LOGIC ---
        mf_val_new = 0.0
        
        if itype == 'S':
            print(f"\n{'-'*70}")
            print(f"👥 STEP 3: MINIFIGURE BREAKDOWN (AUTO)")
            print(f"{'-'*70}")
            
            scraper = BrickLinkScraper()
            minifig_list = scraper.get_minifigs_in_set(current_id, args.force)
            
            if minifig_list:
                print(f"   ✅ Found {len(minifig_list)} minifigures.")
                mf_val_new, mf_val_used = analyze_set_minifigs_dry(current_id, minifig_list, args.force)
                
                set_price_new = results['new']['market_price']
                set_price_used = results['used']['market_price']
                
                pct_new = (mf_val_new / set_price_new * 100) if set_price_new > 0 else 0
                pct_used = (mf_val_used / set_price_used * 100) if set_price_used > 0 else 0

                print(f"\n   📊 COMPARISON (Set vs Minifigs):")
                print(f"   {'Metric':<15} {'NEW':<15} {'USED':<15}")
                print(f"   {'-'*45}")
                print(f"   {'Set Price':<15} {set_price_new:<15.2f} {set_price_used:<15.2f}")
                print(f"   {'Figs Sum':<15} {mf_val_new:<15.2f} {mf_val_used:<15.2f}")
                print(f"   {'Figs % of Set':<15} {pct_new:<14.1f}% {pct_used:<14.1f}%")
                
                if pct_new > 50: print(f"   ✨ NEW: Minifigs hold significant value!")
                else: print(f"   ❄️ NEW: Value is mostly in the bricks.")

                # Interactive Loop
                while True:
                    inspect_id = input("\n🔍 Inspect a specific Minifigure? (Enter ID, or 'n' to next Step/Item): ").strip()
                    
                    if not inspect_id or inspect_id.lower() in ['n', 'no']:
                        break 
                    
                    print(f"\n   ⚙️ Fetching report for {inspect_id}...")
                    if not temp_scraper._is_cache_valid(inspect_id):
                        print(f"   🌐 Downloading data...")
                    else:
                        print(f"   💾 Loading from cache...")

                    mf_results, mf_raw = get_market_price(inspect_id, 'M', force=False)
                    
                    if mf_results:
                        print_basic_report(inspect_id, mf_results['meta']['item_name'], mf_results)
                        print_deep_dive(mf_results)
                    else:
                        print(f"   ❌ Could not fetch data for {inspect_id}")

            else:
                # --- THIS IS THE FIX ---
                print("   🚫 No minifigures found in this set.")

        print_part_out(results)

        # Save CSV
        deep = results["deep_dive"]
        po = results.get("part_out", {})
        row = [
            meta.get("item_id"), meta.get("item_name"), itype, meta.get("year_released"),
            deep["lifecycle"]["status"], results["new"]["market_price"],
            mf_val_new, (mf_val_new - results["new"]["market_price"]),
            deep["sniper"]["rating"], po.get("rating", "N/A")
        ]
        append_set_csv(row)
        print(f"✅ Data for {current_id} saved to CSV.")

    print("\n🏁 All requested items processed.")

if __name__ == "__main__":
    main()