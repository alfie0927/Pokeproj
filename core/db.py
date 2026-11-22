"""SQLite storage. One file, stdlib only."""
import json
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
  item_id TEXT PRIMARY KEY, card_key TEXT, bucket TEXT, title TEXT,
  price REAL, shipping REAL, sold_at TEXT, url TEXT, fetched_at TEXT);
CREATE INDEX IF NOT EXISTS sales_card ON sales(card_key);
CREATE TABLE IF NOT EXISTS asks (
  item_id TEXT, seen_on TEXT, card_key TEXT, bucket TEXT, title TEXT, total REAL, url TEXT,
  PRIMARY KEY (item_id, seen_on));
CREATE INDEX IF NOT EXISTS asks_card ON asks(card_key, seen_on);
CREATE TABLE IF NOT EXISTS ask_daily (
  card_key TEXT, seen_on TEXT, bucket TEXT, median REAL, n INTEGER, PRIMARY KEY (card_key, seen_on, bucket));
CREATE INDEX IF NOT EXISTS ask_daily_day ON ask_daily(seen_on);
CREATE TABLE IF NOT EXISTS fetches (card_key TEXT PRIMARY KEY, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS tracked (
  item_id TEXT PRIMARY KEY, card_key TEXT, bucket TEXT, title TEXT, price REAL, shipping REAL, url TEXT,
  buying TEXT, end_at TEXT, qty_sold INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
  first_seen TEXT, last_seen TEXT);
CREATE INDEX IF NOT EXISTS tracked_card ON tracked(card_key, status);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, front_path TEXT, back_path TEXT, status TEXT,
  data_json TEXT, listing_id TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS watch (card_key TEXT PRIMARY KEY, lang TEXT, card_id TEXT, name TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS cards_seen (
  card_key TEXT PRIMARY KEY, lang TEXT, card_id TEXT, name TEXT, set_name TEXT, image TEXT, rarity TEXT,
  number TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS card_values (card_key TEXT PRIMARY KEY, value REAL, source TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS products (
  key TEXT PRIMARY KEY, set_id TEXT, lang TEXT, era TEXT, set_name TEXT, logo TEXT, release TEXT,
  kind TEXT, name TEXT, packs INTEGER, query TEXT, price REAL, n INTEGER, image TEXT,
  ev_pack REAL, product_ev REAL, ratio REAL, score REAL, trend30 REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS deals (
  item_id TEXT PRIMARY KEY, found_at TEXT, card_key TEXT, name TEXT, set_name TEXT, number TEXT, bucket TEXT,
  title TEXT, total REAL, market REAL, basis TEXT, ratio REAL, image TEXT, url TEXT, status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS deal_seen (item_id TEXT PRIMARY KEY, seen_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS followed (
  card_key TEXT PRIMARY KEY, lang TEXT, card_id TEXT, name TEXT, number TEXT, priority INTEGER, last_seen TEXT,
  reason TEXT);
CREATE TABLE IF NOT EXISTS api_log (day TEXT PRIMARY KEY, calls INTEGER);
CREATE TABLE IF NOT EXISTS auctions (
  item_id TEXT PRIMARY KEY, found_at TEXT, card_key TEXT, name TEXT, set_name TEXT, number TEXT, bucket TEXT,
  rarity TEXT, era TEXT, title TEXT, bid REAL, bids INTEGER, postage REAL, market REAL, basis TEXT, ratio REAL,
  max_bid REAL, image TEXT, url TEXT, end_at TEXT, location TEXT, status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS inventory (
  id INTEGER PRIMARY KEY, card_key TEXT, name TEXT, set_name TEXT, number TEXT, bucket TEXT, image TEXT,
  source_url TEXT, cost REAL, bought_at TEXT, status TEXT DEFAULT 'owned', draft_id INTEGER, sold_price REAL,
  sold_at TEXT, notes TEXT);
"""
MIGRATIONS = ["ALTER TABLE sales ADD COLUMN source TEXT DEFAULT 'soldcomps'",
              "ALTER TABLE asks ADD COLUMN image TEXT",
              "ALTER TABLE deals ADD COLUMN era TEXT",
              "ALTER TABLE deals ADD COLUMN listed_at TEXT",
              "ALTER TABLE deals ADD COLUMN location TEXT",
              "ALTER TABLE deals ADD COLUMN rarity TEXT",
              "ALTER TABLE sales ADD COLUMN best_offer INTEGER DEFAULT 0",
              "ALTER TABLE tracked ADD COLUMN qty_seen INTEGER",
              "ALTER TABLE products ADD COLUMN basis TEXT",
              "ALTER TABLE products ADD COLUMN sold_n INTEGER",
              "ALTER TABLE products ADD COLUMN ebay_share REAL",
              "ALTER TABLE deals ADD COLUMN profit REAL",
              "ALTER TABLE deals ADD COLUMN roi REAL",
              "ALTER TABLE deals ADD COLUMN condition TEXT",
              "ALTER TABLE deals ADD COLUMN risk TEXT",
              "ALTER TABLE auctions ADD COLUMN condition TEXT",
              "ALTER TABLE auctions ADD COLUMN risk TEXT",
              "ALTER TABLE deals ADD COLUMN notes TEXT",
              "ALTER TABLE auctions ADD COLUMN notes TEXT"]

DEFAULTS = {
    "strategy": "market",          # market | lowest
    "pct_below": 5.0,
    "postage": 0.0,                # £ you charge buyers; deducted so totals compare fairly
    "tracked_policy": "",          # optional 2nd postage policy (e.g. Tracked 48), used from tracked_over up
    "tracked_over": 20.0, "postage_tracked": 0.0,
    "mult_LP": 0.8, "mult_MP": 0.6, "mult_HP": 0.4,
    "stale_hours": 24,            # asking-price snapshots (free)
    "sold_stale_hours": 168,      # SoldComps sold comps (free tier is small)
    "eur_gbp": 0.85,               # Cardmarket EU reference only (shown, never used for prices)
    "usd_gbp": 0.75,               # TCGplayer market prices -> £ (One Piece card values in Sealed EV)
    "browse_budget": 4500,
    "postage_cost": 1.55,          # what posting a card costs YOU when you resell it (deal profit)
    "sell_fee_pct": 0.0,           # selling fee % (UK private sellers: 0; buyers pay eBay's protection fee)
    "deal_min_profit": 0.0,        # hide deals below this £ profit
    "deal_alert": True,            # beep + Windows notification when a deal lands
    "auction_window_hours": 2,     # auction sniper: auctions ending within this many hours         # Browse API calls/day the app may use (eBay's free limit is 5,000)
    "deal_pct_modern": 70,        # Deal finder: modern (XY onwards) listings at or under this % of market
    "deal_pct_vintage": 50,       # ...vintage/older (WotC, EX, DP, BW) at or under this %
    "deal_auto": True,            # scan continuously in the background while the app runs
    "deal_ocr": True,             # OCR listing photos when the title has no card number
    "deal_min_market": 5.0,       # ...and only cards worth at least this much (skips £2 commons)
    "best_offer": False,           # new listings accept offers; eBay answers them automatically (below)
    "bo_accept_pct": 92.0,         # ...accept offers at or above this % of your price
    "bo_decline_pct": 80.0,        # ...decline offers under this %; in between you decide in Seller Hub
    "stale_days": 14,              # Live on eBay: flag listings unsold after this many days
    "sheet_auto": True, "sheet_add": False,            # update the Excel workbook after posting, price pushes and sales
    "sheet_path": "",              # "" = the .xlsm in the project folder
    "ebay_username": "",
    "fulfillment_policy": "", "payment_policy": "", "return_policy": "",
    "location_key": "home", "postcode": "",
    "footer": "Shipped in a penny sleeve and top loader. Please see photos — they form part of the description.",
}

_ready = False
_local = threading.local()


def conn():
    """This thread's connection, kept open. (Opening one per query cost 0.5 ms each, and with WAL the last
    connection to close checkpoints the whole log, so every small write paid for a full sync.)"""
    global _ready
    c = getattr(_local, "conn", None)
    if c is None or _local.path != DB:
        DATA.mkdir(exist_ok=True)
        c = sqlite3.connect(DB, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=NORMAL")   # with WAL: safe against crashes, far cheaper small writes
        _local.conn, _local.path = c, DB
    if not _ready:
        c.execute("PRAGMA journal_mode=WAL")   # pages read while the background scanner writes (kept in the file)
        c.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                c.execute(m)
            except sqlite3.OperationalError:
                pass  # column already there
        _ready = True
    return c


def q(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).fetchall()


def run(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


def many(sql, rows):
    with conn() as c:
        c.executemany(sql, rows)


def setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else DEFAULTS.get(key, default)


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
