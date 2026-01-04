import streamlit as st
import pandas as pd
import json
import sqlite3
from database import Database
from pricing_engine import PriceAnalyzer

# 1. Page Configuration
st.set_page_config(page_title="BrickLink Sniper", layout="wide")

# 2. Fixed Image Logic
def get_img_url(item_id):
    item_id = str(item_id).strip()
    is_fig = any(c.isalpha() for c in item_id)
    
    if is_fig:
        # Minifigures: MN/0/sh0584.png
        return f"https://img.bricklink.com/ItemImage/MN/0/{item_id}.png"
    else:
        # Sets: SN/0/76255-1.png (Adding -1 if missing)
        img_id = item_id if "-" in item_id else f"{item_id}-1"
        return f"https://img.bricklink.com/ItemImage/SN/0/{img_id}.png"

# 3. Database Delete Function
def delete_from_db(item_id):
    db = Database()
    try:
        # Delete from items table
        db.cursor.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
        # Delete from inventory if it exists
        db.cursor.execute("DELETE FROM inventory_lists WHERE set_id = ?", (item_id,))
        db.conn.commit()
        st.toast(f"Deleted {item_id} from Database", icon="🗑️")
    except Exception as e:
        st.error(f"Delete failed: {e}")
    finally:
        db.close()

# 4. Data Loading & Cleaning
@st.cache_data(show_spinner=False)
def load_data():
    db = Database()
    db.cursor.execute("SELECT item_id, json_data, updated_at FROM items")
    rows = db.cursor.fetchall()
    db.close()

    sets, figs = [], []
    # Stricter filter for junk entries
    junk_keywords = ['python', 'streamlit', 'n', 'test', 'runner', 'cmd']

    for row in rows:
        item_id = str(row[0]).strip()
        
        # Filter out junk
        if any(key in item_id.lower() for key in junk_keywords) or len(item_id) < 2:
            continue

        try:
            raw_data = json.loads(row[1])
            analysis = PriceAnalyzer(raw_data).analyze()
            sniper = analysis.get("deep_dive", {}).get("sniper", {})
            
            # Formatting Year
            yr = analysis.get("meta", {}).get("year_released")
            year_val = int(float(yr)) if yr and str(yr).replace('.0','').isdigit() else "N/A"

            item = {
                "ID": item_id,
                "Image": get_img_url(item_id),
                "Name": analysis.get("meta", {}).get("item_name", "Unknown"),
                "Year": year_val,
                "Price": analysis.get("new", {}).get("market_price", 0),
                "Profit": sniper.get("profit_abs", 0),
                "Margin %": sniper.get("margin_pct", 0),
                "Rating": sniper.get("rating", "N/A")
            }
            
            if any(c.isalpha() for c in item_id): figs.append(item)
            else: sets.append(item)
        except: continue
    
    return pd.DataFrame(sets), pd.DataFrame(figs)

st.title("🧱 BrickLink Sniper Dashboard")

df_sets, df_figs = load_data()

# --- MAIN VIEW ---
st.subheader("📦 Sets")
if not df_sets.empty:
    df_sets = df_sets.sort_values("Profit", ascending=False)
    # Selection Event
    set_selection = st.dataframe(
        df_sets,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Image": st.column_config.ImageColumn("Preview"),
            "Price": st.column_config.NumberColumn(format="%.2f ₪"),
            "Profit": st.column_config.NumberColumn(format="%.2f ₪"),
            "Margin %": st.column_config.NumberColumn(format="%.1f%%"),
        }
    )
else:
    st.info("No sets found in database.")

st.divider()

# --- SIDEBAR: DELETE & DETAILS ---
st.sidebar.header("Item Actions")
selected_id = None

# If a row is clicked in the main table
if 'set_selection' in locals() and len(set_selection.selection.rows) > 0:
    row_idx = set_selection.selection.rows[0]
    selected_item = df_sets.iloc[row_idx]
    selected_id = selected_item["ID"]
    
    st.sidebar.image(selected_item["Image"], caption=selected_item["Name"])
    st.sidebar.markdown(f"**ID:** `{selected_id}`")
    st.sidebar.markdown(f"**Profit:** {selected_item['Profit']:.2f} ₪")
    
    if st.sidebar.button("🗑️ Delete from DB", type="primary", use_container_width=True):
        delete_from_db(selected_id)
        st.cache_data.clear() # Force clear cache to update UI
        st.rerun()

# --- DYNAMIC MINIFIGS TABLE ---
if selected_id:
    st.subheader(f"👥 Minifigures in {selected_id}")
    db = Database()
    db.cursor.execute("SELECT json_data FROM inventory_lists WHERE set_id = ?", (selected_id,))
    inv = db.cursor.fetchone()
    db.close()
    
    if inv:
        ids_in_set = [m['id'] for m in json.loads(inv[0])]
        relevant_figs = df_figs[df_figs["ID"].isin(ids_in_set)]
        if not relevant_figs.empty:
            st.dataframe(relevant_figs, use_container_width=True, hide_index=True,
                         column_config={"Image": st.column_config.ImageColumn("Preview"), 
                                        "Price": st.column_config.NumberColumn(format="%.2f ₪")})
        else:
            st.info("Minifigure data not found. Scrape this set again.")
    else:
        st.warning("No inventory found for this set.")
else:
    st.subheader("👤 All Tracked Minifigures")
    if not df_figs.empty:
        st.dataframe(df_figs, use_container_width=True, hide_index=True,
                     column_config={"Image": st.column_config.ImageColumn("Preview"),
                                    "Price": st.column_config.NumberColumn(format="%.2f ₪")})