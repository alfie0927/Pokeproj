"""TCGdex card catalog (free, no key). English + Japanese."""
import re
import time
from functools import lru_cache
from urllib.parse import quote

from .ebay import session

BASE = "https://api.tcgdex.net/v2"
_http = session()   # kept-alive: 0.02 s a call instead of 0.45 s


@lru_cache(maxsize=16384)   # the deal scanner meets ~10,000 different cards a week: 4,096 kept evicting and re-fetching
def _get(path):
    for attempt in range(4):
        r = _http.get(BASE + path, timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code in (429, 502, 503) and attempt < 3:   # TCGdex throttles bursts
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()


def release(c):
    """'YYYY-MM-DD' of the card's set. A full card's set brief has no date, so it comes from the (cached) set:
    without this every candidate looked undated and the copyright-year check never ran."""
    s = (c or {}).get("set") or {}
    if s.get("releaseDate") or not s.get("id"):
        return s.get("releaseDate") or ""
    sid = s["id"]
    for lang in (("en", "ja") if sid == sid.lower() else ("ja", "en")):   # Japanese set ids are upper case
        full = set_(lang, sid)
        if full and full.get("releaseDate"):
            return full["releaseDate"]
    return ""


_digital = None


def digital_sets():
    """Set ids of Pokémon TCG Pocket (digital only, never a physical card): 'A1', 'A1a', 'P-A', 'B2a', ..."""
    global _digital
    if _digital is None:
        try:
            _digital = frozenset(s["id"] for s in (_get("/en/series/tcgp") or {}).get("sets", []))
        except Exception:
            return frozenset()   # retry next time
    return _digital


def physical(briefs):
    """Pure-ish. Drop TCG Pocket cards: they crowded real cards out of the candidates (Liepard, Raikou, Kirlia...)."""
    dig = digital_sets()
    return [b for b in briefs if b["id"].rsplit("-", 1)[0] not in dig]


def search(lang, name="", number=""):
    """Brief cards: [{id, localId, name, image}] (physical cards only)"""
    params = []
    if name:
        params.append("name=" + quote(name))
    if number:
        params.append("localId=" + quote(number))  # eq: returns nothing on localId; filter exactly below
    out = physical(_get(f"/{lang}/cards?" + "&".join(params)) or [])
    return [c for c in out if c["localId"].lower() == number.lower()] if number else out


def card(lang, card_id):
    return _get(f"/{lang}/cards/{quote(card_id)}")


def set_(lang, set_id):
    return _get(f"/{lang}/sets/{quote(set_id)}")


ERAS = {"me": "Mega Evolution", "sv": "Scarlet & Violet", "swsh": "Sword & Shield", "sm": "Sun & Moon", "xy": "XY"}


def sets_index(lang="en"):
    """{set_id: brief} for every set (one call, cached): id, name, logo, symbol, cardCount."""
    return {s["id"]: s for s in _get(f"/{lang}/sets") or []}


def series_sets(series_id, lang="en"):
    """Sets of a series in release order (briefs: id, name, logo?)."""
    return (_get(f"/{lang}/series/{quote(series_id)}") or {}).get("sets", [])


def era_of(set_id):
    """'me01' / '30th' -> 'me', 'sv08' -> 'sv', 'swsh12.5gg' -> 'swsh', 'sm115' -> 'sm', 'xy7' -> 'xy'; None if unknown. Pure."""
    for e in ("swsh", "sv", "sm", "xy", "me", "30th"):   # 30th Celebration (2026) sits in TCGdex's Mega Evolution series
        if set_id.lower().startswith(e):
            return "me" if e == "30th" else e
    return None


_JA = re.compile(r"[぀-ヿ一-鿿]")
_SET = re.compile(r"^(?:sv|swsh|sm|xy|me)[\w.]*$|^s\d{1,2}[a-z]?$", re.I)
_NUM = re.compile(r"^(?:[A-Za-z]{1,4})?\d{1,3}(?:/[A-Za-z]{0,4}\d{1,3})?$")


def parse_search(text):
    """Pure. One search box -> {lang, name, number, set}. Examples:
    'charizard 199' -> name+number; 'sv08 238' -> set+number; '199/165' -> number; 'TG05' -> number;
    'リザードン' -> Japanese name; 'pikachu sv08' -> name+set."""
    words = text.strip().split()
    out = {"lang": "ja" if _JA.search(text) else "en", "name": "", "number": "", "set": ""}
    rest = []
    for w in words:
        if not out["set"] and _SET.match(w) and any(c.isdigit() for c in w):
            out["set"] = w
        elif not out["number"] and _NUM.match(w) and any(c.isdigit() for c in w):
            out["number"] = w.split("/")[0]
        else:
            rest.append(w)
    out["name"] = " ".join(rest)
    return out


def set_logo(s):
    return f"{s['logo']}.png" if s and s.get("logo") else None


def cards_by_rarity(lang, set_id, rarity):
    return _get(f"/{lang}/cards?set.id=eq:{quote(set_id)}&rarity=eq:{quote(rarity)}") or []


def image(c, quality="high"):
    return f"{c['image']}/{quality}.webp" if c and c.get("image") else None


def printed_number(c):
    """'199/165' for numbered cards, else the raw localId (TG05, SVP 123...). Promo cards print no set total
    (Black Star Promos: '173', not '173/225', the promo set's card count)."""
    s = c.get("set") or {}
    local, total = c["localId"], s.get("cardCount", {}).get("official")
    if re.search(r"promo|プロモ", s.get("name") or "", re.I):
        return local
    # ponytail: uses the printed padding ('025/165'); sellers who write '25/165' are missed
    return f"{local}/{total}" if local.isdigit() and total else local


def english_name(lang, c):
    """English name for search queries. Japanese cards: via Pokédex number (None for trainers)."""
    if lang == "en":
        return c["name"]
    dex = (c.get("dexId") or [None])[0]
    if not dex:
        return None
    names = [b["name"] for b in search_dex(dex)]
    if not names:
        return None
    base = min(names, key=len)
    jp = c["name"]
    suffix = next((s for s in ("VMAX", "VSTAR", "GX", "ex", "EX", "V") if jp.endswith(s)), "")
    mega = "Mega " if jp.startswith("メガ") else ""
    return f"{mega}{base} {suffix}".strip()


def english_from_chinese(name):
    """'喵喵' -> 'Meowth' via TCGdex's Chinese cards (Traditional, then Simplified) and their Pokédex number.
    ponytail: names written differently in Simplified and Traditional (喷火龙 / 噴火龍) aren't found; add an
    s->t character map if Chinese cards become common."""
    if not name:
        return None
    for lang in ("zh-tw", "zh-cn"):
        briefs = search(lang, name)
        exact = [b for b in briefs if b["name"] == name] or briefs   # '喵喵' before '伽勒爾 喵喵' (Galarian)
        for b in exact[:3]:
            c = card(lang, b["id"])
            en = c and english_name(lang, c)
            if en:
                return en
    return None


def search_dex(dex):
    return _get(f"/en/cards?dexId=eq:{dex}") or []


def cardmarket_gbp(c, eur_gbp, holo=False):
    cm = ((c or {}).get("pricing") or {}).get("cardmarket") or {}
    eur = (cm.get("trend-holo") if holo else None) or cm.get("trend") or cm.get("avg")
    return round(eur * eur_gbp, 2) if eur else None


def _cards(lang, briefs, limit=40):
    """Full cards for brief results, fetched in parallel (each is one TCGdex call, cached afterwards)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        return [c for c in ex.map(lambda b: card(lang, b["id"]), briefs[:limit]) if c]


def find(lang, name_en, number, set_code=None):
    """Match a card read from a photo. Returns list of full card candidates (0, 1 or many)."""
    local, _, total = number.replace(" ", "").partition("/")
    if lang == "ja":
        c = card("ja", f"{set_code}-{local.zfill(3) if local.isdigit() else local}") if set_code else None
        if c:
            return [c]
        briefs = search("ja", "", local.zfill(3) if local.isdigit() else local)   # no/unreadable set code
        if total.isdigit():   # set size from the cached sets list first: '092' alone is in hundreds of sets
            idx = sets_index("ja")
            briefs = [b for b in briefs if (idx.get(b["id"].rsplit("-", 1)[0]) or {}).get("cardCount", {})
                      .get("official") in (None, int(total))]
        return [c for c in _cards("ja", briefs, limit=80)
                if not total.isdigit() or (c.get("set") or {}).get("cardCount", {}).get("official") == int(total)]
    tries = list(dict.fromkeys([local.zfill(3), local.lstrip("0") or "0"])) if local.isdigit() else [local]
    fits = lambda c: not total.isdigit() or (c.get("set") or {}).get("cardCount", {}).get("official") == int(total)
    out = []
    # every number form ('079' and '79': sets write it differently), with the name first; stopping at the first
    # form that returned anything lost Champion's Path 79/73 to other sets' card 079 that the total then removed
    idx = sets_index(lang) if total.isdigit() else {}
    # set size from the cached sets list, before fetching any card: a bare '5' is in 170+ sets
    size_ok = lambda b: (idx.get(b["id"].rsplit("-", 1)[0]) or {}).get("cardCount", {}).get("official") in (None, int(total))
    for nm in dict.fromkeys([name_en, ""]):
        briefs = [b for b in {b["id"]: b for t in tries for b in search("en", nm, t)}.values() if not idx or size_ok(b)]
        out = [c for c in _cards(lang, briefs, limit=80) if fits(c)]
        if out:
            break
    if set_code and len(out) > 1:
        coded = [c for c in out if (set_(lang, c["set"]["id"]).get("abbreviation") or {}).get("official", "").upper() == set_code.upper()]
        out = coded or out
    return out
