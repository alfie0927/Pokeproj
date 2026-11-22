"""eBay UK prices: sold comps (inferred from eBay listings that end sold, your own orders, optional SoldComps)
+ asking-price snapshots -> condition buckets -> market value."""
import os
import re
import shlex
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import requests

from . import catalog, db, ebay

SOLDCOMPS = "https://www.soldcomps.io/v1/sold"
JUNK = "-proxy -custom -orica -choose -pick"
DROP_FLAGS = {"lot", "for_parts", "box_only", "replica", "accessory"}  # 'outlier' ignored: computed over mixed conditions
GRADES = r"10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4\.5|4|3\.5|3|2\.5|2|1\.5|1"
GRADED = re.compile(rf"\b(PSA|BGS|BECKETT|CGC|SGC|TAG|ACE)\s*(?:GEM\s*MI?N?T\s*|MINT\s*|PRISTINE\s*|NM-MT\s*)?({GRADES})\b", re.I)
PLAYED = re.compile(r"\b(damaged|creased?|crease|played|LP|MP|DMG|heavily)\b", re.I)


SALE_INSERT = ("INSERT OR REPLACE INTO sales (item_id, card_key, bucket, title, price, shipping, sold_at, url, "
               "fetched_at, source, best_offer) VALUES (?,?,?,?,?,?,?,?,?,?,?)")


class SoldCompsError(Exception):
    pass


def bucket(title):
    m = GRADED.search(title)
    if m:
        grader = "BGS" if m[1].upper() == "BECKETT" else m[1].upper()
        return f"{grader} {m[2]}"
    return "Raw played" if PLAYED.search(title) else "Raw"


def card_query(name_en, number, lang="en", variant="normal"):
    q = f'"{name_en}" {number}'
    q += " reverse" if variant == "reverse_holo" else " -reverse"
    q += ' "1st edition"' if variant == "first_edition" else " -1st"
    q += " japanese" if lang == "ja" else " -japanese -jpn -korean -chinese"
    return f"{q} {JUNK}"


def _soldcomps(query, limit=100):
    key = os.environ.get("SOLDCOMPS_KEY")
    if not key:
        raise SoldCompsError("SOLDCOMPS_KEY missing in .streamlit/secrets.toml")
    for attempt in range(4):
        r = requests.get(SOLDCOMPS, params={"q": query, "site": "EBAY_GB", "days": 90, "limit": limit},
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if r.status_code in (429, 502) and attempt < 3:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt * 2)))
            continue
        if r.status_code == 402:
            raise SoldCompsError("SoldComps credits used up for this month (HTTP 402)")
        if r.status_code >= 400:
            raise SoldCompsError(f"SoldComps {r.status_code}: {r.text[:200]}")
        return r.json()


def clean(items, fixed_bucket=None):
    """SoldComps items -> rows (item_id, bucket, title, price, shipping, sold_at, url)."""
    out = []
    for it in items:
        if DROP_FLAGS & set(it.get("flags") or []) or it.get("best_offer_price_known") is False:
            continue
        ship = (it.get("shipping") or {}).get("value") or 0.0
        out.append((it["item_id"], fixed_bucket or bucket(it["title"]), it["title"],
                    it["sold_price"]["value"], ship, it["sold_at"], it.get("url")))
    return out


def refresh(card_key, query, fixed_bucket=None, force=False):
    """Fetch sold comps if stale and a SoldComps key exists (free tier: ~14 fetches/month at limit 30).
    Returns True if a fetch happened."""
    if not os.environ.get("SOLDCOMPS_KEY"):
        return False
    if not force:
        r = db.q("SELECT fetched_at FROM fetches WHERE card_key=?", (card_key,))
        if r and datetime.fromisoformat(r[0]["fetched_at"]) > datetime.now(timezone.utc) - timedelta(hours=db.setting("sold_stale_hours")):
            return False
    now = datetime.now(timezone.utc).isoformat()
    rows = clean(_soldcomps(query, limit=30)["items"], fixed_bucket)
    db.many(SALE_INSERT, [(r[0], card_key, *r[1:], now, "soldcomps", 0) for r in rows])
    db.run("INSERT OR REPLACE INTO fetches VALUES (?,?)", (card_key, now))
    return True


def summarize(sales, today=None):
    """Pure. sales: [(total, 'YYYY-MM-DD'[, best_offer])] any order -> stats dict, or None.
    Uses the last 90 days (all sales if none are that recent, flagged 'stale'); drops Best-Offer sales (true price
    hidden, at or below list) when >=3 others exist; drops IQR outliers. market = median of the last 30 days when
    there are >=3 sales in it, else median of the latest 10."""
    if not sales:
        return None
    today = today or date.today()
    age = lambda d: (today - date.fromisoformat(d[:10])).days
    sales = sorted([(s[0], s[1], bool(s[2]) if len(s) > 2 else False) for s in sales], key=lambda s: s[1], reverse=True)
    recent = [s for s in sales if age(s[1]) <= 90]
    stale, sales = not recent, recent or sales
    clean_ = [s for s in sales if not s[2]]
    incl_bo = len(clean_) < 3 and len(clean_) < len(sales)
    sales = clean_ if len(clean_) >= 3 else sales
    kept = set(iqr_filter([t for t, _, _ in sales]))
    sales = [s for s in sales if s[0] in kept]

    def window(days):
        w = [t for t, d, _ in sales if age(d) <= days]
        return round(statistics.median(w), 2) if w else None

    d30 = [t for t, d, _ in sales if age(d) <= 30]
    market, win = (statistics.median(d30), "30 d") if len(d30) >= 3 else \
        (statistics.median([t for t, _, _ in sales[:10]]), f"last {min(len(sales), 10)}")
    return {"market": round(market, 2), "n": len(sales), "n30": len(d30), "window": win, "last_sold": sales[0][1][:10],
            "d7": window(7), "d30": window(30), "d90": window(90), "low_confidence": len(sales) < 3,
            "incl_best_offer": incl_bo, "stale": stale, "kind": "sold"}


def values(card_key, postage=True):
    """{bucket: stats} for a card, from stored sales (all sources; stats['sources'] says which).
    postage=False: item prices only (how pokepulse quotes a market price)."""
    by, src = {}, {}
    lang = card_key.split(":", 1)[0]
    num = card_number(card_key)
    total = "price+shipping" if postage else "price"
    for r in db.q(f"SELECT bucket, {total} AS total, sold_at, source, COALESCE(best_offer, 0) bo, title FROM sales "
                  "WHERE card_key=?", (card_key,)):
        if r["source"] != "own" and not card_key.startswith("sealed:") and junk_title(r["title"], lang, num):
            continue
        by.setdefault(r["bucket"], []).append((r["total"], r["sold_at"], r["bo"]))
        src.setdefault(r["bucket"], set()).add(r["source"] or "soldcomps")
    order = lambda b: (b.startswith("Raw") is False, b)
    out = {}
    for b, s in sorted(by.items(), key=lambda kv: order(kv[0])):
        out[b] = summarize(s)
        out[b]["sources"] = sorted(src[b])
    return out


def comps(card_key, bucket_=None):
    sql = "SELECT * FROM sales WHERE card_key=?" + (" AND bucket=?" if bucket_ else "") + " ORDER BY sold_at DESC"
    return [dict(r) for r in db.q(sql, (card_key, bucket_) if bucket_ else (card_key,))]


# ---------- Free, unlimited source: active eBay UK listings (Browse API, 5,000 calls/day) ----------

TITLE_JUNK = ("proxy", "custom", "orica", "fake", "replica", "lot", "bundle", "choose", "pick", "digital",
              "code card", "jumbo", "empty", "opened",
              # merch and accessories that name the card (seen in live UK results)
              "sticker", "stickers", "binder", "insert", "inserts", "display", "case", "metal", "poster", "a4",
              "keyring", "keychain", "key ring", "extended art", "extended artwork", "frame", "framed", "acrylic",
              "playmat", "sleeve", "sleeves", "magnet", "fan art", "fanart", "gold plated", "24k", "coin", "figure",
              "plush", "art print", "canvas")
SEALED_JUNK = ("fake", "replica", "empty", "opened", "lot", "proxy", "custom", "sleeves", "sleeve", "deck box",
               "dice", "coin", "poster", "art card", "code card", "damage counters", "binder", "playmat", "wrapper",
               "display stand", "box only", "x2", "x3", "x4", "x5", "bundle of", "joblot", "job lot", "resealed",
               "pack only", "packs only", "loose packs", "mystery")
OTHER_LANG = ("japanese", "jpn", "jap", "jp", "japan", "korean", "chinese", "spanish", "español", "espanol", "portuguese", "português",
              "portugues", "french", "français", "francais", "german", "deutsch", "italian", "italiano", "thai",
              "indonesian", "dutch", "polish")


def _has(t, word):
    return re.search(rf"(?<![\w]){re.escape(word.lower())}(?![\w])", t) is not None


@lru_cache(maxsize=512)
def _words_re(words):
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w.lower()) for w in words) + r")(?![\w])")


def _has_any(t, words):
    """Any of `words` present as a whole word: one compiled pattern per word list (a search per word was 12,000
    regex searches for a single market value)."""
    return bool(words) and _words_re(tuple(words)).search(t) is not None


def _required(t, w):
    """Pure. A required phrase is present; '(op09,op-09)' means any of the alternatives (eBay's OR group syntax)."""
    w = w.lower()
    if w.startswith("(") and w.endswith(")"):
        return any(a.strip() in t for a in w[1:-1].split(","))
    return w in t


_SLASH = re.compile(r"(?<![\w/])#?([a-z]{0,4}\d{1,4})\s*/\s*([a-z]{0,4}\d{1,4})(?![\d/])")
_PROMO = re.compile(r"\b(mep|svp|swsh|sm|xy|bw|dp|hgss|pr)\s*-?\s*(?:en\s*)?(\d{1,3})\b")


def _num(s):
    """Pure. '017' -> '17', 'TG05' -> 'tg5', 'svp 027' -> 'svp27'."""
    s = re.sub(r"\s+", "", s.lower())
    return re.sub(r"(?<![0-9])0+(\d)", r"\1", s)


def number_conflict(title, number):
    """Pure. The title names another card's printed number, so it is a different card whatever its name says:
    a printed number ('017/078', 'Pikachu 179/131') with none of them ours (card number AND set size),
    or a promo code ('MEP 131', 'SVP 027') when ours isn't that promo. 'Stage 2' or '50 HP' don't count.
    Seen in stored sales: 'Blastoise 017/078 Pokémon GO' as Base Set 2/102, 'Pikachu 12/17' as 13/17."""
    t = title.lower()
    left, _, total = number.lower().partition("/")
    mine_left, mine_total = _num(left), _num(total)
    found = _SLASH.findall(t)
    if found and not any(_num(a) == mine_left and (not mine_total or _num(b) == mine_total) for a, b in found):
        return True
    promos = [_num(p + n) for p, n in _PROMO.findall(t)]
    # only for a numbered set card ('131/131'): a promo of ours ('027', 'SM210') is written with these codes
    return bool(promos and mine_total) and mine_left not in promos


def title_ok(title, required=(), excluded=TITLE_JUNK, number=None, lang=None):
    """Does an active-listing title describe this exact card/product?"""
    t = title.lower()
    if _has_any(t, excluded) or not all(_required(t, w) for w in required):
        return False
    if number:
        left, _, total = number.lower().partition("/")
        if not (_has(t, left) or _has(t, left.lstrip("0") or "0")):
            return False
        if number_conflict(t, number):
            return False
    if lang == "en" and _has_any(t, OTHER_LANG):
        return False
    if lang == "ja" and not _has_any(t, ("japanese", "jpn", "japan")):
        return False
    if lang == "zh" and not _has_any(t, ("chinese", "simplified", "s-chinese")):
        return False
    return True


def lang_word(lang):
    """Word added to an eBay search so it finds that language's copies (English needs none)."""
    return {"ja": " japanese", "zh": " chinese"}.get(lang, "")


def junk_title(title, lang="en", number=None):
    """Accessory/fake listing, a multi-card lot ('x10 Pikachu'), (for English cards) another language's copy, or
    (when the card's number is given) a title naming another card's number: never a price point."""
    t = (title or "").lower()
    return _has_any(t, TITLE_JUNK) or bool(LOT.search(t)) or (lang == "en" and _has_any(t, OTHER_LANG)) \
        or bool(number and number_conflict(t, number))


@lru_cache(maxsize=8192)
def card_number(card_key):
    """The card's printed number ('183/165', 'TG05'), from what the app already stored, else the catalog; None for
    sealed products or when unknown. Lets stored sales/listings naming another number be ignored when read."""
    if card_key.startswith("sealed:"):
        return None
    r = db.q("SELECT number FROM followed WHERE card_key=? AND number IS NOT NULL AND number != '' "
             "UNION ALL SELECT number FROM cards_seen WHERE card_key=? AND number IS NOT NULL AND number != '' LIMIT 1",
             (card_key, card_key))
    if r:
        return r[0]["number"]
    try:
        lang, cid = card_key.split(":", 1)
        c = catalog.card(lang, cid)
        return catalog.printed_number(c) if c else None
    except Exception:
        return None


LOT = re.compile(r"(?<![\w/])(?:x\s?(?:[2-9]|\d{2,3})|(?:[2-9]|\d{2,3})\s?x)(?![\w/])")   # 'x10', '3x'; not '×2' weakness


def parse_query(q):
    """'"151" "booster bundle" sealed -opened -"pokemon center"' -> ('151 booster bundle sealed', [phrases], [excluded])."""
    parts = shlex.split(q)
    inc = [p for p in parts if not p.startswith("-")]
    exc = [p[1:] for p in parts if p.startswith("-")]
    return " ".join(inc), inc, exc


def ask_bucket(item):
    """Bucket an active listing using eBay's condition (graded vs raw) + the title (grade)."""
    t = item["title"]
    if item.get("conditionId") == "2750":
        return bucket(t) if GRADED.search(t) else None   # graded but grade unreadable -> skip
    if GRADED.search(t):
        return None                                        # raw listing mentioning PSA etc. -> ambiguous
    return "Raw played" if PLAYED.search(t) else "Raw"


TRACK_UPSERT = """INSERT INTO tracked (item_id, card_key, bucket, title, price, shipping, url, buying, end_at, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
price=excluded.price, shipping=excluded.shipping, end_at=excluded.end_at, last_seen=excluded.last_seen"""


def _ship(item):
    return float(((item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}).get("value", 0))


def refresh_asks(card_key, query, check, fixed_bucket=None, condition_ids=None, category=ebay.CATEGORY, force=False,
                 buying="FIXED_PRICE", stale_hours=None, ref=None):
    """Snapshot today's active UK listings (asks) and remember each one in `tracked` so we can later see if it sold.
    buying='AUCTION' only tracks (current bids aren't asking prices). check(title)->bool. Returns True if fetched.
    ref: an outside reference price for sealed products (TCGplayer's box price); listings under 40 % of it are
    dropped even when they outnumber the real ones (UK eBay's £10 'sealed booster box' scam listings)."""
    fkey = ("ask:" if buying == "FIXED_PRICE" else "auc:") + card_key
    if not force:
        r = db.q("SELECT fetched_at FROM fetches WHERE card_key=?", (fkey,))
        hours = stale_hours or db.setting("stale_hours")
        if r and datetime.fromisoformat(r[0]["fetched_at"]) > datetime.now(timezone.utc) - timedelta(hours=hours):
            return False
    now = datetime.now(timezone.utc)
    asks, track = [], []
    for it in ebay.search_active(query, condition_ids, category=category, buying=buying):
        b = fixed_bucket or ask_bucket(it)
        if not (b and check(it["title"])):
            continue
        price = float((it.get("currentBidPrice") or it["price"])["value"])
        if risky_seller(it, price):
            continue                        # likely fake/scam: never a price point or a "sale"
        if buying == "FIXED_PRICE":
            asks.append((it["itemId"], now.date().isoformat(), card_key, b, it["title"], round(price + _ship(it), 2),
                         it.get("itemWebUrl"), (it.get("image") or {}).get("imageUrl")))
        track.append((it["itemId"], card_key, b, it["title"], price, _ship(it), it.get("itemWebUrl"), buying,
                      it.get("itemEndDate"), now.isoformat(), now.isoformat()))
    if fixed_bucket == "Sealed":
        sold = (values(card_key).get("Sealed") or {}).get("market")
        asks = drop_cheap(asks, 5, ref=max(filter(None, [ref, sold]), default=None))   # accessories/fakes/scams
        kept = {a[0] for a in asks}
        track = [t for t in track if t[0] in kept]   # an accessory must never become a "sale"
    elif buying == "FIXED_PRICE" and category == ebay.CATEGORY and sum(a[3] == "Raw" for a in asks) >= 8:
        # one more search (eBay's structured condition) on cards with enough raw listings for the mix to matter:
        # ~5 % of 'NM-looking' titles are declared LP/MP there, and they sit at the cheap end that sets the value
        played = {i["itemId"] for i in ebay.search_active(query, condition_ids, category=category, aspect=ebay.PLAYED_ASPECT)}
        asks, track = rebucket_played(asks, track, played)
    db.many("INSERT OR REPLACE INTO asks (item_id, seen_on, card_key, bucket, title, total, url, image) "
            "VALUES (?,?,?,?,?,?,?,?)", asks)
    db.many(TRACK_UPSERT, track)
    if buying == "FIXED_PRICE":
        rollup_day(card_key, now.date().isoformat())
    db.run("INSERT OR REPLACE INTO fetches VALUES (?,?)", (fkey, now.isoformat()))
    return True


def _card_search(name_en, number, lang):
    q = f"{name_en} {number.split('/')[0]}" + lang_word(lang)
    first = name_en.split(" ")[0]
    return q, (lambda t: title_ok(t, [first], number=number, lang=lang))


def refresh_card_asks(card_key, name_en, number, lang, force=False, stale_hours=None, auctions=True):
    """UK asks + tracked listings (fixed price and, unless auctions=False, auctions), and real sold items if
    Marketplace Insights works. 2 Browse calls made side by side, or 1 with auctions=False."""
    q, check = _card_search(name_en, number, lang)
    if auctions:
        with ThreadPoolExecutor(2) as ex:
            fixed = ex.submit(refresh_asks, card_key, q, check, force=force, stale_hours=stale_hours)
            auc = ex.submit(refresh_asks, card_key, q, check, force=force, buying="AUCTION", stale_hours=stale_hours)
            did = fixed.result()
            auc.result()
    else:
        did = refresh_asks(card_key, q, check, force=force, stale_hours=stale_hours)
    if did:
        refresh_sold_insights(card_key, q, check)
    return did


# ---------- Real sold data: eBay Marketplace Insights (restricted API, needs eBay's approval) ----------

def insights_ok(now=None):
    """False for 7 days after eBay refused access (no wasted calls while not approved)."""
    d = db.setting("insights_denied_at")
    now = now or datetime.now(timezone.utc)
    return not d or datetime.fromisoformat(d) < now - timedelta(days=7)


def _denied(e):
    s = str(e).lower()
    return "403" in s or "invalid_scope" in s or "insufficient" in s or "access denied" in s


def refresh_sold_insights(card_key, query, check):
    """Sold UK listings (last 90 days) for one card -> sales (source 'insights'). Returns rows added, or None
    when access isn't granted. Field names per eBay's item_sales docs; unverified until access is approved."""
    if not insights_ok():
        return None
    now = datetime.now(timezone.utc).isoformat()
    try:
        items = ebay.sold_search(query)
    except ebay.EbayError as e:
        if _denied(e):
            db.set_setting("insights_denied_at", now)
            db.set_setting("insights_error", str(e)[:300])
            return None
        raise
    rows = []
    for it in items:
        b = ask_bucket(it) if it.get("title") and check(it["title"]) else None
        price = float((it.get("lastSoldPrice") or {}).get("value") or 0)
        if b and price:
            rows.append((f"mi:{it.get('itemId')}", card_key, b, it["title"], price, _ship(it),
                         (it.get("lastSoldDate") or now)[:10], it.get("itemWebUrl"), now, "insights",
                         int("BEST_OFFER" in (it.get("buyingOptions") or []))))
    db.many(SALE_INSERT, rows)
    db.set_setting("insights_denied_at", None)
    return len(rows)


def test_insights():
    """Settings button: retry Marketplace Insights now. Returns a message for the user."""
    db.set_setting("insights_denied_at", None)
    try:
        ebay.sold_search("charizard 199/165", limit=5)
        return True, "Marketplace Insights works: real UK sold prices will be used from now on."
    except ebay.EbayError as e:
        if _denied(e):
            db.set_setting("insights_denied_at", datetime.now(timezone.utc).isoformat())
            return False, ("Not approved yet: eBay refused access to Marketplace Insights. Apply in your eBay "
                           "developer account (Application Growth Check / API access request).")
        return False, str(e)[:300]


# ---------- Which cards to keep following (sold inference needs a daily snapshot per card) ----------

def follow(card_key, priority=1, reason="", name=None, number=None, now=None):
    """Upsert a followed card. Highest priority wins; last_seen moves to now. 3 = watched/draft/deal,
    2 = viewed, 1 = seen by the deal scanner or a sealed-EV hit card. Unseen for 30 days -> dropped."""
    lang, card_id = card_key.split(":", 1)
    now = (now or datetime.now(timezone.utc)).isoformat()
    db.run("""INSERT INTO followed (card_key, lang, card_id, name, number, priority, last_seen, reason)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO UPDATE SET
              reason=CASE WHEN excluded.priority >= priority THEN excluded.reason ELSE reason END,
              priority=MAX(priority, excluded.priority), last_seen=excluded.last_seen,
              name=COALESCE(excluded.name, name), number=COALESCE(excluded.number, number)""",
           (card_key, lang, card_id, name, number, priority, now, reason))


def calls_today():
    r = db.q("SELECT calls FROM api_log WHERE day=date('now')")
    return r[0]["calls"] if r else 0


def refresh_plan(now=None):
    """Followed cards whose asks are older than stale_hours: priority first, then most recently seen.
    Drops cards unseen for 30 days. The refresh loop stops at the daily Browse budget."""
    now = now or datetime.now(timezone.utc)
    db.run("DELETE FROM followed WHERE last_seen < ?", ((now - timedelta(days=30)).isoformat(),))
    stale = (now - timedelta(hours=db.setting("stale_hours"))).isoformat()
    return db.q("""SELECT f.* FROM followed f LEFT JOIN fetches x ON x.card_key='ask:'||f.card_key
                   WHERE x.fetched_at IS NULL OR x.fetched_at < ? ORDER BY f.priority DESC, f.last_seen DESC""", (stale,))


def budget_left():
    return int(db.setting("browse_budget")) - calls_today()


def refresh_followed(rows, stale_hours=None, max_calls=None):
    """Snapshot asks for each followed card, then check which of its tracked listings sold.
    Stops after max_calls Browse calls (this job's slice) or at the daily budget.
    Returns {status: count, 'cards': n, 'stopped': bool}."""
    start = calls_today()
    out = {"cards": 0, "stopped": False}
    for r in rows:
        if budget_left() <= 0 or (max_calls is not None and calls_today() - start >= max_calls):
            out["stopped"] = True
            break
        name, number = r["name"], r["number"]
        try:
            if not (name and number):
                c = catalog.card(r["lang"], r["card_id"])
                name, number = (catalog.english_name(r["lang"], c), catalog.printed_number(c)) if c else (None, None)
                if not (name and number):
                    continue
                db.run("UPDATE followed SET name=?, number=? WHERE card_key=?", (name, number, r["card_key"]))
            refresh_card_asks(r["card_key"], name, number, r["lang"], stale_hours=stale_hours,
                              auctions=(r["priority"] or 1) >= 2)   # scanner/EV cards: fixed price only, 1 call
            for k, v in infer_sales(r["card_key"]).items():
                out[k] = out.get(k, 0) + v
            out["cards"] += 1
        except Exception as e:   # one bad card must not stop the batch
            out["errors"] = out.get("errors", 0) + 1
            out["last_error"] = f"{r['card_key']}: {e}"[:200]
    return out


# ---------- Sold prices inferred from eBay's own end-of-listing signals (Browse getItem, free) ----------

def _past(ts, now):
    return bool(ts) and datetime.fromisoformat(ts.replace("Z", "+00:00")) <= now


def judge(row, item, now):
    """Pure. row: tracked row (dict-like); item: getItem JSON or None.
    -> (status, sale) where sale = (price, shipping, 'YYYY-MM-DD') or None.
    Auction: ended with >=1 bid = sold at final bid. Fixed price: ended/out of stock with >=1 sold = sold at list price.
    Ended with stock left (seller ended it) = 'ended', not a sale. Vanished from eBay = 'gone' (unknown)."""
    if item is None:
        return "gone", None
    end = item.get("itemEndDate")
    ended = _past(end, now)
    if row["buying"] == "AUCTION":
        if not ended:
            return "active", None
        if int(item.get("bidCount") or 0) >= 1:
            return "sold", (float(item["currentBidPrice"]["value"]), _ship(item), end[:10], False)
        return "ended", None
    av = (item.get("estimatedAvailabilities") or [{}])[0]
    qty = int(av.get("estimatedSoldQuantity") or 0)
    out = av.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" or av.get("estimatedRemainingQuantity") == 0
    if not (ended or out):
        return "active", None
    if qty >= 1 and (out or not av.get("estimatedAvailabilityStatus")):
        # Best Offer listings may have sold below list (accepted offers are hidden): flagged, see summarize()
        return "sold", (float(item["price"]["value"]), _ship(item), (end or now.isoformat())[:10],
                        "BEST_OFFER" in (item.get("buyingOptions") or []))
    return "ended", None


def judge_qty(prev_sold, item):
    """Pure. Multi-quantity listings sell without ending. -> (sold count now, units sold since the last look).
    prev_sold None = first look: baseline only (units sold before we started following aren't dated)."""
    av = (item.get("estimatedAvailabilities") or [{}])[0]
    sold = int(av.get("estimatedSoldQuantity") or 0)
    return sold, (0 if prev_sold is None else max(sold - prev_sold, 0))


def check_quantity_sales(card_key, max_checks=4):
    """getItem on the cheapest live listings of a sealed product; records units sold since the last look as sales
    at the current price (source 'ebay'). Returns units recorded."""
    rows = db.q("""SELECT * FROM tracked WHERE card_key=? AND status='active' AND buying='FIXED_PRICE'
                   AND substr(last_seen,1,10)=date('now') ORDER BY price+shipping LIMIT ?""", (card_key, max_checks))
    now, units_total = datetime.now(timezone.utc), 0
    for r, item in zip(rows, ebay.get_items([r["item_id"] for r in rows])):
        if not isinstance(item, dict):   # gone, or the call failed: looked at again next time
            continue
        sold, units = judge_qty(r["qty_seen"], item)
        bo = int("BEST_OFFER" in (item.get("buyingOptions") or []))
        db.many(SALE_INSERT, [(f"{r['item_id']}#{sold - i}", card_key, r["bucket"], r["title"],
                               float(item["price"]["value"]), _ship(item), now.date().isoformat(), r["url"],
                               now.isoformat(), "ebay", bo) for i in range(units)])
        db.run("UPDATE tracked SET qty_seen=? WHERE item_id=?", (sold, r["item_id"]))
        units_total += units
    return units_total


def infer_sales(card_key=None):
    """Check tracked listings that vanished from today's snapshot (fixed price) or whose auction has ended.
    Writes confirmed sales (source 'ebay'). Returns {status: count}."""
    now = datetime.now(timezone.utc)
    where, args = "", ()
    if card_key:
        where, args = " AND t.card_key=?", (card_key,)
    rows = db.q(f"""SELECT t.* FROM tracked t LEFT JOIN fetches f ON f.card_key='ask:'||t.card_key
        WHERE t.status='active' AND ((t.buying='AUCTION' AND t.end_at < ?) OR
              (t.buying='FIXED_PRICE' AND f.fetched_at IS NOT NULL AND substr(t.last_seen,1,10) < substr(f.fetched_at,1,10))){where}""",
                (now.isoformat(), *args))
    counts = {}
    for r, item in zip(rows, ebay.get_items([r["item_id"] for r in rows])):   # 6 listings at a time
        if isinstance(item, Exception):   # this call failed: the listing stays active and is checked next time
            counts["error"] = counts.get("error", 0) + 1
            continue
        status, sale = judge(r, item, now)
        if sale and r["buying"] == "FIXED_PRICE" and r["qty_seen"] is not None and item:
            sold_now, units = judge_qty(r["qty_seen"], item)
            if units <= 0:
                sale = None                   # every unit already recorded while the listing was live
        counts[status] = counts.get(status, 0) + 1
        if sale:
            db.run(SALE_INSERT, (r["item_id"], r["card_key"], r["bucket"], r["title"], sale[0], sale[1], sale[2],
                                 r["url"], now.isoformat(), "ebay", int(sale[3])))
        if status != "active":
            db.run("UPDATE tracked SET status=?, qty_sold=qty_sold+? WHERE item_id=?", (status, int(bool(sale)), r["item_id"]))
        elif r["buying"] == "FIXED_PRICE":
            # still live, just outside today's search results: seen now, so it isn't asked about again on every
            # page click (it was: ~10 calls and 2 s per click on a popular card), only after the next snapshot
            db.run("UPDATE tracked SET last_seen=? WHERE item_id=?", (now.isoformat(), r["item_id"]))
    return counts


def sync_own_sales():
    """Your own eBay orders for cards listed through this app -> sales (source 'own'). Returns rows added."""
    import json
    since = db.setting("own_synced_at") or (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    drafts = {f"PP-{r['id']}": json.loads(r["data_json"]) for r in db.q("SELECT id, data_json FROM drafts WHERE status='posted'")}
    rows, now = [], datetime.now(timezone.utc)
    for o in ebay.orders(since):
        if (o.get("cancelStatus") or {}).get("cancelState") == "CANCELED" or o.get("orderPaymentStatus") not in (None, "PAID"):
            continue
        for li in o.get("lineItems", []):
            d = drafts.get(li.get("sku"))
            if not d or not d.get("card_key"):
                continue
            b = f"{d['grader']} {d['grade']}" if d.get("graded") else ("Raw" if (d.get("condition") or "NM") == "NM" else "Raw played")
            ship = float(((li.get("deliveryCost") or {}).get("shippingCost") or {}).get("value", 0))
            if d.get("inventory_id"):
                from . import inventory
                inventory.mark_sold(d["inventory_id"], float(li["lineItemCost"]["value"]), o["creationDate"][:10])
            rows.append((li["lineItemId"], d["card_key"], b, li.get("title") or d.get("title"), float(li["lineItemCost"]["value"]),
                         ship, o["creationDate"][:10], ebay.listing_url(li.get("legacyItemId", "")), now.isoformat(), "own", 0))
    db.many(SALE_INSERT, rows)
    db.set_setting("own_synced_at", now.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    return len(rows)


# "2x", "x25", "2-pack", "twin pack": several products in one listing
MULTI = re.compile(r"\b\d+\s*[x×]\b|\b[x×]\s*\d+\b|\b(?:[2-9]|1\d)\s*-\s*pack\b|\b(?:two|twin|2)\s+pack\b", re.I)


def sealed_title_ok(title, inc, exc, lang=None):
    """Pure. A sealed product listing: required words present, no junk/excluded words, not a multiple; English
    products also reject other-language copies ('Indonesian Team Up Booster Box')."""
    return title_ok(title, inc, SEALED_JUNK + tuple(exc), lang="en" if lang == "en" else None) and not MULTI.search(title)


def refresh_sealed_asks(card_key, ebay_query, force=False, exclude=(), ref=None):
    """exclude: extra title phrases meaning another product (other sets/series), from ev.conflicts();
    ref: outside reference price (see refresh_asks)."""
    q, inc, exc = parse_query(ebay_query)
    lang = card_key.split(":")[1]
    return refresh_asks(card_key, q, lambda t: sealed_title_ok(t, inc, exc + list(exclude), lang),
                        fixed_bucket="Sealed", condition_ids=["1000"], category=None, force=force, ref=ref)


risky_seller = ebay.risky_seller


def rebucket_played(asks, track, played_ids):
    """Pure. Listings eBay's own 'Card Condition' says are played move to the 'Raw played' bucket (asks tuple
    index 3, tracked tuple index 2), whatever the title said."""
    fix = lambda row, i: row[:i] + ("Raw played",) + row[i + 1:] if row[0] in played_ids and row[i] == "Raw" else row
    return [fix(a, 3) for a in asks], [fix(t, 2) for t in track]


def remember_card(lang, c, name_en=None):
    """Cache name/set/image for a card so overview pages don't need catalog calls."""
    if not c:
        return
    db.run("INSERT OR REPLACE INTO cards_seen VALUES (?,?,?,?,?,?,?,?,?)",
           (f"{lang}:{c['id']}", lang, c["id"], name_en or catalog.english_name(lang, c) or c["name"],
            (c.get("set") or {}).get("name"), catalog.image(c, "low"), c.get("rarity"), catalog.printed_number(c),
            datetime.now(timezone.utc).isoformat()))


def drop_cheap(rows, total_idx, floor=0.4, ref=None):
    """Pure. Sealed listings priced under 40% of the median are accessories/parts/fakes, not the product.
    ref: the product's recent sold value, which also judges a lone listing (a £150 'vintage box' vs £900 sales)."""
    totals = [r[total_idx] for r in rows]
    if not ref and len(totals) < 3:
        return rows
    med = max(ref or 0, statistics.median(totals) if len(totals) >= 3 else 0)
    return [r for r in rows if r[total_idx] >= floor * med]


def movers(days=7, limit=5):
    """Pure-ish: biggest % moves in median Raw asking price between the oldest snapshot within `days` and the
    latest, per card. Returns (up, down) lists of (card_key, pct, latest_median, name)."""
    _ensure_rollup()
    since = (date.today() - timedelta(days=days)).isoformat()
    by = {}
    for r in db.q("SELECT card_key, seen_on, median FROM ask_daily WHERE bucket='Raw' AND seen_on>=? "
                  "AND card_key NOT LIKE 'sealed:%'", (since,)):
        by.setdefault(r["card_key"], {})[r["seen_on"]] = r["median"]
    names = {r["card_key"]: r["name"] for r in db.q("SELECT card_key, name FROM cards_seen")}
    out = []
    for k, days_ in by.items():
        if len(days_) < 2:
            continue
        a, b = days_[min(days_)], days_[max(days_)]
        if a > 0:
            out.append((k, round((b - a) / a * 100, 1), round(b, 2), names.get(k, k)))
    out.sort(key=lambda x: x[1])
    return [x for x in reversed(out) if x[1] > 0][:limit], [x for x in out if x[1] < 0][:limit]


def iqr_filter(totals):
    if len(totals) < 4:
        return totals
    q1, _, q3 = statistics.quantiles(totals, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [t for t in totals if lo <= t <= hi]


def ask_values(card_key):
    """{bucket: {market (median ask), low_q (25th percentile), low, n, date}} from the latest snapshot."""
    latest = db.q("SELECT MAX(seen_on) d FROM asks WHERE card_key=?", (card_key,))[0]["d"]
    if not latest:
        return {}
    by, lang, sealed = {}, card_key.split(":", 1)[0], card_key.startswith("sealed:")
    num = None if sealed else card_number(card_key)
    for r in db.q("SELECT bucket, total, title FROM asks WHERE card_key=? AND seen_on=?", (card_key, latest)):
        if sealed or not junk_title(r["title"], lang, num):
            by.setdefault(r["bucket"], []).append(r["total"])
    out = {}
    for b, totals in by.items():
        # under 40 % of the median = accessory/fake that slipped past the title filter, not a real copy
        med = statistics.median(totals)
        kept = iqr_filter([t for t in totals if t >= 0.4 * med] if len(totals) >= 3 else totals)
        low_q = statistics.quantiles(kept, n=4, method="inclusive")[0] if len(kept) >= 2 else kept[0]
        out[b] = {"market": round(statistics.median(kept), 2), "low_q": round(low_q, 2), "low": min(kept),
                  "n": len(totals), "date": latest, "low_confidence": len(totals) < 3, "kind": "asks"}
    return out


def ask_history(card_key):
    """[(date, bucket, median ask)] for charts."""
    _ensure_rollup()
    return [(r["seen_on"], r["bucket"], round(r["median"], 2)) for r in db.q(
        "SELECT seen_on, bucket, median FROM ask_daily WHERE card_key=? ORDER BY seen_on, bucket", (card_key,))]


# ---------- ask_daily: one row per card / day / condition, so history and movers never scan every listing ----------

def _daily_rows(rows):
    """Pure. rows: [(card_key, seen_on, bucket, total)] -> [(card_key, seen_on, bucket, median, n)], outliers dropped."""
    by = {}
    for k, d, b, t in rows:
        by.setdefault((k, d, b), []).append(t)
    return [(*key, statistics.median(iqr_filter(v)), len(v)) for key, v in by.items()]


def rollup_day(card_key, day):
    """(Re)write one card's ask_daily rows for a day from that day's listing snapshot."""
    db.many("INSERT OR REPLACE INTO ask_daily VALUES (?,?,?,?,?)", _daily_rows(
        [(card_key, day, r["bucket"], r["total"]) for r in db.q(
            "SELECT bucket, total FROM asks WHERE card_key=? AND seen_on=?", (card_key, day))]))


_rolled = False


def _ensure_rollup(force=False):
    """One-off per database: build ask_daily from the snapshots stored before it existed (a few seconds)."""
    global _rolled
    if _rolled and not force:
        return
    if force or not db.setting("ask_daily_built"):
        db.many("INSERT OR REPLACE INTO ask_daily VALUES (?,?,?,?,?)", _daily_rows(
            [tuple(r) for r in db.q("SELECT card_key, seen_on, bucket, total FROM asks")]))
        db.set_setting("ask_daily_built", True)
    _rolled = True


def tracking_stats():
    """(cards with UK price history, days of history) for the Card prices overview."""
    _ensure_rollup()
    r = db.q("SELECT COUNT(DISTINCT CASE WHEN card_key NOT LIKE 'sealed:%' THEN card_key END) c, "
             "COUNT(DISTINCT seen_on) d FROM ask_daily")[0]
    return r["c"], r["d"]


# ---------- Market value: eBay data only ----------

def played_ratio_from(rows, min_cards=10):
    """Pure. rows: [(card_key, bucket, total)] sales. Median over cards of median(Raw played)/median(Raw), using
    cards with >=3 sales of each. Returns (ratio, cards) or (None, cards) below min_cards."""
    by = {}
    for k, b, t in rows:
        by.setdefault(k, {}).setdefault(b, []).append(t)
    ratios = [statistics.median(v["Raw played"]) / statistics.median(v["Raw"]) for v in by.values()
              if len(v.get("Raw", [])) >= 3 and len(v.get("Raw played", [])) >= 3 and statistics.median(v["Raw"]) > 0]
    return (round(statistics.median(ratios), 2), len(ratios)) if len(ratios) >= min_cards else (None, len(ratios))


def condition_mult(condition):
    """(multiplier of the NM value, note). LP is learned from tracked sales once 10+ cards have both raw and played
    sales; MP/HP follow it (× 0.75 / × 0.5 of LP). Until then the Settings values."""
    if condition in (None, "NM"):
        return 1.0, ""
    lp, n = played_ratio_from([(r["card_key"], r["bucket"], r["t"]) for r in db.q(
        "SELECT card_key, bucket, price+shipping t FROM sales WHERE bucket IN ('Raw', 'Raw played')")])
    # ponytail: one aggregate query per call; cache it if sales grows past ~100k rows
    if lp:
        return round({"LP": lp, "MP": lp * 0.75, "HP": lp * 0.5}[condition], 2), f" learned from {n} cards"
    return float(db.setting(f"mult_{condition}")), ""


def solid(stats):
    """Enough eBay evidence to act on (deals): sold-based, manual, or a low-quartile ask over >=5 listings."""
    return bool(stats) and (stats.get("kind") in ("sold", "override") or (stats.get("kind") == "asks" and stats["n"] >= 5))


def searched_value(items, b, name_en, number, lang):
    """Pure. Median total (price + postage) of the search results that are this exact card in bucket `b`
    (graded/played/other-language/junk/risky-seller listings dropped, then accessories under 40 % of the median
    and outliers). Returns (median, n) or (None, 0)."""
    first = re.split(r"[\s&]+", name_en.strip())[0]   # 'Solgaleo&Lunala' -> 'Solgaleo' (titles write 'Solgaleo & Lunala')
    totals = []
    for it in items:
        if not (it.get("price") or {}).get("value") or not it.get("title"):
            continue
        t = round(float(it["price"]["value"]) + _ship(it), 2)
        if ask_bucket(it) == b and title_ok(it["title"], [first], number=number, lang=lang) and not risky_seller(it, t):
            totals.append(t)
    if not totals:
        return None, 0
    med = statistics.median(totals)
    kept = iqr_filter([t for t in totals if t >= 0.4 * med] if len(totals) >= 3 else totals)
    return round(statistics.median(kept), 2), len(kept)


# Sold price without postage ÷ median asking price incl. postage, measured 2026-11-17 over 113 cards with 3+ tracked
# raw sales (quartiles 0.62 / 0.71 / 0.81). Fallback for a price band with too few cards to learn its own ratio.
SOLD_PER_ASK = 0.72
ASK_BANDS = (10, 25, 60)   # £ asking median: under 10 / 10-25 / 25-60 / 60+ (2026-11-19: 0.64 / 0.75 / 0.69 / 0.75)


def _band(ask):
    return sum(ask >= b for b in ASK_BANDS)


def ratios_by_band(pairs, min_cards=10):
    """Pure. pairs: [(ask median, sold median ÷ ask median)] per card -> {band: median ratio} for bands with
    >= min_cards cards; other bands are left out (they use SOLD_PER_ASK)."""
    by = {}
    for ask, r in pairs:
        by.setdefault(_band(ask), []).append(r)
    return {b: round(statistics.median(v), 3) for b, v in by.items() if len(v) >= min_cards}


def learned_ratios():
    """{band: ratio} learned from cards with 3+ tracked raw sales and 5+ listings today; recomputed once a day."""
    today = date.today().isoformat()
    cached = db.setting("sold_per_ask_learned")
    if cached and cached.get("day") == today:
        return {int(k): v for k, v in cached["ratios"].items()}
    pairs = []
    for r in db.q("SELECT card_key FROM sales WHERE bucket='Raw' GROUP BY card_key HAVING COUNT(*) >= 3"):
        s, a = values(r["card_key"], postage=False).get("Raw"), ask_values(r["card_key"]).get("Raw")
        if s and s["n"] >= 3 and a and a["n"] >= 5 and a["market"] > 0:
            pairs.append((a["market"], s["market"] / a["market"]))
    ratios = ratios_by_band(pairs)
    db.set_setting("sold_per_ask_learned", {"day": today, "ratios": ratios})
    return ratios


def sold_per_ask(ask):
    """(ratio, note) for a card whose median asking price is `ask`."""
    r = learned_ratios().get(_band(ask))
    return (r, f"× {r} (learned for this price band)") if r else (SOLD_PER_ASK, f"× {SOLD_PER_ASK}")


def _legacy_id(item_id):
    """Pure. 'v1|123456789|0' (Browse API) -> '123456789' (the listing number on ebay.co.uk)."""
    parts = str(item_id).split("|")
    return parts[1] if len(parts) > 1 else parts[0]


def _wmedian(pairs):
    """Pure. Weighted median of [(value, weight)]."""
    pairs = sorted(pairs)
    half, acc = sum(w for _, w in pairs) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def blend(solds, asks, ratio, today=None, half_life=45, k_cap=1.5):
    """Pure. Market = what the card sells for, item price without postage, from SOLDS (weighted more: they sold)
    and today's LISTINGS (converted to a sold-equivalent). Design reviewed with Fable, 2026-11-22.
      solds: [(item price, 'YYYY-MM-DD', best_offer)]  asks: [listing totals incl. postage]  ratio: f(ask median)
    - each sale weighs 0.5^(age/half_life); a Best-Offer sale (accepted offer hidden) counts at 90 % with half weight
    - listings: asks outside 0.5-3x their median dropped; L = median x sold/ask ratio; weight min(1.5, m/4), capped
      because the ratio's per-card error (IQR 0.62-0.81) doesn't shrink with more listings
    - sold outliers: kept within median/b .. median*b, b = exp(3 * 1.4826 * MAD(ln price)) clamped to 1.6-2.5
      (centred on the sales alone: sales that agree are real even when listings ask far more);
      1-2 sales aren't dropped but weigh x0.25 when outside 0.5-2x L
    - market = (n_eff * S + k * L) / (n_eff + k), S = weighted median of the kept sales
    Returns dict(market, S, L, n_sold, n_eff, m, k, rejected, check=[reasons to look before posting]) or None."""
    import math
    today = today or date.today()
    age = lambda d: max(0, (today - date.fromisoformat(d[:10])).days)
    L = m = None
    if asks:
        med = statistics.median(asks)
        asks = [a for a in asks if 0.5 * med <= a <= 3 * med]
        m = len(asks)
        L = statistics.median(asks) * ratio(statistics.median(asks))
    pts = [(p * (0.9 if bo else 1.0), 0.5 ** (age(d) / half_life) * (0.5 if bo else 1.0), age(d))
           for p, d, bo in solds if p and p > 0 and age(d) <= 365]
    rejected = 0
    if len(pts) >= 3:
        xs = [p for p, _, _ in pts]
        centre = statistics.median(xs)   # sales that agree with each other are never thrown out because of asks
        lmed = statistics.median(math.log(x) for x in xs)
        mad = statistics.median(abs(math.log(x) - lmed) for x in xs)
        b = min(2.5, max(1.6, math.exp(3 * 1.4826 * mad)))
        kept = [t for t in pts if centre / b <= t[0] <= centre * b]
        rejected = len(pts) - len(kept)
        pts = kept
    elif pts and L:
        pts = [(p, w * (0.25 if not 0.5 * L <= p <= 2 * L else 1.0), a) for p, w, a in pts]
    if not pts and L is None:
        return None
    n_eff = sum(w for _, w, _ in pts)
    S = _wmedian([(p, w) for p, w, _ in pts]) if pts else None
    k = min(k_cap, m / 4) if L else 0.0
    market = ((n_eff * S if S else 0) + k * (L or 0)) / ((n_eff if S else 0) + k)
    check = []
    if n_eff + k < 1.5 and market >= 10:
        check.append("very little eBay data for this card")
    if S and L and n_eff >= 1 and m >= 3 and not 0.67 <= S / L <= 1.5:
        check.append(f"sold (£{S:.2f}) and listings (≈£{L:.2f}) disagree")
    if rejected and rejected > (len(pts) + rejected) / 3:
        check.append(f"{rejected} odd sales left out")
    if market >= 60 and sum(1 for _, _, a in pts if a <= 60) < 2:
        check.append("high value with under 2 sales in 60 days")
    return {"market": round(market, 2), "S": S and round(S, 2), "L": L and round(L, 2), "n_sold": len(pts),
            "n_eff": round(n_eff, 2), "m": m or 0, "k": round(k, 2), "rejected": rejected, "check": check}


def blended_market(card_key, grader=None, grade=None, condition="NM"):
    """(value, stats, basis) from stored eBay data: blend() of this bucket's sales (item price) and today's tracked
    listings. Graded buckets never mix with raw; played raw = the Raw blend x condition multiplier. Listings of
    your own (posted from this app) are left out so your price never feeds itself. (None, None, None) if no data."""
    b, mult, tag = _bucket_mult(grader, grade, condition)
    lang, num = card_key.split(":", 1)[0], card_number(card_key)
    solds = [(r["price"], r["sold_at"], r["bo"]) for r in db.q(
        "SELECT price, sold_at, COALESCE(best_offer, 0) bo, title, source FROM sales WHERE card_key=? AND bucket=?",
        (card_key, b)) if r["source"] == "own" or not junk_title(r["title"], lang, num)]
    mine = {str(r["listing_id"]) for r in db.q("SELECT listing_id FROM drafts WHERE listing_id IS NOT NULL")}
    latest = db.q("SELECT MAX(seen_on) d FROM asks WHERE card_key=? AND bucket=?", (card_key, b))[0]["d"]
    asks = [r["total"] for r in db.q("SELECT item_id, total, title FROM asks WHERE card_key=? AND bucket=? AND seen_on=?",
                                     (card_key, b, latest))
            if not junk_title(r["title"], lang, num) and _legacy_id(r["item_id"]) not in mine] if latest else []
    # a market moving fast (listing median >20 % off a month ago): recent sales matter more
    hist = db.q("SELECT median FROM ask_daily WHERE card_key=? AND bucket=? AND seen_on <= date('now', '-28 day') "
                "ORDER BY seen_on DESC LIMIT 1", (card_key, b))
    moving = bool(hist and asks and abs(statistics.median(asks) / hist[0]["median"] - 1) > 0.2)
    r = blend(solds, asks, lambda a: sold_per_ask(a)[0], half_life=14 if moving else 45)
    if not r:
        return None, None, None
    parts = ([f"{r['n_sold']} sold (weight {r['n_eff']})"] if r["n_sold"] else []) + \
            ([f"{r['m']} listings × {sold_per_ask(statistics.median(asks))[0]} (weight {r['k']})"] if r["m"] else [])
    stats = {**r, "kind": "blend", "n": r["n_sold"] + r["m"], "low_confidence": bool(r["check"])}
    return round(r["market"] * mult, 2), stats, f"{b}{tag} · eBay blend: " + " + ".join(parts) + \
        (" · fast-moving market: recent sales weighted more" if moving else "")


def _bucket_mult(grader, grade, condition):
    b = f"{grader.upper()} {grade}" if grader else "Raw"
    mult, learned = (1.0, "") if grader else condition_mult(condition)
    return b, mult, (f" × {mult}{learned}" if mult != 1 else "")


def sold_item_market(card_key, grader=None, grade=None, condition="NM"):
    """Market as pokepulse quotes it: median eBay sold price WITHOUT postage (>=3 sales, summarize's rules).
    Raw LP/MP/HP = the Raw median × condition multiplier. (value, stats, basis) or (None, None, None)."""
    b, mult, tag = _bucket_mult(grader, grade, condition)
    s = values(card_key, postage=False).get(b)
    if not s or s["n"] < 3:
        return None, None, None
    return round(s["market"] * mult, 2), s, f"{b}{tag} · eBay sold ×{s['n']} ({s['window']}), median without postage"


def item_market(card_key, grader=None, grade=None, condition="NM"):
    """Stored data only (no eBay call): sold median without postage, else today's tracked asks × SOLD_PER_ASK."""
    v = sold_item_market(card_key, grader, grade, condition)
    if v[0] is not None:
        return v
    b, mult, tag = _bucket_mult(grader, grade, condition)
    a = ask_values(card_key).get(b)
    if not a:
        return None, None, None
    r, note = sold_per_ask(a["market"])
    return round(a["market"] * r * mult, 2), {**a, "kind": "estimate"}, \
        f"{b}{tag} · estimate: {a['n']} listings' median {note} (no solds yet)"


def searched_market(name_en, number, lang, grader=None, grade=None, condition="NM"):
    """Estimated sold price (without postage) from one live eBay UK search for '<name> <number>' (e.g.
    'Charizard ex 183/165'): the median fixed-price listing in the same condition × SOLD_PER_ASK.
    Raw LP/MP/HP = × condition multiplier. (value, stats, basis), or (None, None, None) when nothing matches."""
    if not (name_en or "").strip() or not (number or "").strip():
        return None, None, None   # unread card: nothing sensible to search for
    if grader and not grade:
        return None, None, None   # graded but grade unknown: no bucket to price from
    q = re.sub(r"\s*&\s*", " & ", f"{name_en.strip()} {number.strip()}") + lang_word(lang)
    b, mult, tag = _bucket_mult(grader, grade, condition)
    value, n = searched_value(ebay.search_active(q), b, name_en, number, lang)
    if value is None:
        return None, None, None
    r, note = sold_per_ask(value)
    return round(value * r * mult, 2), {"kind": "search", "n": n, "low_confidence": n < 3}, \
        f"{b}{tag} · estimate: eBay search '{q}' median of {n} {note} (no solds yet)"


def market_for(card_key, grader=None, grade=None, condition="NM", card=None, bucket=None):
    """Returns (value, stats, basis) from eBay data only (`card` is unused; Cardmarket no longer prices anything).
    manual override > sold (>=3 sales, last 90 days, Best Offer excluded when possible) > low quartile of today's
    UK asks (>=3 listings: the cheaper asks are the ones that sell) > thin (1-2 sales or asks) > None.
    stats['kind'] = override / sold / asks / thin. Raw LP/MP/HP = NM value × condition multiplier.
    bucket: explicit bucket with no condition multiplier (sealed products use "Sealed")."""
    b = bucket or (f"{grader.upper()} {grade}" if grader else "Raw")
    mult, learned = (1.0, "") if grader or bucket else condition_mult(condition)
    tag = f" × {mult}{learned}" if mult != 1 else ""
    v = lambda x: round(x * mult, 2)
    o = db.setting(f"override:{card_key}:{b}")
    if o and o.get("value"):
        return v(o["value"]), {"kind": "override", "n": None}, f"{b}{tag} · manual override"
    sold, ask = values(card_key).get(b), ask_values(card_key).get(b)
    if sold and sold["n"] >= 3:
        extra = ", incl. Best Offer" if sold["incl_best_offer"] else ""
        return v(sold["market"]), sold, f"{b}{tag} · sold ×{sold['n']} ({sold['window']}, {'/'.join(sold['sources'])}{extra})"
    if ask and ask["n"] >= 3:
        return v(ask["low_q"]), ask, f"{b}{tag} · asks ×{ask['n']} (low quartile)"
    if sold:
        return v(sold["market"]), {**sold, "kind": "thin"}, f"{b}{tag} · thin: {sold['n']} sale{'s' * (sold['n'] > 1)}"
    if ask:
        return v(ask["market"]), {**ask, "kind": "thin"}, f"{b}{tag} · thin: {ask['n']} listing{'s' * (ask['n'] > 1)}"
    return None, None, b
