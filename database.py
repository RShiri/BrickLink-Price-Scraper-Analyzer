import sqlite3
import json
import os
from datetime import datetime

class Database:
    DB_NAME = "bricklink_data.db"

    def __init__(self):
        # Create DB if not exists
        self.conn = sqlite3.connect(self.DB_NAME)
        self.cursor = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        # Table for Items (Sets/Minifigs)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                json_data TEXT,
                updated_at DATETIME
            )
        ''')
        
        # Table for Inventory Lists (Which minifigs are in which set)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory_lists (
                set_id TEXT PRIMARY KEY,
                json_data TEXT,
                updated_at DATETIME
            )
        ''')
        self.conn.commit()

    def save_item(self, item_id, data):
        now = datetime.now().isoformat()
        json_str = json.dumps(data)
        self.cursor.execute('''
            INSERT OR REPLACE INTO items (item_id, json_data, updated_at)
            VALUES (?, ?, ?)
        ''', (item_id, json_str, now))
        self.conn.commit()

    def get_item(self, item_id):
        self.cursor.execute('SELECT json_data, updated_at FROM items WHERE item_id = ?', (item_id,))
        row = self.cursor.fetchone()
        if row:
            data = json.loads(row[0])
            if "meta" in data:
                data["meta"]["cache_date"] = row[1]
            return data
        return None

    def save_inventory(self, set_id, data):
        now = datetime.now().isoformat()
        json_str = json.dumps(data)
        self.cursor.execute('''
            INSERT OR REPLACE INTO inventory_lists (set_id, json_data, updated_at)
            VALUES (?, ?, ?)
        ''', (set_id, json_str, now))
        self.conn.commit()

    def get_inventory(self, set_id):
        self.cursor.execute('SELECT json_data, updated_at FROM inventory_lists WHERE set_id = ?', (set_id,))
        row = self.cursor.fetchone()
        if row:
            return json.loads(row[0]), row[1]
        return None, None

    def close(self):
        self.conn.close()