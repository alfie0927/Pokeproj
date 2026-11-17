"""Photo pair -> CardRead. Backends: 'ocr' (default, free, offline: RapidOCR reads the printed number / set code /
slab label; the catalog resolves the card) or 'claude' (VISION_BACKEND=claude, needs ANTHROPIC_API_KEY)."""
import base64
import io
import os
import re
from typing import Literal, Optional

from PIL import Image, ImageOps
from pydantic import BaseModel

MODEL = "claude-opus-5-5"

SYSTEM = """You identify and grade Pokémon TCG cards for eBay UK listings.
Image 1 is the FRONT, image 2 should be the BACK of the same card (or the same graded slab).

Identify: the English name of the card (even if the card is Japanese), the name exactly as printed,
the collector number exactly as printed (e.g. "199/165", "TG05/TG30", "SVP 123"), the printed set code
if visible (bottom-left, e.g. "MEW", "SV2a"), and the English set name if you know it. Language "en" or "ja"
(anything else "other"). Variant: reverse_holo only if the whole card body outside the art is holofoil.

If it is in a grading slab, read grader, grade and certification number from the label; condition is then null.

If raw, grade condition against eBay's ungraded definitions:
- NM: near mint or better — no visible wear beyond tiny factory imperfections.
- LP: lightly played — minor edge/corner whitening or light surface scratches.
- MP: moderately played — noticeable whitening, scuffs, minor bends.
- HP: heavily played — major wear, creases, bends, tears or water damage.
Be conservative: when torn between two grades, pick the lower one. Phone photos hide surface scratches,
so mention anything you cannot assess. Centering counts in condition_notes but rarely drops a grade alone.

condition_summary: 2-3 honest buyer-facing sentences (UK English) describing the condition.
List in uncertain_fields every field you are not confident about. Set pair_ok=false if image 2 is not the
back of the same card/slab (e.g. two fronts, different cards)."""


class CardRead(BaseModel):
    pair_ok: bool
    language: Literal["en", "ja", "other"]
    name_en: str
    name_printed: str
    number: str
    set_code: Optional[str]
    set_name_en: Optional[str]
    variant: Literal["normal", "holo", "reverse_holo", "first_edition", "other"]
    graded: bool
    grader: Optional[Literal["PSA", "BGS", "CGC", "SGC", "TAG", "ACE", "Other"]]
    grade: Optional[str]
    cert_number: Optional[str]
    condition: Optional[Literal["NM", "LP", "MP", "HP"]]
    condition_notes: list[str]
    condition_summary: str
    uncertain_fields: list[str]


def _natural(name):
    """'IMG_2.jpg' < 'IMG_10.jpg'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def pair_photos(named):
    """named: [(filename, image bytes or PIL image)], fronts and backs in any order.
    Returns [(front_index, back_index)]. Card backs are one shared design, so the back photos look like each
    other while fronts all differ: the half of the photos closest to their neighbours are the backs.
    With a single card (2 photos) there is nothing to compare, so filename order is kept (front first)."""
    import numpy as np
    order = sorted(range(len(named)), key=lambda i: _natural(named[i][0]))
    n = len(order)
    if n < 4:
        return [(order[i], order[i + 1]) for i in range(0, n - 1, 2)]
    vecs = []
    for i in order:
        im = named[i][1]
        im = Image.open(io.BytesIO(im)) if isinstance(im, (bytes, bytearray)) else im
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        im = im.crop((w // 8, h // 8, w - w // 8, h - h // 8)).resize((24, 32))  # drop background edges
        v = np.asarray(im, dtype=float).ravel()
        vecs.append((v - v.mean()) / (v.std() or 1))                                # ignore brightness / exposure
    v = np.array(vecs)
    dist = np.sqrt(((v[:, None, :] - v[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(dist, np.inf)
    k = n // 2 - 1                                   # each back has k other backs to look like
    score = np.sort(dist, axis=1)[:, :k].mean(1)
    is_back = set(np.argsort(score)[: n // 2].tolist())
    fronts = [order[j] for j in range(n) if j not in is_back]
    backs = [order[j] for j in range(n) if j in is_back]
    return list(zip(fronts, backs))


def backend():
    return os.environ.get("VISION_BACKEND") or "ocr"


def read_card(front_path, back_path, client=None):
    return _claude(front_path, back_path, client) if backend() == "claude" else _ocr(front_path, back_path)


# ---------------------------------------------------------------- Claude (paid, optional)

def _jpeg_b64(path, max_edge=2576):
    im = path if isinstance(path, Image.Image) else ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im = im.copy()
    im.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode()


def _claude(front_path, back_path, client=None):
    import anthropic
    client = client or anthropic.Anthropic()
    imgs = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(p)}}
            for p in (front_path, back_path)]
    resp = client.beta.messages.parse(
        model=MODEL, max_tokens=16000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "high"}, output_format=CardRead,
        messages=[{"role": "user", "content": [*imgs, {"type": "text", "text": "Identify and grade this card."}]}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude could not read this pair (stop_reason={resp.stop_reason})")
    return resp.parsed_output


# ---------------------------------------------------------------- Listing photos (Deal finder condition check)

GRADE_MODEL = "claude-sonnet-5-5"   # ponytail: hundreds of listings a day; switch to claude-opus-5-5 if grades look soft
GRADE_SYSTEM = """You grade raw (ungraded) Pokémon TCG cards from the photos of one eBay UK listing, for a buyer.
Decide from what the photos actually show:
- front_seen / back_seen: is the card's front / back shown clearly enough to judge (a real photo, not a stock image)?
- white_background: are the card photos taken on a white or near-white surface, where edge whitening cannot be seen?
- condition against eBay's ungraded definitions, from the corners, edges, surface and back:
  NM: no visible wear beyond tiny factory imperfections. LP: minor edge/corner whitening or light surface scratches.
  MP: noticeable whitening, scuffs, minor bends. HP: creases, bends, tears, heavy wear or water damage.
  Be conservative: torn between two grades, pick the lower. null when the photos are too small, blurred or glary to judge.
- notes: up to 3 short UK-English phrases naming the wear seen ("whitening on back corners", "scratch across the holo");
  empty when none."""
COND_ORDER = ["NM", "LP", "MP", "HP"]


class PhotoGrade(BaseModel):
    front_seen: bool
    back_seen: bool
    white_background: bool
    condition: Optional[Literal["NM", "LP", "MP", "HP"]]
    notes: list[str]


def photo_urls(item, limit=6):
    """Pure. Full-size photo URLs of a Browse item (main first), at most `limit`."""
    urls = [(item.get("image") or {}).get("imageUrl")] + [i.get("imageUrl") for i in item.get("additionalImages") or []]
    return [u.replace("s-l225", "s-l1600") for u in urls if u][:limit]


def fetch_photo(url, max_edge=1200):
    from . import ebay
    r = ebay._http.get(url, timeout=15)
    r.raise_for_status()
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(r.content))).convert("RGB")
    im.thumbnail((max_edge, max_edge))
    return im


def is_card_back(im):
    """Pure. Card backs are the one shared design: a blue-dominant centre. On real listing photos backs score
    0.42-0.67 bluish pixels and fronts (incl. pink / red / green cards) at most 0.22."""
    import numpy as np
    w, h = im.size
    a = np.asarray(im.crop((w // 8, h // 8, w - w // 8, h - h // 8))).astype(int)
    bluish = (a[..., 2] > a[..., 0] + 15) & (a[..., 2] >= a[..., 1])
    # ponytail: a blue full-art front can pass and a back shot under warm light in a sleeve can fail; Claude's
    # back_seen wins when a key is set
    return bool(bluish.mean() > 0.32)


def on_white(im):
    """Pure. Photo taken on a white / near-white surface (the outer 6 % of the frame): edge whitening can't be seen."""
    import numpy as np
    a = np.asarray(im).astype(int)
    h, w = a.shape[:2]
    m = max(2, int(min(h, w) * 0.06))
    border = np.concatenate([a[:m].reshape(-1, 3), a[-m:].reshape(-1, 3), a[:, :m].reshape(-1, 3), a[:, -m:].reshape(-1, 3)])
    white = (border.min(1) > 185) & (border.max(1) - border.min(1) < 40)
    return bool(white.mean() > 0.6)


def photo_verdict(backs, whites, grade, declared):
    """Pure. backs/whites: per-photo heuristics; grade: PhotoGrade or None; declared: NM/LP/MP/HP from eBay/title.
    Returns (reason the listing is ignored or None, condition, notes). No back photo -> ignored; back shown on a white
    background -> ignored; a photo grade below the declared one wins (a seller's 'NM' does not beat visible wear)."""
    back = grade.back_seen if grade else any(backs)
    white = grade.white_background if grade else any(w for b, w in zip(backs, whites) if b)
    if not back:
        return "no photo of the back", declared, []
    if white:
        return "photos on a white background (whitening can't be seen)", declared, []
    notes = list(grade.notes) if grade else []
    if grade and grade.condition and COND_ORDER.index(grade.condition) > COND_ORDER.index(declared):
        return None, grade.condition, notes
    return None, declared, notes


def grade_photos(images, client=None):
    """Claude looks at every photo of the listing (needs ANTHROPIC_API_KEY). images: PIL images."""
    import anthropic
    client = client or anthropic.Anthropic()
    imgs = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(im, 1200)}}
            for im in images]
    resp = client.messages.parse(
        model=GRADE_MODEL, max_tokens=1500, system=GRADE_SYSTEM, output_format=PhotoGrade,
        messages=[{"role": "user", "content": [*imgs, {"type": "text", "text": f"The {len(images)} photos of one listing. Grade the card."}]}],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude could not grade these photos (stop_reason={resp.stop_reason})")
    return resp.parsed_output


# ---------------------------------------------------------------- OCR (free, offline)

NUMBER = re.compile(r"(?<![\d/])([A-Z]{0,3} ?\d{1,3})\s*/\s*(\d{2,3})(?!\d)")   # '201/165SAR' -> 201/165
SET_CODE = re.compile(r"^(?:[A-Z]{2,4}|(?i:SV|S|SM)\d{1,2}[a-zA-Z]?)$")   # 'MEW', 'SSP', 'SV2a'/'sv2a'; not 'from'
NOT_CODES = {"HP", "PSA", "BGS", "CGC", "SGC", "TAG", "ACE", "MT", "NM", "GEM", "MINT", "LP", "MP",
             "GAME", "FREAK", "INC", "LLC", "TM", "LTD", "CO"}   # copyright line words
GRADERS = re.compile(r"\b(PSA|BGS|BECKETT|CGC|SGC|TAG|ACE)\b", re.I)
GRADE = re.compile(r"\b(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b")
JA = re.compile(r"[぀-ヿ一-鿿]")
# Card-type labels and evolution lines that sit near the name ('BASIC', 'STAGE2', 'Evolves from…', 'たね', '…から進化')
STAGE = re.compile(r"^\s*(BASIC|STAGE\s*\w?|Evolves\s+from|TRAINER|ITEM|SUPPORTER|STADIUM|ENERGY|たね|1進化|2進化)\b|進化$", re.I)
GENERIC_SUMMARY = "Please see the photos, which form part of the description. Condition as stated above."

_engines = {}


def _engine(hi=False):
    """PP-OCRv5 mobile (en+ja), 4 threads. hi=True: finer text detection for the small collector-number print;
    hi="rec": separate instance for recognition-only calls (RapidOCR keeps per-call flags on the instance)."""
    if hi not in _engines:
        from rapidocr import ModelType, OCRVersion, RapidOCR
        # ponytail: mobile model + 4 threads = ~5s/image on a laptop CPU; RapidOCR defaults were 5x slower here
        params = {"Det.model_type": ModelType.MOBILE, "Det.ocr_version": OCRVersion.PPOCRV5,
                  "Rec.model_type": ModelType.MOBILE, "Rec.ocr_version": OCRVersion.PPOCRV5, "Global.use_cls": False,
                  "EngineConfig.onnxruntime.intra_op_num_threads": int(os.environ.get("OCR_THREADS", 4))}
        if hi is True:
            params["Det.limit_side_len"] = 1600
        _engines[hi] = RapidOCR(params=params)
    return _engines[hi]


def _load(path):
    import numpy as np
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((3000, 3000))
    return np.array(im)


def card_quad(img):
    """Corners of the card in a photo (largest card-shaped bright/dark region), or None. img: RGB array."""
    import cv2
    g = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    best = None
    for mask in (th, 255 - th):                       # card lighter or darker than the background
        cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cs:
            rect = cv2.minAreaRect(c)
            w, h = rect[1]
            if not w or not h:
                continue
            frac = w * h / (img.shape[0] * img.shape[1])
            if 0.2 <= frac <= 0.97 and 0.55 <= min(w, h) / max(w, h) <= 0.85 and (best is None or frac > best[0]):
                best = (frac, cv2.boxPoints(rect))
    return best[1] if best else None


def warp_card(img, quad, W=1500, H=2100):
    """Straighten the card to a W×H portrait image."""
    import cv2
    import numpy as np
    pts = sorted(quad.tolist(), key=lambda p: p[0] + p[1])
    tl, br = pts[0], pts[-1]
    bl, tr = sorted(pts[1:3], key=lambda p: p[0] - p[1])
    src = [tl, tr, br, bl]
    if np.hypot(*np.subtract(tr, tl)) > np.hypot(*np.subtract(br, tr)):   # card lying sideways
        src = [bl, tl, tr, br]
    m = cv2.getPerspectiveTransform(np.float32(src), np.float32([[0, 0], [W, 0], [W, H], [0, H]]))
    return cv2.warpPerspective(img, m, (W, H), flags=cv2.INTER_CUBIC)


def _lines(img, rot=0, crop=None, scale=1, left=1.0, hi=False):
    """[(text, cx, cy, h)] with coords normalised 0-1. img: path or RGB array; rot: degrees;
    crop: (top, bottom) fractions; left: keep this fraction of the width (from the left)."""
    import numpy as np
    im = Image.fromarray(img) if not isinstance(img, (str, os.PathLike)) else ImageOps.exif_transpose(Image.open(img)).convert("RGB")
    im.thumbnail((2400, 2400))
    if rot:
        im = im.rotate(rot, expand=True)
    if crop or left < 1:
        c = crop or (0, 1)
        im = im.crop((0, int(im.height * c[0]), int(im.width * left), int(im.height * c[1])))
    if scale != 1:                                   # upscale small print, but keep the strip <= ~3000px wide
        scale = max(1, min(scale, 3000 // im.width))
        im = im.resize((im.width * scale, im.height * scale), Image.BICUBIC)
    res = _engine(hi)(np.array(im), use_det=True, use_cls=False, use_rec=True)
    out = []
    if res.boxes is None or res.txts is None:
        return out
    for box, txt in zip(res.boxes, res.txts):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append((txt.strip(), (min(xs) + max(xs)) / 2 / im.width, (min(ys) + max(ys)) / 2 / im.height,
                    (max(ys) - min(ys)) / im.height))
    return out


def parse_lines(lines):
    """Pure. lines: [(text, cx, cy, h)] -> dict(number, set_codes, name, language, graded, grader, grade, cert)."""
    texts = [t for t, *_ in lines]
    joined = lines + [(a[0] + b[0], b[1], b[2], b[3]) for a, b in zip(lines, lines[1:])]  # '199/' + '165'
    nums = [(m[1].replace(" ", "") + "/" + m[2], cy) for t, _, cy, _ in joined
            if "HP" not in t.upper() for m in [NUMBER.search(t)] if m]
    number = max(nums, key=lambda n: n[1])[0] if nums else None
    codes = []
    for t, _, cy, _ in lines:
        if cy > 0.75:
            for w in re.split(r"[\s/|]+", t):
                # 'MEWEN' / 'SSPE' = set code + (partly read) language mark: try both the raw and stripped forms
                for v in dict.fromkeys([w, re.sub(r"^([A-Z]{2,4})(EN|JP|E|J)$", r"\1", w)]):
                    if re.match(r"^s[vm]?\d", v, re.I):
                        v = v[:2].upper() + v[2:] if v[1].isalpha() else v[0].upper() + v[1:]   # 'sv2a' -> 'SV2a'
                    if SET_CODE.match(v) and v.upper() not in NOT_CODES and v not in codes:
                        codes.append(v)
    top = [l for l in lines if l[2] < 0.3 and not GRADERS.search(l[0]) and re.search(r"[A-Za-z぀-ヿ一-鿿]{3,}", l[0])
           and not STAGE.search(l[0])]
    name = re.sub(r"\bHP\s*\d+\b", "", max(top, key=lambda l: l[3])[0]).strip() if top else ""
    graded = next((GRADERS.search(t) for t in texts if GRADERS.search(t)), None)
    grader = grade = cert = None
    if graded:
        grader = {"BECKETT": "BGS"}.get(graded[1].upper(), graded[1].upper())
        for i, t in enumerate(texts):
            u = t.upper()
            if GRADE.search(u) and (re.search(r"MINT|MT|GEM|PRISTINE|NM|GRADE", u) or (i and GRADERS.search(texts[i - 1]))):
                grade = GRADE.search(u)[1]
                break
        cert = next((m[0] for t in texts for m in [re.search(r"\b\d{7,10}\b", t)] if m), None)
    # Japanese only if the name is, or several lines are: one stray misread character shouldn't flip it
    ja = bool(JA.search(name)) or sum(bool(JA.search(t)) for t in texts) >= 3
    return {"number": number, "set_codes": codes, "name": name, "graded": bool(graded), "grader": grader,
            "grade": grade, "cert": cert, "language": "ja" if ja else "en"}


class OcrRead(CardRead):
    """CardRead plus the catalog cards the OCR backend already resolved (first = best match)."""
    candidate_ids: Optional[list[str]] = None


# Characters OCR mixes up in the italic number font -> one canonical form ('TGBO' reads as 'TG30').
_LOOKALIKE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "!": "1",
                            "B": "3", "8": "3", "Z": "2", "S": "5"})
# Where the collector number is printed on a straightened card: (top, bottom, left, right) fractions.
NUMBER_BANDS = [(0.94, 0.98, 0.12, 0.36), (0.935, 0.985, 0.10, 0.40),   # Sword & Shield / Scarlet & Violet
                (0.935, 0.985, 0.62, 0.98)]                            # older cards: bottom right


def printed_full(c):
    """The number as printed: '199/165', gallery cards 'TG03/TG30', 'GG34/GG70'; promos just the id."""
    local, total = c.get("localId") or "", ((c.get("set") or {}).get("cardCount") or {}).get("official")
    m = re.match(r"^([A-Za-z]*)(\d+)$", local)
    if not (m and total):
        return local
    return f"{local}/{m[1]}{total}" if m[1] else f"{local}/{total}"


def _canon(s):
    return s.upper().replace(" ", "").translate(_LOOKALIKE)


def number_similarity(read, printed):
    """Pure. 0-1: how well the printed number appears somewhere in the OCR text, ignoring look-alike characters."""
    from difflib import SequenceMatcher
    r, p = _canon(read), _canon(printed)
    if not p or "/" not in p or not r:
        return 0.0
    n = len(p)
    return max(SequenceMatcher(None, r[i:i + n], p).ratio() for i in range(max(1, len(r) - n + 1)))


def pick_card(cards, reads):
    """Pure. cards: catalog cards sharing the OCR'd name; reads: OCR strings from the number bands.
    Returns (best or None, candidates best-first). Auto-picks only a clear winner."""
    from .catalog import release
    score = {c["id"]: max((number_similarity(t, printed_full(c)) for t in reads), default=0.0) for c in cards}
    score = {k: (v if v >= 0.6 else 0.0) for k, v in score.items()}   # weak partial matches are noise
    ranked = sorted(cards, key=lambda c: (round(score[c["id"]], 2), release(c)), reverse=True)
    if not ranked:
        return None, []
    top, second = score[ranked[0]["id"]], (score[ranked[1]["id"]] if len(ranked) > 1 else 0.0)
    if top >= 0.75 and top - second >= 0.15:
        return ranked[0], [ranked[0]]
    return None, ranked


NAME_SUFFIX = re.compile(r"\s*(ex|EX|V|VMAX|VSTAR|GX)$")


def _name_cards(lang, name):
    """Catalog cards for an OCR'd name: as read, then without an ex/V suffix ('Tinkatonex'), then first word."""
    from . import catalog
    for q in dict.fromkeys([name, NAME_SUFFIX.sub("", name), name.split(" ")[0]]):
        if len(q) >= 3:
            briefs = catalog.search(lang, q)
            if briefs:
                return catalog._cards(lang, briefs, limit=80)
    return []


def _ocr(front_path, back_path, quick=False):
    """Straightened card first (phone photos); if that finds no catalog card at all, the photo as-is
    (scans/renders with no background, where the 'card' found may really be the artwork window).
    quick=True (deal scanner): no rotated re-reads and no fine-detail read, so a photo that can't be read costs
    one or two reads (2-5 s) instead of up to eight (15 s+)."""
    img = _load(front_path)
    q = card_quad(img)
    tries = ([(warp_card(img, q), True)] if q is not None else []) + [(img, False)]
    for card, straight in tries:
        res = _resolve(card, straight, quick)
        if res.candidate_ids:   # one card, or a short list to pick from (re-reading the raw photo cost 8-10 s
            return res          # and came back with the same list)
    return res


def bottom_reads(lines, straight=True):
    """Pure. Texts the first read found near the bottom edge, where the collector number is printed
    ('TG03/TG30', 'GG24/GG70★': read fine, but not a plain n/total that parse_lines turns into a number)."""
    return [t for t, _, cy, _ in lines if cy > (0.86 if straight else 0.6)]


def _resolve(card, straight, quick=False):
    import numpy as np
    from . import catalog
    lines = _lines(card)
    p = parse_lines(lines)
    if not p["name"] and not quick:                    # straightened upside down, or no card found: try rotations
        for rot in (180, 90, 270):
            lines2 = _lines(card, rot=rot)
            p2 = parse_lines(lines2)
            if p2["name"]:
                p, lines, card = p2, lines2, np.ascontiguousarray(np.rot90(card, rot // 90))   # same turn as PIL's rotate()
                break
    lang, number = p["language"], p["number"] or ""
    cands = []
    if number:                                         # full '199/165' read: set code + count narrow it down
        for code in p["set_codes"] + [None]:
            cands = catalog.find(lang, p["name"], number, code)
            if cands:
                break
    if not cands and p["name"]:                        # no plain number: match what was read within this name's cards
        cards = _name_cards(lang, p["name"])
        # cheapest first, stopping at a clear winner: the first read's own bottom lines (free), then the fixed number
        # bands (recognition only, ~0.1 s), then a fine-detail read of the bottom strip (3-5 s, rarely needed)
        reads = bottom_reads(lines, straight) + ([number] if number else [])
        best, cands = pick_card(cards, reads)
        if not best and straight:
            H, W = card.shape[:2]
            reads += [t for y0, y1, x0, x1 in NUMBER_BANDS
                      for t in (_engine("rec")(np.ascontiguousarray(card[int(H * y0):int(H * y1), int(W * x0):int(W * x1)]),
                                               use_det=False, use_cls=False, use_rec=True).txts or [])]
            best, cands = pick_card(cards, reads)
        if not best and not quick:
            reads += [t for t, *_ in _lines(card, crop=(0.88, 1.0), left=0.55, scale=2, hi=True)]
            best, cands = pick_card(cards, reads)
        cands = [best] if best else cands[:15]
    c = cands[0] if len(cands) == 1 else None
    if c:
        number = printed_full(c)
    unsure = ["photo pairing (not checked by OCR)"] + (["number"] if not c else []) + (["name"] if not p["name"] else []) \
        + ([k for k in ("grade", "cert_number") if not p[k.split("_")[0]]] if p["graded"] else [])
    return OcrRead(
        pair_ok=True, language=lang, name_en=(catalog.english_name(lang, c) if c else None) or p["name"] or "Unknown",
        name_printed=p["name"] or "", number=number, set_code=p["set_codes"][0] if p["set_codes"] else None,
        set_name_en=None, variant="normal", graded=p["graded"], grader=p["grader"], grade=p["grade"],
        cert_number=p["cert"], condition=None if p["graded"] else "NM", condition_notes=[],
        condition_summary=GENERIC_SUMMARY, uncertain_fields=unsure, candidate_ids=[x["id"] for x in cands])
