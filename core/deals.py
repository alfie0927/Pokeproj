"""Deal finder: newest UK eBay listings -> identify the card -> flag the ones priced well under market.
Free: Browse API only (no scraping). Title first ('Charizard ex 199/165'); OCR on the listing photo as a fallback."""
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import catalog, db, ebay, prices, vision

QUERIES = ("pokemon",)   # ponytail: one broad query, newest first, paged; add per-set queries if cards slip through
BULK = ("Common", "Uncommon")   # rarities never worth sniping
WORD = re.compile(r"[a-z0-9']+")
JA = re.compile(r"\bjapanese\b|\bjpn\b|\bjap\b", re.I)


def flip(total, market, postage_out=None, fee_pct=None):
    """Pure-ish. (profit £, ROI) if you buy at `total` (delivered) and resell at `market`: minus your selling fee %
    and what posting it costs you."""
    postage_out = float(db.setting("postage_cost")) if postage_out is None else postage_out
    fee_pct = float(db.setting("sell_fee_pct")) if fee_pct is None else fee_pct
    profit = round(market * (1 - fee_pct / 100) - postage_out - total, 2)
    return profit, (round(profit / total, 2) if total else None)


def max_bid(market, threshold, postage):
    """Pure. Highest bid that still lands the auction at or under `threshold` × market, delivered."""
    return max(round(threshold * market - postage, 2), 0.0)


def notify(title, body, url=None):
    """Beep + Windows notification (click opens the listing). Silent no-op off Windows or when it fails."""
    import base64
    import subprocess
    from xml.sax.saxutils import escape
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        launch = f' activationType="protocol" launch="{escape(url, {chr(34): "&quot;"})}"' if url else ""
        xml = (f'<toast{launch}><visual><binding template="ToastGeneric"><text>{escape(title)}</text>'
               f'<text>{escape(body)}</text></binding></visual></toast>')
        ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
              " | Out-Null; [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime]"
              " | Out-Null; $x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('" + xml.replace("'", "''")
              + "'); $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
              " [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
              "[Windows.UI.Notifications.ToastNotification]::new($x))")
        subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand",
                          base64.b64encode(ps.encode("utf-16-le")).decode()], creationflags=0x08000000)
    except Exception:
        pass


def is_modern(set_id):
    """Pure. XY onwards (2014+) = modern, incl. Japanese 'S12a'/'SV2a'; everything older (WotC, EX, DP, BW) = vintage."""
    return bool(catalog.era_of(set_id or "") or re.match(r"^s\d", set_id or "", re.I))


def threshold_for(set_id):
    """Fraction of market at or under which a listing counts as a deal, by era."""
    return float(db.setting("deal_pct_modern" if is_modern(set_id) else "deal_pct_vintage")) / 100


def _norm(s):
    return " " + " ".join(WORD.findall(s.lower())) + " "


def title_candidates(title, briefs):
    """Pure. Brief catalog cards (all sharing the local number) whose name appears in the title; full-name
    matches win over first-word matches ('Pikachu' for 'Pikachu V' when the title drops the V)."""
    t = _norm(title)
    full = [b for b in briefs if _norm(b["name"]) in t]
    if full:   # 'Pikachu' also matches a 'Pikachu VMAX' title: the longest name present wins
        longest = max(len(_norm(b["name"])) for b in full)
        return [b for b in full if len(_norm(b["name"])) == longest]
    return [b for b in briefs if _norm(b["name"].split(" ")[0]) in t]


def condition_of(item):
    """(grader, grade, condition) from eBay's condition + title, or None when ambiguous."""
    b = prices.ask_bucket(item)
    if not b:
        return None
    if b == "Raw":
        return None, None, "NM"
    if b == "Raw played":
        return None, None, "LP"
    grader, grade = b.split(" ", 1)
    return grader, grade, None


def identify_title(title):
    """Card from a listing title: needs a 'n/total' number and the card's name. None if unsure."""
    m = vision.NUMBER.search(title.upper())
    if not m or prices.junk_title(title, "ja" if JA.search(title) else "en"):   # merch, fakes, foreign copies
        return None
    lang = "ja" if JA.search(title) else "en"
    local, total = m[1].replace(" ", ""), int(m[2])
    digits = re.sub(r"\D", "", local)   # 'EX199' from 'Charizard ex 199/165'; TG/GG numbers stay as read
    tries = [local] if not local.isdigit() else []
    tries += [digits.zfill(3), digits.lstrip("0") or "0"]
    briefs = list({b["id"]: b for t in dict.fromkeys(tries) for b in catalog.search(lang, "", t)}.values())
    cands = title_candidates(title, briefs)
    cards = [c for c in catalog._cards(lang, cands, limit=12)
             if ((c.get("set") or {}).get("cardCount") or {}).get("official") == total]
    if not cards:
        return None
    return max(cards, key=catalog.release), lang   # same name/number/count in several sets (rare): newest


def identify_photo(image_url):
    """OCR the listing's main photo (quick mode: 2-5 s). None unless OCR resolves exactly one card."""
    if not image_url:
        return None
    url = image_url.replace("s-l225", "s-l1600")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(ebay._http.get(url, timeout=30).content)
    try:
        r = vision._ocr(f.name, None, quick=True)
        return (catalog.card(r.language, r.candidate_ids[0]), r.language) if len(r.candidate_ids) == 1 else None
    finally:
        os.unlink(f.name)


def judge_item(item, card, lang, cond, refresh_ok):
    """(key, name, total, market, basis, ratio, stats) for one listing against the identified card.
    The card is followed from now on, so its UK listings are tracked and its sold prices build up daily.
    refresh_ok: bool, or a callable asked only when a first-time market lookup is actually needed."""
    key = f"{lang}:{card['id']}"
    name_en = catalog.english_name(lang, card) or card["name"]
    number = catalog.printed_number(card)
    prices.remember_card(lang, card, name_en)
    prices.follow(key, 1, "seen by deal scanner", name_en, number)
    grader, grade, condition = cond
    val, stats, basis = prices.market_for(key, grader, grade, condition)
    if not prices.solid(stats) and (refresh_ok() if callable(refresh_ok) else refresh_ok):
        # no/thin data: snapshot today's UK fixed-price asks (1 Browse call, cached 24 h per card)
        prices.refresh_card_asks(key, name_en, number, lang, auctions=False)
        val, stats, basis = prices.market_for(key, grader, grade, condition)
    total = ebay._total(item)
    return key, name_en, total, val, basis, (total / val if val else None), stats


COND_LABEL = {"NM": "near mint", "LP": "lightly played", "MP": "moderately played", "HP": "heavily played"}


def cond_text(cond):
    """Pure. ('PSA', '10', None) -> 'PSA 10'; (None, None, 'LP') -> 'LP'."""
    return f"{cond[0]} {cond[1]}" if cond and cond[0] else (cond[2] if cond else None)


def photo_check(full, declared):
    """A raw card's own photos decide: (reason to ignore the listing or None, condition, notes). Free checks first
    (a back photo must exist and not sit on a white background); then, with ANTHROPIC_API_KEY set, Claude grades the
    corners / edges / surface from the photos and a grade below the declared one wins. Photo problems never hide a
    listing by accident: download or grading errors fall back to the declared condition."""
    ims = []
    for u in vision.photo_urls(full):
        try:
            ims.append(vision.fetch_photo(u))
        except Exception:
            pass
    if not ims:
        return None, declared, []
    backs, whites = [vision.is_card_back(i) for i in ims], [vision.on_white(i) for i in ims]
    grade = None
    if os.environ.get("ANTHROPIC_API_KEY") and vision.photo_verdict(backs, whites, None, declared)[0] is None:
        try:
            grade = vision.grade_photos(ims)
        except Exception as e:
            db.set_setting("last_grade_error", f"{full.get('itemId')}: {e}"[:300])
    return vision.photo_verdict(backs, whites, grade, declared)


def verify(item, key, cond, val, mstats, basis):
    """One getItem on a listing that already looks like a deal (a few per pass, not every listing): eBay's structured
    condition, which sellers fill in far more often than the title, then the listing's photos (photo_check), plus the
    full description for the scam checks. A played card priced against its real condition is no longer a 'deal'.
    Returns (full item or None when gone, cond, val, mstats, basis, note, reason to ignore or None, photo notes)."""
    full = ebay.get_item(item["itemId"])
    if not full:
        return None, cond, val, mstats, basis, "gone", None, []
    c, new, note, drop, notes = ebay.condition_from_item(full), cond, "", None, []
    if c and c["graded"] and c["grader"] and c["grade"]:
        new = (c["grader"], c["grade"], None)
    elif c and not c["graded"]:
        new = (None, None, c["condition"])
    if new != cond:
        note = (cond_text(new) if new[0] else COND_LABEL[new[2]]) + " by eBay's condition" \
            + (" (description admits damage)" if c.get("downgraded") else "")
    if not new[0]:   # raw card: front + back photos, not on white, and the photo grade when it is worse
        drop, pcond, notes = photo_check(full, new[2])
        if pcond != new[2]:
            new, note = (None, None, pcond), f"{COND_LABEL[pcond]} by the photos"
    if new != cond:
        val, mstats, basis = prices.market_for(key, *new)
    return full, new, val, mstats, basis, note, drop, notes


def _identify(item):
    try:
        return identify_title(item["title"])
    except Exception as e:   # one bad title must not stop the scan
        return e


def allowance(n):
    """Thread-safe countdown: the returned take() is True for the first n calls only."""
    lock, left = threading.Lock(), [n]

    def take():
        with lock:
            left[0] -= 1
            return left[0] >= 0
    return take


def by_card(hits):
    """Pure. [(item, card, lang, cond)] -> one list per card, first-seen order. One worker takes all of a card's
    listings, so its market value is looked up once however many listings arrived."""
    groups = {}
    for h in hits:
        groups.setdefault(f"{h[2]}:{h[1]['id']}", []).append(h)
    return list(groups.values())


def judge_all(hits, refresh_ok):
    """Value listings 6 cards at a time (the wait is eBay, not this PC).
    Yields (item, card, lang, cond, judge_item's tuple or the Exception it raised)."""
    def group(g):
        out = []
        for it, card, lang, cond in g:
            try:
                out.append((it, card, lang, cond, judge_item(it, card, lang, cond, refresh_ok)))
            except Exception as e:   # one bad listing must not stop the scan
                out.append((it, card, lang, cond, e))
        return out
    with ThreadPoolExecutor(6) as ex:
        for res in ex.map(group, by_card(hits)):
            yield from res


def newest_unseen(seen, max_items=2000):
    """Newest UK listings not checked yet, paging 200 at a time until a page is mostly ones already seen."""
    new, ids = [], set(seen)
    for q in QUERIES:
        for offset in range(0, max_items, 200):
            page = ebay.search_active(q, sort="newlyListed", offset=offset)
            fresh = [i for i in page if i["itemId"] not in ids]
            new += fresh
            ids.update(i["itemId"] for i in fresh)
            if len(page) < 200 or len(fresh) < len(page) // 2:   # caught up with the last scan
                break
    return new


def scan(ocr=True, ocr_budget=8, refresh_budget=None, progress=None, max_items=2000, photo_seconds=35):
    """One pass over every UK listing posted since the last scan (up to max_items). Returns stats; deals land in
    the `deals` table. A deal = rare or better, total <= era threshold (modern 70 % / vintage 50 % of market by
    default), market >= deal_min_market, and a solid eBay value behind it (sold-based, or low ask over >=5)."""
    min_market = float(db.setting("deal_min_market"))
    now = datetime.now(timezone.utc)
    seen = {r["item_id"] for r in db.q("SELECT item_id FROM deal_seen")}
    new = newest_unseen(seen, max_items)
    if refresh_budget is None:   # first-time market lookups (1 Browse call each, cached 24 h per card)
        refresh_budget = 25 + len(new) // 3
    if prices.budget_left() < 1000:   # keep the rest of today's calls for scanning itself
        refresh_budget = 0
    stats = {"new": len(new), "identified": 0, "deals": 0, "at": now.isoformat(timespec="seconds")}
    take, done = allowance(refresh_budget), [0]

    def mark(it, result):
        db.run("INSERT OR REPLACE INTO deal_seen VALUES (?,?,?)", (it["itemId"], stats["at"], result))
        done[0] += 1
        if progress:
            progress(done[0] / len(new), it["title"][:60])

    def record(it, card, lang, cond, judged):
        """Save the listing as a deal if it is one. Returns the line shown for it in deal_seen."""
        stats["identified"] += 1
        if isinstance(judged, Exception):
            return f"error: {judged}"[:120]
        key, name_en, total, val, basis, ratio, mstats = judged
        s = card.get("set") or {}
        th = threshold_for(s.get("id"))
        is_deal = lambda: bool(ratio) and ratio <= th and val >= min_market and prices.solid(mstats)
        if not is_deal():
            return f"{name_en}: {ratio:.0%} of market ({basis.split('· ')[-1]})" if ratio else f"{name_en}: no market value"
        # looks like a deal from the title alone: now one getItem for the declared condition and the scam checks
        full, cond, val, mstats, basis, note, drop, notes = verify(it, key, cond, val, mstats, basis)
        if full is None:
            return f"{name_en}: gone before it could be checked"
        ratio = total / val if val else None
        if not is_deal():
            return f"{name_en}: {note or 'condition'}, " + (f"{ratio:.0%} of market" if ratio else "no market value")
        if drop:
            return f"{name_en}: {ratio:.0%} of market but {drop} (not shown)"
        hard, soft = ebay.listing_risk(full, total, val)
        if hard:
            return f"{name_en}: {ratio:.0%} of market but {hard[0]} (not shown)"
        profit, roi = flip(total, val)
        prices.follow(key, 3, "deal")
        if db.setting("deal_alert") and profit >= float(db.setting("deal_min_profit")):
            notify(f"Deal: {name_en} £{total:.2f}", f"Market £{val:.2f} · profit £{profit:.2f} · "
                   f"{ratio:.0%} of market", it.get("itemWebUrl"))
        stats["deals"] += 1
        loc = it.get("itemLocation") or {}
        db.run("INSERT OR REPLACE INTO deals (item_id, found_at, card_key, name, set_name, number, bucket, "
               "title, total, market, basis, ratio, image, url, status, era, listed_at, location, rarity, "
               "profit, roi, condition, risk, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?,?,?,?,?)",
               (it["itemId"], stats["at"], key, name_en, s.get("name"), catalog.printed_number(card),
                prices.ask_bucket(it), it["title"], total, val, basis.split("· ")[-1], ratio,
                (it.get("image") or {}).get("imageUrl"), it.get("itemWebUrl"),
                "modern" if is_modern(s.get("id")) else "vintage", it.get("itemCreationDate"),
                " ".join(x for x in (loc.get("city"), loc.get("postalCode"), loc.get("country")) if x),
                card.get("rarity"), profit, roi, cond_text(cond), "; ".join(soft) or None, "; ".join(notes) or None))
        return f"{name_en}: {ratio:.0%} of market ({basis.split('· ')[-1]})" + (f" · {note}" if note else "")

    def skipped(card):
        return f"{card['name']}: {card['rarity'].lower()}, skipped"

    # 1. every title, 4 at a time (catalogue lookups; each fetches up to 4 cards at once itself)
    with ThreadPoolExecutor(4) as ex:
        hits = list(ex.map(_identify, new))
    todo, photos = [], []
    for it, hit in zip(new, hits):
        cond = condition_of(it)
        if isinstance(hit, Exception):
            mark(it, f"error: {hit}"[:120])
        elif not hit and ocr and cond and len(photos) < ocr_budget:
            photos.append((it, cond))
        elif not hit:
            mark(it, "no card number")
        elif not cond:
            mark(it, "condition unclear")
        elif hit[0].get("rarity") in BULK:
            mark(it, skipped(hit[0]))
        else:
            todo.append((it, hit[0], hit[1], cond))
    # 2. value the rare+ ones side by side: title-identified deals land within seconds of the pass starting
    for it, card, lang, cond, judged in judge_all(todo, take):
        try:
            mark(it, record(it, card, lang, cond, judged))
        except Exception as e:   # one bad listing must not stop the scan
            mark(it, f"error: {e}"[:120])
    # 3. photos last (2-5 s each), so they never hold up the deals above; capped so the next pass starts on time
    t_photos = time.time()
    for it, cond in photos:
        if time.time() - t_photos > photo_seconds:
            mark(it, "no card number")
            continue
        result = "ocr: no match"
        try:
            hit = identify_photo((it.get("image") or {}).get("imageUrl"))
            if hit and hit[0].get("rarity") in BULK:
                result = skipped(hit[0])
            elif hit:
                try:
                    judged = judge_item(it, hit[0], hit[1], cond, take)
                except Exception as e:
                    judged = e
                result = record(it, hit[0], hit[1], cond, judged)
        except Exception as e:
            result = f"error: {e}"[:120]
        mark(it, result)
    db.run("DELETE FROM deal_seen WHERE seen_at < ?", ((now - timedelta(days=7)).isoformat(),))
    db.set_setting("last_deal_scan", stats)
    return stats


_worker = None


def pace(budget_left, interval=60):
    """Pure. (scan now?, seconds between pass starts) from today's remaining Browse calls: every `interval` s with
    room to spare, every 3 min when under 1,000 are left, and no scanning at all once the allowance is gone
    (eBay refuses calls past its daily limit; it resets overnight)."""
    return budget_left > 0, (interval if budget_left > 1000 else 180)


def start_background(interval=60):
    """Scan continuously (a pass starts every `interval` s) in a daemon thread while the app is running; one thread
    per process. Pauses when the `deal_auto` setting is off. Errors are kept in `last_deal_error`, never raised."""
    # ponytail: in-process thread, stops with the server; run scan() from Task Scheduler for 24/7 without the app
    global _worker
    if _worker and _worker.is_alive():
        return _worker

    def loop():
        while True:
            t0, every = time.time(), interval
            try:
                go, every = pace(prices.budget_left(), interval)
                if db.setting("deal_auto") and go:
                    scan(ocr=bool(db.setting("deal_ocr")))
                    db.set_setting("last_deal_error", "")
                elif not go:
                    db.set_setting("last_deal_error", "paused: today's eBay call allowance is used up (resumes tomorrow)")
            except Exception as e:
                db.set_setting("last_deal_error", f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {e}"[:300])
            time.sleep(max(10, every - (time.time() - t0)))

    def slow_loop():   # own thread: auction passes and the 6-hourly re-check must never hold up new-listing scans
        while True:
            if db.setting("deal_auto"):
                try:
                    scan_auctions()
                except Exception as e:
                    db.set_setting("last_auction_error", str(e)[:300])
            try:
                sweep()
            except Exception as e:
                db.set_setting("last_sweep_error", str(e)[:300])
            time.sleep(600)

    threading.Thread(target=slow_loop, daemon=True, name="auction-scanner").start()
    _worker = threading.Thread(target=loop, daemon=True, name="deal-scanner")
    _worker.start()
    return _worker


def scan_auctions(pages=2):
    """UK auctions ending within `auction_window_hours` whose current bid + postage is at or under the era
    threshold × market (same identification and value rules as deals). Upserts `auctions`; drops ended ones.
    Returns {checked, kept}."""
    now = datetime.now(timezone.utc)
    window = now + timedelta(hours=float(db.setting("auction_window_hours")))
    min_market = float(db.setting("deal_min_market"))
    db.run("DELETE FROM auctions WHERE end_at < ?", (now.isoformat(),))
    db.set_setting("last_auction_error", "")   # only this pass's problems are shown, not one from days ago
    items = []
    for p in range(pages):
        page = ebay.search_active("pokemon", sort="endingSoonest", buying="AUCTION", offset=200 * p)
        items += page
        if len(page) < 200 or (page and _end(page[-1]) > window):
            break
    items = [it for it in items if _end(it) and _end(it) <= window]
    out = {"checked": len(items), "kept": 0}
    with ThreadPoolExecutor(4) as ex:   # same parallel identify + value steps as scan()
        hits = list(ex.map(_identify, items))
    todo = []
    for it, hit in zip(items, hits):
        cond = condition_of(it)
        if isinstance(hit, tuple) and cond and hit[0].get("rarity") not in BULK:
            bid = float((it.get("currentBidPrice") or it.get("price") or {}).get("value") or 0)
            todo.append(({**it, "price": {"value": bid}}, hit[0], hit[1], cond))   # auctions carry no 'price'
    for it, card, lang, cond, judged in judge_all(todo, lambda: prices.budget_left() > 1000):
        try:
            if isinstance(judged, Exception):
                raise judged
            key, name_en, _, val, basis, _, mstats = judged
            bid, end = float(it["price"]["value"]), _end(it)
            post = prices._ship(it)
            s = card.get("set") or {}
            th = threshold_for(s.get("id"))
            under = lambda: val and val >= min_market and prices.solid(mstats) and bid + post <= th * val
            known = db.q("SELECT condition, risk, notes FROM auctions WHERE item_id=?", (it["itemId"],))
            risk, notes = (known[0]["risk"], known[0]["notes"]) if known else (None, None)
            if under() and not known:   # first time under the line: one getItem for the condition, photos + scam checks
                full, cond, val, mstats, basis, _, drop, pnotes = verify(it, key, cond, val, mstats, basis)
                hard, soft = ebay.listing_risk(full, bid + post, val) if full else (["gone"], [])
                risk, notes = "; ".join(soft) or None, "; ".join(pnotes) or None
                if hard or drop:
                    continue
            elif known:
                cond = known[0]["condition"]
            if not under():
                db.run("DELETE FROM auctions WHERE item_id=?", (it["itemId"],))   # bid has gone past the line
                continue
            loc = it.get("itemLocation") or {}
            db.run("""INSERT INTO auctions (item_id, found_at, card_key, name, set_name, number, bucket, rarity, era,
                      title, bid, bids, postage, market, basis, ratio, max_bid, image, url, end_at, location,
                      condition, risk, notes)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                      bid=excluded.bid, bids=excluded.bids, market=excluded.market, basis=excluded.basis,
                      ratio=excluded.ratio, max_bid=excluded.max_bid""",
                   (it["itemId"], now.isoformat(timespec="seconds"), key, name_en, s.get("name"),
                    catalog.printed_number(card), prices.ask_bucket(it), card.get("rarity"),
                    "modern" if is_modern(s.get("id")) else "vintage", it["title"], bid, int(it.get("bidCount") or 0),
                    post, val, basis.split("· ")[-1], (bid + post) / val, max_bid(val, th, post),
                    (it.get("image") or {}).get("imageUrl"), it.get("itemWebUrl"), end.isoformat(),
                    " ".join(x for x in (loc.get("city"), loc.get("postalCode")) if x),
                    cond if isinstance(cond, str) else cond_text(cond), risk, notes))
            out["kept"] += 1
        except Exception as e:   # one bad listing must not stop the scan
            db.set_setting("last_auction_error", f"{it.get('itemId')}: {e}"[:300])
    db.set_setting("last_auction_scan", {**out, "at": now.isoformat(timespec="seconds")})
    return out


def _end(it):
    e = it.get("itemEndDate")
    return datetime.fromisoformat(e.replace("Z", "+00:00")) if e else None


def auctions_feed():
    return db.q("SELECT * FROM auctions WHERE status='new' AND end_at > ? ORDER BY end_at",
                (datetime.now(timezone.utc).isoformat(),))


def sweep(every_hours=6):
    """Every 6 h: watched / draft / deal cards (priority 3) get fresh asks and a sold check, so their sales are
    seen within hours rather than at the next daily refresh. Budget-capped like the daily job."""
    last = db.setting("last_sweep")
    now = datetime.now(timezone.utc)
    if last and datetime.fromisoformat(last) > now - timedelta(hours=every_hours):
        return None
    db.set_setting("last_sweep", now.isoformat())
    rows = db.q("SELECT * FROM followed WHERE priority >= 3 ORDER BY last_seen DESC")
    return prices.refresh_followed(rows, stale_hours=every_hours,
                                   max_calls=int(0.1 * int(db.setting("browse_budget"))))


def feed(hours=48):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return db.q("SELECT * FROM deals WHERE status='new' AND found_at >= ? ORDER BY found_at DESC, ratio", (since,))
