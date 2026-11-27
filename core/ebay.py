"""eBay REST: OAuth, Browse (active listings), Media (photos), Inventory/Offer (posting), Account (policies)."""
import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import db


def session():
    """Keep-alive connections shared by every call (a new TLS handshake per call cost 0.3-0.5 s; TCGdex went from
    0.45 s to 0.02 s a call). GETs are retried twice when a pooled connection has gone stale; POST/PUT (posting,
    price changes) are never retried once sent."""
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_maxsize=16, max_retries=Retry(
        total=2, backoff_factor=0.3, allowed_methods=frozenset({"GET"}), status_forcelist=())))
    return s


_http = session()

MARKET = "EBAY_GB"
CATEGORY = "183454"  # CCG Individual Cards
SELL_SCOPES = " ".join("https://api.ebay.com/oauth/api_scope/" + s
                       for s in ("sell.inventory", "sell.account", "sell.fulfillment"))

# eBay trading-card condition descriptor IDs (developer.ebay.com mip-enum-condition-descriptor-ids-for-trading-cards)
GRADER_IDS = {"PSA": "275010", "BGS": "275013", "CGC": "275015", "SGC": "275016", "TAG": "2750115",
              "ACE": "2750119", "Other": "2750123"}
GRADE_IDS = {g: f"27502{i}" for i, g in enumerate(
    ["10", "9.5", "9", "8.5", "8", "7.5", "7", "6.5", "6", "5.5", "5", "4.5", "4", "3.5", "3", "2.5", "2", "1.5", "1"])}
RAW_IDS = {"NM": "400010", "LP": "400015", "MP": "400016", "HP": "400017"}  # 183454 only; NOT 400011-13


class EbayError(Exception):
    pass


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise EbayError(f"{name} missing in .streamlit/secrets.toml")
    return v


PROD = "https://api.ebay.com"


def sandbox():
    return os.environ.get("EBAY_ENV", "sandbox") == "sandbox"


def hosts():
    """Hosts for selling calls. Browse (market data) always uses production — sandbox has no real listings."""
    return ("https://api.sandbox.ebay.com", "https://apim.sandbox.ebay.com") if sandbox() \
        else (PROD, "https://apim.ebay.com")


_tokens = {}


def _token(kind):
    """kind 'app' (Browse, production keys) or 'user' (Sell, sandbox keys while EBAY_ENV=sandbox)."""
    tok, exp = _tokens.get(kind, (None, 0))
    if tok and exp > time.time() + 60:
        return tok
    pre = "EBAY_SANDBOX_" if kind == "user" and sandbox() else "EBAY_"   # 'insights' = app keys, own scope
    basic = base64.b64encode(f"{_env(pre + 'APP_ID')}:{_env(pre + 'CERT_ID')}".encode()).decode()
    # Production refresh token: the one get_ebay_token.py saved in the app database wins over secrets.toml
    # (an editor saving an old copy of secrets.toml can't clobber it there).
    refresh = (db.setting("ebay_refresh_token", "") if pre == "EBAY_" else "") or (_env(pre + "REFRESH_TOKEN") if kind == "user" else "")
    scope = {"app": "https://api.ebay.com/oauth/api_scope",
             "insights": "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"}.get(kind)
    data = ({"grant_type": "client_credentials", "scope": scope} if scope
            else {"grant_type": "refresh_token", "refresh_token": refresh})  # scopes = those granted
    host = PROD if scope else hosts()[0]
    r = _http.post(host + "/identity/v1/oauth2/token", data=data,
                   headers={"Authorization": f"Basic {basic}"}, timeout=30)
    if r.status_code >= 400:
        raise EbayError(f"eBay OAuth ({kind}) failed: {r.text[:300]}")
    j = r.json()
    _tokens[kind] = (j["access_token"], time.time() + j["expires_in"])
    return j["access_token"]


def call(method, path, kind="user", host=None, **kw):
    headers = {"Authorization": f"Bearer {_token(kind)}", "X-EBAY-C-MARKETPLACE-ID": MARKET,
               "Content-Language": "en-GB", "Accept-Language": "en-GB", **kw.pop("headers", {})}
    r = _http.request(method, (host or (PROD if kind != "user" else hosts()[0])) + path,
                      headers=headers, timeout=60, **kw)
    if kind == "app":   # Browse calls count against the 5,000/day free limit
        db.run("INSERT INTO api_log VALUES (date('now'), 1) ON CONFLICT(day) DO UPDATE SET calls=calls+1")
    if r.status_code >= 400:
        raise EbayError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
    return r


# ---------- Browse: cheapest active listing in the same condition ----------

def _total(item):
    ship = ((item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}).get("value", 0)
    return float(item["price"]["value"]) + float(ship)


def _descriptor_condition(item):
    """Read (grader, grade) or raw condition from getItem conditionDescriptors."""
    d = {c["name"]: [v.get("content") or v.get("value") for v in c.get("values", [])]
         for c in item.get("conditionDescriptors") or []}
    # Browse returns display text in values[].content (e.g. 'Professional Sports Authenticator (PSA)', '10')
    return d


def _matches(desc, grader, grade, condition):
    flat = " ".join(" ".join(x for x in v if x) for v in desc.values()).lower()
    if grader:
        g = {"BGS": "beckett grading services"}.get(grader, f"({grader.lower()})")
        grades = desc.get("Grade") or desc.get("27502") or []
        return g in flat and any(str(x).strip() == str(grade) for x in grades)
    words = {"NM": "near mint", "LP": "lightly played", "MP": "moderately played", "HP": "heavily played"}
    return words[condition] in flat


PLAYED_ASPECT = "Card Condition:{Lightly Played (Excellent)|Moderately Played (Very Good)|Heavily Played (Poor)|Damaged}"


def search_active(query, condition_ids=None, category=CATEGORY, limit=200, sort=None, buying="FIXED_PRICE", offset=0,
                  aspect=None):
    """Active UK-located listings (excluding your own). buying: FIXED_PRICE or AUCTION. Free: 5,000 calls/day.
    offset pages through results 200 at a time (eBay allows up to 10,000). aspect: an item-specific filter such as
    PLAYED_ASPECT (eBay's structured 'Card Condition', which sellers fill in far more often than the title)."""
    f = [f"buyingOptions:{{{buying}}}", "itemLocationCountry:GB", "deliveryCountry:GB"]
    if condition_ids:
        f.append("conditionIds:{" + "|".join(condition_ids) + "}")
    me = db.setting("ebay_username")
    if me:
        f.append(f"excludeSellers:{{{me}}}")
    params = {"q": query, "filter": ",".join(f), "limit": limit}
    if offset:
        params["offset"] = offset
    if category:
        params["category_ids"] = category
    if sort:
        params["sort"] = sort
    if aspect:
        params["aspect_filter"] = f"categoryId:{category or CATEGORY},{aspect}"
    return call("GET", "/buy/browse/v1/item_summary/search", kind="app", params=params).json().get("itemSummaries", [])


# ---------- Reading one listing: condition and scam signals (pure) ----------

GRADERS = ("PSA", "BGS", "CGC", "SGC", "TAG", "ACE")
# Words in a description that mean the card is not a real, single card. Title junk lives in prices.TITLE_JUNK.
DESC_JUNK = ("proxy", "proxies", "custom card", "custom made", "reprint", "replica", "reproduction", "not official",
             "unofficial", "fan made", "fanmade", "fan-made", "metal card", "gold metal", "picture only", "photo only",
             "image only", "no physical", "printed at home", "orica")
_NEGATED = re.compile(r"\b(?:no|not|without|free (?:of|from)|zero|any|never)\b[^.,;!]{0,30}?"
                      r"(?:crease\w*|bend\w*|bent|water damage|torn|tear|ink|writing)")
_DAMAGE = re.compile(r"\b(crease[sd]?|creasing|bent|bends?|water damage|torn|tear|ink marks?|writing on|heavy whitening|"
                     r"rounded corners?)\b")


def _text(vals):
    return " ".join(str(v.get("content") or "") + " " + " ".join(v.get("additionalInfo") or []) for v in vals or [])


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))


def damaged_text(text):
    """Pure. True when free text admits a crease / bend / tear / ink (negations like 'no creases' ignored).
    ponytail: a phrase scrubber, not a parser; 'no creases but a bend' loses the bend."""
    t = _NEGATED.sub(" ", (text or "").lower())
    t = re.sub(r"\b(?:crease|bend|damage|tear)\w*[- ]free\b", " ", t)
    return _DAMAGE.search(t) is not None


def condition_from_item(item):
    """Pure. eBay's structured condition from a getItem payload, or None when the listing has none (title words
    decide then). Raw: {'graded': False, 'condition': NM/LP/MP/HP, 'downgraded': bool}; a declared NM/LP whose own
    description admits a crease or bend counts as MP. Graded: {'graded': True, 'grader', 'grade', 'cert'}."""
    desc = {d.get("name", ""): d.get("values") or [] for d in item.get("conditionDescriptors") or []}
    if not desc:
        return None
    pick = lambda *names: next((desc[n] for n in names if n in desc), [])
    if pick("Professional Grader", "27501") or item.get("conditionId") == "2750":
        g = _text(pick("Professional Grader", "27501")).upper()
        grader = next((x for x in GRADERS if x in g), "BGS" if "BECKETT" in g else None)
        grade = _text(pick("Grade", "27502")).strip() or None
        return {"graded": True, "grader": grader, "grade": grade,
                "cert": _text(pick("Certification Number", "27503")).strip() or None}
    cc = _text(pick("Card Condition", "40001")).lower().strip()
    cond = ("NM" if cc.startswith("near mint") else "LP" if cc.startswith("lightly") else
            "MP" if cc.startswith("moderately") else "HP" if cc.startswith(("heavily", "damaged")) else None)
    if not cond:
        return None
    own_words = " ".join([item.get("conditionDescription") or "", strip_html(item.get("description"))])
    down = cond in ("NM", "LP") and damaged_text(own_words)
    return {"graded": False, "condition": "MP" if down else cond, "downgraded": down}


def risky_seller(item, total):
    """Pure. Fake/scam risk from the seller: under 95 % positive feedback, or a near-new account (<10 feedback)
    asking £50+. Listings without seller info are not judged."""
    s = item.get("seller") or {}
    if s.get("feedbackScore") is None:
        return False
    return float(s.get("feedbackPercentage") or 0) < 95 or (int(s["feedbackScore"]) < 10 and total >= 50)


def listing_risk(item, total, market=None):
    """Pure. (hard, soft) reasons a listing may not be what it claims. hard = never shown, never a price point
    (poor-feedback or brand-new seller on a dear card, a lot, a description admitting a proxy/reprint, a price far
    too low to be real); soft = shown with a warning (few feedback, well under market, a single photo)."""
    hard, soft = [], []
    s = item.get("seller") or {}
    fb, pct = s.get("feedbackScore"), float(s.get("feedbackPercentage") or 100)
    if fb is not None:
        if pct < 95:
            hard.append(f"seller {pct:.0f}% positive")
        elif int(fb) < 10 and total >= 50:
            hard.append(f"new seller ({fb} feedback) at £{total:.0f}")
        elif int(fb) < 50 and total >= 30:
            soft.append(f"seller has {fb} feedback")
    if (item.get("lotSize") or 0) > 1:
        hard.append(f"lot of {item['lotSize']}")
    text = " ".join([item.get("title") or "", item.get("shortDescription") or "", strip_html(item.get("description"))]).lower()
    junk = next((w for w in DESC_JUNK if re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text)), None)
    if junk:
        hard.append(f"description says '{junk}'")
    if market and total:
        if market >= 40 and total <= 0.2 * market:
            hard.append(f"{total / market:.0%} of market: too cheap to be real")
        elif market >= 30 and total <= 0.35 * market:
            soft.append(f"only {total / market:.0%} of market")
    photos = (1 + len(item.get("additionalImages") or [])) if item.get("image") else 0
    if photos <= 1 and (market or 0) >= 30:
        soft.append("1 photo")
    return hard, soft


def sold_search(query, category=CATEGORY, limit=200):
    """Sold UK items, last 90 days (Marketplace Insights: restricted, needs eBay's approval for your keyset).
    Raises EbayError (403 / invalid_scope) until approved."""
    params = {"q": query, "category_ids": category, "limit": limit}
    return call("GET", "/buy/marketplace_insights/v1_beta/item_sales/search", kind="insights",
                params=params).json().get("itemSales", [])


def get_item(item_id):
    """Full Browse item, or None when eBay no longer has it (error 11001 / 404)."""
    try:
        return call("GET", f"/buy/browse/v1/item/{item_id}", kind="app").json()
    except EbayError as e:
        if "11001" in str(e) or "-> 404" in str(e):
            return None
        raise


def get_items(ids):
    """getItem for several listings at once (6 in flight: ~1 s per 6 instead of ~0.8 s each), in order.
    A call that fails comes back as its exception, so one bad listing doesn't lose the rest: callers skip those
    (`isinstance(item, Exception)`) and check them again next time. None still means eBay no longer has it."""
    def one(item_id):
        try:
            return get_item(item_id)
        except (EbayError, requests.RequestException) as e:
            return e
    if not ids:
        return []
    with ThreadPoolExecutor(6) as ex:
        return list(ex.map(one, ids))


def orders(since_iso):
    """Your own paid orders since an ISO timestamp (Sell Fulfillment; token needs sell.fulfillment scope)."""
    out, params = [], {"filter": f"creationdate:[{since_iso}..]", "limit": 200}
    while True:
        j = call("GET", "/sell/fulfillment/v1/order", params=params).json()
        out += j.get("orders", [])
        if not j.get("next"):
            return out
        params["offset"] = int(params.get("offset", 0)) + 200


def lowest_listing(query, title_ok, grader=None, grade=None, condition="NM", max_checks=10):
    """Cheapest active fixed-price UK listing whose condition descriptors match. None if not found."""
    found = search_active(query, ["2750" if grader else "4000"], limit=50, sort="price")
    items = sorted((i for i in found if title_ok(i["title"])), key=_total)[:max_checks]
    for n in range(0, len(items), 5):   # 5 at a time, cheapest first: usually one round trip instead of up to 10
        batch = items[n:n + 5]
        for it, full in zip(batch, get_items([i["itemId"] for i in batch])):
            if isinstance(full, dict) and _matches(_descriptor_condition(full), grader, grade, condition) \
                    and not listing_risk(full, _total(it))[0]:
                return {"title": it["title"], "total": round(_total(it), 2), "price": float(it["price"]["value"]),
                        "url": it.get("itemWebUrl")}
    return None


# ---------- Account / setup ----------

def policies():
    out = {}
    for kind in ("fulfillment", "payment", "return"):
        j = call("GET", f"/sell/account/v1/{kind}_policy", params={"marketplace_id": MARKET}).json()
        out[kind] = {p["name"]: p[f"{kind}PolicyId"] for p in j.get(f"{kind}Policies", [])}
    return out


def postage_for(price):
    """(postage policy id, £ the buyer pays for postage) for an item price: the tracked policy from
    `tracked_over` up when one is chosen in Settings, otherwise the standard (letter) policy."""
    if db.setting("tracked_policy") and price is not None and price >= float(db.setting("tracked_over")):
        return db.setting("tracked_policy"), float(db.setting("postage_tracked"))
    return db.setting("fulfillment_policy"), float(db.setting("postage"))


def ensure_location(key, postcode):
    try:
        call("GET", f"/sell/inventory/v1/location/{key}")
    except EbayError:
        call("POST", f"/sell/inventory/v1/location/{key}", json={
            "location": {"address": {"postalCode": postcode, "country": "GB"}},
            "locationTypes": ["WAREHOUSE"], "merchantLocationStatus": "ENABLED", "name": "Home"})


def required_aspects():
    r = call("GET", "/commerce/taxonomy/v1/category_tree/3/get_item_aspects_for_category", kind="app",
             params={"category_id": CATEGORY})
    return [a["localizedAspectName"] for a in r.json().get("aspects", [])
            if a.get("aspectConstraint", {}).get("aspectRequired")]


# ---------- Posting ----------

def upload_image(path):
    with open(path, "rb") as f:
        r = call("POST", "/commerce/media/v1_beta/image/create_image_from_file", host=hosts()[1],
                 files={"image": (os.path.basename(path), f, "image/jpeg")})
    loc = r.headers["Location"]
    return call("GET", loc.split(hosts()[1], 1)[-1] if loc.startswith("http") else loc, host=hosts()[1]).json()["imageUrl"]


def condition_payload(d):
    if d["graded"]:
        desc = [{"name": "27501", "values": [GRADER_IDS.get(d["grader"], GRADER_IDS["Other"])]},
                {"name": "27502", "values": [GRADE_IDS[str(d["grade"])]]}]
        if d.get("cert_number"):
            desc.append({"name": "27503", "additionalInfo": str(d["cert_number"])[:30]})
        return "LIKE_NEW", desc
    return "USED_VERY_GOOD", [{"name": "40001", "values": [RAW_IDS[d["condition"] or "NM"]]}]


def best_offer_terms(price, enabled=None, accept_pct=None, decline_pct=None, floor=None):
    """Inventory API bestOfferTerms for a listing at `price` (Settings by default), or None when Best Offer is off.
    eBay answers offers itself: >= accept_pct % of the price accepted, under decline_pct % declined. With a profit
    floor (listing.profit_floor), offers under it are always declined and never auto-accepted."""
    enabled = db.setting("best_offer") if enabled is None else enabled
    if not enabled:
        return None
    acc = float(db.setting("bo_accept_pct") if accept_pct is None else accept_pct)
    dec = float(db.setting("bo_decline_pct") if decline_pct is None else decline_pct)
    if floor and price > 0:   # as % of this price, so the floor wins whichever is higher
        dec = max(dec, floor / price * 100)
        acc = max(acc, dec)
    gbp = lambda pct: {"value": f"{price * pct / 100:.2f}", "currency": "GBP"}
    terms = {"bestOfferEnabled": True}
    if 0 < acc < 100:   # eBay rejects an auto-accept price at or above the listing price
        terms["autoAcceptPrice"] = gbp(acc)
    if 0 < dec < min(acc, 100):
        terms["autoDeclinePrice"] = gbp(dec)
    elif floor and 0 < dec < 100:   # floor at the accept level: decline everything under it
        terms["autoDeclinePrice"] = gbp(dec - 0.01 / price * 100)
    return terms


def post_listing(sku, d, image_urls):
    """Idempotent by SKU: re-running updates the item/offer instead of duplicating. Returns listingId."""
    condition, descriptors = condition_payload(d)
    call("PUT", f"/sell/inventory/v1/inventory_item/{sku}", json={
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
        "condition": condition, "conditionDescriptors": descriptors,
        "product": {"title": d["title"][:80], "description": d["description"],
                    "aspects": {k: [str(v)] for k, v in d["aspects"].items() if v not in (None, "")},
                    "imageUrls": image_urls}})
    offer = {"sku": sku, "marketplaceId": MARKET, "format": "FIXED_PRICE", "availableQuantity": 1,
             "categoryId": CATEGORY, "listingDescription": d["description"], "listingDuration": "GTC",
             "merchantLocationKey": db.setting("location_key"),
             "listingPolicies": {"fulfillmentPolicyId": postage_for(d["price"])[0],
                                 "paymentPolicyId": db.setting("payment_policy"),
                                 "returnPolicyId": db.setting("return_policy")},
             "pricingSummary": {"price": {"value": f"{d['price']:.2f}", "currency": "GBP"}}}
    if terms := best_offer_terms(d["price"], floor=d.get("profit_floor")):
        offer["listingPolicies"]["bestOfferTerms"] = terms
    existing = []
    try:
        existing = call("GET", "/sell/inventory/v1/offer", params={"sku": sku, "marketplace_id": MARKET}).json().get("offers", [])
    except EbayError:
        pass  # 404 when no offer exists yet
    if existing:
        offer_id = existing[0]["offerId"]
        if existing[0].get("listing", {}).get("listingId") and existing[0].get("status") == "PUBLISHED":
            return existing[0]["listing"]["listingId"]  # already live: never publish twice
        call("PUT", f"/sell/inventory/v1/offer/{offer_id}", json=offer)
    else:
        offer_id = call("POST", "/sell/inventory/v1/offer", json=offer).json()["offerId"]
    return call("POST", f"/sell/inventory/v1/offer/{offer_id}/publish").json()["listingId"]


# ---------- Live listings: real price (Trading API) and price changes (Inventory API) ----------

def xml_first(text, tag):
    """Pure. Text of the first <tag ...>…</tag> in an XML string, or None."""
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", text, re.S)
    return m[1] if m else None


def listed_price(listing_id):
    """The price actually on the live listing (what Seller Hub shows). Edits made in Seller Hub change this,
    but NOT the Inventory API offer, so this is the one to trust."""
    body = ('<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{int(listing_id)}</ItemID><OutputSelector>Item.StartPrice</OutputSelector>"
            "<OutputSelector>Item.SellingStatus.ListingStatus</OutputSelector></GetItemRequest>")
    r = _http.post(hosts()[0] + "/ws/api.dll", data=body.encode(), timeout=30, headers={
        "X-EBAY-API-CALL-NAME": "GetItem", "X-EBAY-API-SITEID": "3", "X-EBAY-API-COMPATIBILITY-LEVEL": "1225",
        "X-EBAY-API-IAF-TOKEN": _token("user"), "Content-Type": "text/xml"})
    if xml_first(r.text, "Ack") not in ("Success", "Warning"):
        raise EbayError(f"GetItem {listing_id}: {xml_first(r.text, 'LongMessage') or r.text[:300]}")
    return float(xml_first(r.text, "StartPrice")), xml_first(r.text, "ListingStatus")


def selling_limits():
    """{'items_left': int|None, 'value_left': float|None, 'active': int} from your seller account."""
    body = ('<?xml version="1.0" encoding="utf-8"?><GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<SellingSummary><Include>true</Include></SellingSummary>"
            "<ActiveList><Include>true</Include><Pagination><EntriesPerPage>1</EntriesPerPage></Pagination></ActiveList>"
            "</GetMyeBaySellingRequest>")
    r = _http.post(hosts()[0] + "/ws/api.dll", data=body.encode(), timeout=30, headers={
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling", "X-EBAY-API-SITEID": "3", "X-EBAY-API-COMPATIBILITY-LEVEL": "1225",
        "X-EBAY-API-IAF-TOKEN": _token("user"), "Content-Type": "text/xml"})
    t = r.text
    if xml_first(t, "Ack") not in ("Success", "Warning"):
        raise EbayError(f"GetMyeBaySelling: {xml_first(t, 'LongMessage') or t[:200]}")
    q, a = xml_first(t, "QuantityLimitRemaining"), xml_first(t, "AmountLimitRemaining")
    return {"items_left": int(q) if q else None, "value_left": float(a) if a else None,
            "active": int(xml_first(t, "TotalNumberOfEntries") or 0)}


def rescale_offer_terms(terms, old_price, new_price):
    """Pure. bestOfferTerms with auto-accept/decline £ moved to the same % of new_price."""
    out = dict(terms)
    for k in ("autoAcceptPrice", "autoDeclinePrice"):
        if terms.get(k) and old_price > 0:
            out[k] = {"value": f"{float(terms[k]['value']) / old_price * new_price:.2f}", "currency": "GBP"}
    return out


def watcher_eligible():
    """Listing ids whose watchers can be sent an offer right now (Sell Negotiation API, read-only)."""
    out, offset = [], 0
    while True:
        j = call("GET", "/sell/negotiation/v1/find_eligible_items", params={"limit": 200, "offset": offset}).json()
        out += [x["listingId"] for x in j.get("eligibleItems", [])]
        offset += 200
        if offset >= int(j.get("total") or 0):
            return out


def send_watcher_offer(listing_id, pct):
    """Offer everyone watching a listing pct % off for 2 days (eBay's 'send offers to interested buyers').
    Outward-facing: buyers get a message and can buy at that price."""
    return call("POST", "/sell/negotiation/v1/send_offer_to_interested_buyers", json={
        "allowCounterOffer": False, "offerDuration": {"unit": "DAY", "value": 2},
        "message": "Thanks for watching - here's a little off if you'd like it.",
        "offeredItems": [{"listingId": str(listing_id), "discountPercentage": f"{pct:g}", "quantity": 1}]}).json()


def send_buyer_message(item_id, buyer, subject, body):
    """Message a buyer about an item they bought from you (Trading API AddMemberMessageAAQToPartner).
    Outward-facing: lands in the buyer's eBay inbox. POST is never retried, so it can't send twice."""
    from xml.sax.saxutils import escape
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<AddMemberMessageAAQToPartnerRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f"<ItemID>{int(item_id)}</ItemID><MemberMessage><Subject>{escape(subject[:80])}</Subject>"
           f"<Body>{escape(body[:2000])}</Body><QuestionType>General</QuestionType>"
           f"<RecipientID>{escape(buyer)}</RecipientID></MemberMessage></AddMemberMessageAAQToPartnerRequest>")
    r = _http.post(hosts()[0] + "/ws/api.dll", data=xml.encode(), timeout=30, headers={
        "X-EBAY-API-CALL-NAME": "AddMemberMessageAAQToPartner", "X-EBAY-API-SITEID": "3",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1225", "X-EBAY-API-IAF-TOKEN": _token("user"), "Content-Type": "text/xml"})
    if xml_first(r.text, "Ack") not in ("Success", "Warning"):
        raise EbayError(f"Message to {buyer}: {xml_first(r.text, 'LongMessage') or r.text[:300]}")


def set_price(sku, price, floor=None):
    """Change a live listing's price. Updates the listing and the Inventory API offer together."""
    offers = call("GET", "/sell/inventory/v1/offer", params={"sku": sku, "marketplace_id": MARKET}).json().get("offers", [])
    if not offers:
        raise EbayError(f"No eBay offer found for {sku}")
    o = offers[0]
    bo = (o.get("listingPolicies") or {}).get("bestOfferTerms") or {}
    if bo.get("bestOfferEnabled"):
        # auto-accept/decline are £ figures: move them with the price (same % of it), in one offer update
        terms = rescale_offer_terms(bo, float(o["pricingSummary"]["price"]["value"]), price)
        if floor:   # a lower price must not drag the auto-accept / auto-decline under the profit floor
            for k in ("autoAcceptPrice", "autoDeclinePrice"):
                if terms.get(k) and float(terms[k]["value"]) < floor:
                    terms[k] = {"value": f"{min(floor, price - 0.01):.2f}", "currency": "GBP"}
        o["listingPolicies"]["bestOfferTerms"] = terms
        o["pricingSummary"]["price"] = {"value": f"{price:.2f}", "currency": "GBP"}
        call("PUT", f"/sell/inventory/v1/offer/{o['offerId']}",
             json={k: v for k, v in o.items() if k not in ("offerId", "status", "listing", "statusReason")})
        return
    r = call("POST", "/sell/inventory/v1/bulk_update_price_quantity", json={"requests": [{
        "sku": sku, "offers": [{"offerId": offers[0]["offerId"],
                                "price": {"value": f"{price:.2f}", "currency": "GBP"}}]}]}).json()
    res = (r.get("responses") or [{}])[0]
    if res.get("statusCode", 200) >= 400 or res.get("errors"):
        raise EbayError(f"Price update for {sku} failed: {(res.get('errors') or [{}])[0].get('message', res)}")


def listing_url(listing_id):
    return ("https://sandbox.ebay.co.uk/itm/" if os.environ.get("EBAY_ENV", "sandbox") == "sandbox"
            else "https://www.ebay.co.uk/itm/") + str(listing_id)
