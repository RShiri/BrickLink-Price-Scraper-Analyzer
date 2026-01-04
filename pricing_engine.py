import statistics
import re
from datetime import datetime
from typing import Dict, List, Any

class PriceAnalyzer:
    BULK_THRESHOLD = 3
    
    # רשימה מורחבת הכוללת צירופים נפוצים וביטויים של מוכרים "בעייתיים"
    BLACKLIST = [
        'incomplete', 'missing', 'no minifig', 'no minifigs', 'no figure', 
        'no figs', 'no box', 'no instructions', 'no manual', 'only build', 
        'build only', 'just build', 'instruction only', 'without minifig',
        'without minifigures', 'figures removed', 'minifigures removed',
        'no figures', 'no mf', 'no character', 'no-minifig', 'no-minifigures'
    ]

    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.meta = data.get("meta", {})

    def _is_strictly_complete(self, item: Dict) -> bool:
        price = item.get('price', 0)
        is_target = 53 <= price <= 54 
        
        # 1. בדיקה ראשונה: האם הסקרייפר כבר תייג כ-Incomplete
        if item.get('status') == 'incomplete':
            if is_target: print(f"[DEBUG] ❌ REJECTED {price}: Marked by scraper")
            return False
            
        # 2. בדיקה שנייה: חיפוש מילים בתוך התיאור המלא (ה-description)
        # אנחנו בודקים גם את ה-item עצמו וגם את שדה התיאור שחילצנו
        text_to_scan = (str(item) + " " + item.get('description', '')).lower()
        
        for word in self.BLACKLIST:
            if word in text_to_scan:
                if is_target: print(f"[DEBUG] ❌ REJECTED {price}: Found word '{word}'")
                return False
        
        # 3. בדיקת "Only"
        if 'only' in text_to_scan:
            if any(x in text_to_scan for x in ['build', 'instruction', 'parts']):
                if is_target: print(f"[DEBUG] ❌ REJECTED {price}: Found 'only' pattern")
                return False

        return True

    def analyze(self) -> Dict[str, Any]:
        results = {}
        for condition in ["new", "used"]:
            results[condition] = self._analyze_condition(condition)
        
        results["deep_dive"] = self._analyze_investment_potential(results["new"])
        results["part_out"] = self._analyze_part_out_potential(results["new"]["market_price"])
        results["meta"] = self.meta
        return results

    def _analyze_condition(self, condition: str) -> Dict[str, Any]:
        sold_raw = self.data.get(condition, {}).get("sold", [])
        stock_raw = self.data.get(condition, {}).get("stock", [])

        # הסינון קורה כאן
        sold_data = [x for x in sold_raw if self._is_strictly_complete(x)]
        stock_data = [x for x in stock_raw if self._is_strictly_complete(x)]

        sold_res = self._process_dataset(sold_data)
        stock_res = self._process_dataset(stock_data)

        sold_avg = sold_res["avg"]
        stock_anchor = self._get_competitive_stock_price(stock_res["clean_items"])
        sold_count = sold_res["final_count"]

        if sold_count >= 10:
            market_price = (sold_avg * 0.70) + (stock_anchor * 0.30)
            confidence = "HIGH"
        elif sold_count >= 3:
            market_price = (sold_avg * 0.50) + (stock_anchor * 0.50)
            confidence = "MEDIUM"
        else:
            market_price = stock_anchor
            confidence = "LOW"

        return {
            "market_price": round(market_price, 2),
            "range": (round(market_price * 0.9, 2), round(market_price * 1.1, 2)),
            "buy_target": round(market_price * 0.80, 2),
            "confidence": confidence,
            "stats": {"sold": sold_res, "stock": stock_res, "stock_anchor": stock_anchor}
        }

    def _analyze_investment_potential(self, new_data: Dict) -> Dict[str, Any]:
        year = self.meta.get("year_released")
        curr = datetime.now().year
        status, desc = "UNKNOWN", "Year not found"
        
        if year:
            age = curr - year
            if age <= 1: status, desc = "NEW", "Flooded market"
            elif 2 <= age <= 4: status, desc = "EOL WATCH", "Production ending soon"
            else: status, desc = "RETIRED", "Production stopped"

        # שאיבה מהרשימה המטוהרת בלבד
        stock = new_data["stats"]["stock"]["clean_items"]
        mkt = new_data["market_price"]
        best = None
        
        if stock:
            # מציאת הכי זול מבין הסטים השלמים באמת
            cheapest_item = sorted(stock, key=lambda x: x['price'])[0]
            cheapest_price = cheapest_item['price']
            
            profit, margin = 0, 0
            if mkt > 0 and cheapest_price > 0:
                profit = mkt - (cheapest_price * 1.13)
                margin = (profit / cheapest_price) * 100
            
            rating = "IRRELEVANT"
            if margin >= 20: rating = "EXCELLENT"
            elif margin >= 10: rating = "GOOD"
            
            if status in ["EOL WATCH", "RETIRED"] and rating == "GOOD": 
                rating = "GREAT INVEST"
            
            best = {
                "price": cheapest_price, 
                "margin_pct": round(margin, 1), 
                "profit_abs": round(profit, 2), 
                "rating": rating
            }
        
        return {"lifecycle": {"status": status, "year": year, "desc": desc}, "sniper": best}

    def _get_competitive_stock_price(self, items):
        if not items: return 0.0
        sorted_s = sorted(items, key=lambda x: x['price'])
        cutoff = max(3, int(len(sorted_s) * 0.35))
        return self._weighted_avg(sorted_s[:cutoff])

    def _weighted_avg(self, items):
        if not items: return 0.0
        val = sum(x["price"] * x["qty"] for x in items)
        qty = sum(x["qty"] for x in items)
        return val / qty if qty > 0 else 0.0

    def _analyze_part_out_potential(self, mkt: float) -> Dict[str, Any]:
        specs = self.meta.get("specs", {})
        parts, w, minifigs = specs.get("parts", 0), specs.get("weight_g", 0), specs.get("minifigs", 0)
        ppp = mkt / parts if parts > 0 else 0
        ppg = mkt / w if w > 0 else 0
        rating, reason = "LOW", "Expensive per piece"
        if parts > 0:
            if ppp < 0.25: rating, reason = "HIGH", "Excellent PPP (<0.25)"
            elif ppp < 0.35: rating, reason = "MEDIUM", "Decent PPP"
        return {"ppp": round(ppp, 3), "ppg": round(ppg, 3), "parts_count": parts, 
                "weight_g": w, "minifigs_count": minifigs, "rating": rating, "reason": reason}

    def _process_dataset(self, items: List[Dict]) -> Dict[str, Any]:
        # סינון כפול: גם לפי הסטטוס מהסקרייפר וגם לפי ה-Blacklist המורחב
        complete_items = [
            x for x in items 
            if x.get('status') == 'complete' and self._is_strictly_complete(x)
        ]
        
        if not complete_items:
            return {"avg": 0.0, "final_count": 0, "clean_items": []}

        # המשך הלוגיקה...
        no_bulk = [x for x in complete_items if x["qty"] <= self.BULK_THRESHOLD]
        
        final = no_bulk
        if len(no_bulk) >= 5:
            try:
                vals = []
                for x in no_bulk: vals.extend([x["price"]] * x["qty"])
                if len(vals) >= 4:
                    q1, q3 = statistics.quantiles(vals, n=4)[0], statistics.quantiles(vals, n=4)[2]
                    iqr = q3 - q1
                    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                    # סינון Outliers סטטיסטי
                    final = [x for x in no_bulk if low <= x["price"] <= high]
            except: 
                pass
                
        return {
            "avg": self._weighted_avg(final), 
            "final_count": len(final), 
            "clean_items": final
        }