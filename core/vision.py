"""Photo pair -> CardRead. Backends: 'ocr' (default, free, offline: RapidOCR reads the printed number / set code /
slab label; the catalog resolves the card) or 'claude' (VISION_BACKEND=claude, needs ANTHROPIC_API_KEY)."""
import base64
import io
import os
import re
from functools import lru_cache
from typing import Literal, Optional

from PIL import Image, ImageOps
from pydantic import BaseModel

MODEL = "claude-opus-5-5"

SYSTEM = """You identify and grade Pokémon TCG cards for eBay UK listings.
Image 1 is the FRONT, image 2 should be the BACK of the same card (or the same graded slab).

Identify: the English name of the card (even if the card is Japanese), the name exactly as printed,
the collector number exactly as printed (e.g. "199/165", "TG05/TG30", "SVP 123"), the printed set code
if visible (bottom-left, e.g. "MEW", "SV2a"), and the English set name if you know it. Language "en", "ja" or "zh"
(Chinese; anything else "other"). Variant: reverse_holo only if the whole card body outside the art is holofoil.

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
    language: Literal["en", "ja", "zh", "other"]
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


GENERIC_NAME = re.compile(r"(image|photo|img|file)?\s*(\(\d+\))?|[0-9a-f-]{8,}", re.I)


def numbered(names):
    """Pure. True when file names say the shooting order ('IMG_4521.jpeg', '21_front.jpg'). False for phone and
    browser names that don't ('image.jpg', 'image (1).jpg', random ids, the same name twice): then the order you
    picked the photos in is used."""
    stems = [os.path.splitext(os.path.basename(n))[0].strip() for n in names]
    return len(set(stems)) == len(stems) and not any(GENERIC_NAME.fullmatch(s) for s in stems)


def _natural(name):
    """'IMG_2.jpg' < 'IMG_10.jpg'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def capture_time(im):
    """Seconds-resolution sortable capture time from EXIF ('2026:11:19 18:53:07' + sub-second '650'), or None.
    iPhone photos keep it through AirDrop; messaging apps and screenshots usually don't."""
    try:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        t = sub.get(36867) or sub.get(36868) or ex.get(306)
        return f"{t}.{str(sub.get(37521) or sub.get(37520) or '0').strip():0<3}" if t else None
    except Exception:
        return None


def _open(im):
    return Image.open(io.BytesIO(im)) if isinstance(im, (bytes, bytearray)) else im


BACK_REF = os.path.join(os.path.dirname(__file__), "card_back_en.jpg")   # the English card back, straight-on
BACK_MIN = 15   # matched points: real backs 34-1000, fronts 0-4 (46 real photos)


@lru_cache(maxsize=1)
def _back_ref():
    import cv2
    import numpy as np
    orb = cv2.ORB_create(2000)
    k, d = orb.detectAndCompute(np.array(Image.open(BACK_REF).convert("L")), None)
    return orb, k, d


def back_points(im):
    """How many distinctive points of the English card back (logo letters, Poké Ball) appear in the photo in the
    right geometric arrangement (ORB features + RANSAC homography). Works at any angle, crop, glare or sleeve
    and doesn't need the card's outline (a dark blue border on a dark mat hides it)."""
    import cv2
    import numpy as np
    orb, rk, rd = _back_ref()
    g = ImageOps.exif_transpose(im).convert("L")
    g.thumbnail((1000, 1000))
    k, d = orb.detectAndCompute(np.array(g), None)
    if d is None or len(k) < 10:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d, rd, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return 0
    _, mask = cv2.findHomography(np.float32([k[m.queryIdx].pt for m in good]),
                                 np.float32([rk[m.trainIdx].pt for m in good]), cv2.RANSAC, 8.0)
    return int(mask.sum()) if mask is not None else 0


def back_scores(ims):
    """(scores, flags) per photo: higher score = more like a card back; flags = is it a back.
    English backs: matched points of the card-back design (exact). When that doesn't find exactly half backs
    (Japanese backs are another design), the older similarity rule decides: backs look like each other while
    fronts all differ; with 2 photos the bluer one, and when neither is clearly blue the first is the front."""
    pts = [back_points(im) for im in ims]
    flags = [p >= BACK_MIN for p in pts]
    if len(ims) >= 2 and sum(flags) == len(ims) // 2:
        return [float(p) for p in pts], flags
    sim_scores, sim_flags = _similar_backs(ims)
    return [p + s / 1000 for p, s in zip(pts, sim_scores)], sim_flags


def _similar_backs(ims):
    import numpy as np
    n = len(ims)
    rgb = [ImageOps.exif_transpose(im).convert("RGB") for im in ims]
    if n < 4:
        blue = []
        for im in rgb:
            w, h = im.size
            a = np.asarray(im.crop((w // 8, h // 8, w - w // 8, h - h // 8)).resize((48, 64))).astype(int)
            blue.append(float(((a[..., 2] > a[..., 0] + 15) & (a[..., 2] >= a[..., 1])).mean()))
        if n == 2 and max(blue) > 0.32 and min(blue) < 0.22:   # clearly one back: that one
            return blue, [b == max(blue) for b in blue]
        return [float(i % 2) for i in range(n)], [i % 2 == 1 for i in range(n)]   # unclear: front first
    vecs = []
    for im in rgb:
        w, h = im.size
        im = im.crop((w // 8, h // 8, w - w // 8, h - h // 8)).resize((24, 32))  # drop background edges
        v = np.asarray(im, dtype=float).ravel()
        vecs.append((v - v.mean()) / (v.std() or 1))                                # ignore brightness / exposure
    v = np.array(vecs)
    dist = np.sqrt(((v[:, None, :] - v[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(dist, np.inf)
    k = n // 2 - 1                                   # each back has k other backs to look like
    near = np.sort(dist, axis=1)[:, :k].mean(1)
    backs = set(np.argsort(near)[: n // 2].tolist())
    return [float(-x) for x in near], [i in backs for i in range(n)]


def order_pairs(seq, score, flag):
    """Pure. seq: photo indices in shooting order; score/flag: {index: back-likeness, detector says back}.
    Photos are shot card by card, front-then-back or back-then-front (both happen in one batch), so neighbours
    pair up (photos 1+2, 3+4, ...) and inside each pair the more back-like photo is the back: a relative call,
    so a sleeve or warm light that fools the detector on one photo can't break the pairing.
    Returns (pairs [(front, back)], how) or (None, reason) when the photos weren't shot card by card
    (several neighbour pairs hold two clear fronts or two clear backs, e.g. all fronts first)."""
    chunks = [(seq[j], seq[j + 1]) for j in range(0, len(seq) - 1, 2)]
    odd = sum(flag[a] == flag[b] for a, b in chunks)
    if odd > max(0, len(chunks) // 6):   # more than a stray misjudged photo
        return None, f"{odd} of {len(chunks)} neighbouring pairs aren't one front + one back"
    return [(a, b) if score[b] >= score[a] else (b, a) for a, b in chunks], "neighbouring photos paired"


def pair_photos_info(named, saved=None):
    """named: [(filename, image bytes or PIL image)] in any order (uploads arrive shuffled; AirDrop renames).
    Returns (pairs [(front_index, back_index)], how): shooting order from the photos' capture times (else
    filenames), then neighbours paired; if the photos weren't shot card by card, fronts and backs are each kept
    in shooting order and matched up."""
    ims = [_open(im) for _, im in named]
    times = [capture_time(im) for im in ims]
    timed = all(times) and len(set(times)) == len(times)
    src = "capture time"
    if not timed and saved and all(saved) and len(set(saved)) == len(saved):   # when each was saved on the phone
        times, timed, src = list(saved), True, "when the photos were saved on your phone"
    by_name = not timed and numbered([n for n, _ in named])
    seq = sorted(range(len(named)), key=(lambda i: times[i]) if timed else (lambda i: _natural(named[i][0])) if by_name
                 else (lambda i: i))
    src = src if timed else "file names (no capture time in the photos)" if by_name else \
        "the order you picked them in (no capture time or numbered names)"
    if len(seq) < 2:
        return [], src
    scores, flags = back_scores([ims[i] for i in seq])
    score = {seq[j]: scores[j] for j in range(len(seq))}
    flag = {seq[j]: flags[j] for j in range(len(seq))}
    pairs, how = order_pairs(seq, score, flag)
    if pairs is None:   # e.g. all fronts first, then all backs: the n-th front goes with the n-th back
        fronts = [i for i in seq if not flag[i]]
        backs = [i for i in seq if flag[i]]
        return list(zip(fronts, backs)), f"{src}; {how}, so fronts and backs were matched in order — check them"
    return pairs, f"{src}, {how}"


def pair_photos(named):
    """[(front_index, back_index)] — see pair_photos_info."""
    return pair_photos_info(named)[0]


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
    """Card backs are the one shared design: matched back-design points first (exact for English backs), then a
    blue-dominant centre (Japanese backs; on real listing photos backs score 0.42-0.67 bluish pixels and fronts
    incl. pink / red / green cards at most 0.22)."""
    import numpy as np
    try:
        if back_points(im) >= BACK_MIN:
            return True
    except Exception:
        pass
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

NUMBER = re.compile(r"(?<![\d/])([A-Z]{0,3} ?\d{1,3})\s*/\s*(\d{2,3})")   # '201/165SAR', '109/1090' (stray mark) -> 109/109
SET_CODE = re.compile(r"^(?:[A-Z]{2,4}|(?i:SV|S|SM)\d{1,2}[a-zA-Z]?)$")   # 'MEW', 'SSP', 'SV2a'/'sv2a'; not 'from'
NOT_CODES = {"HP", "PSA", "BGS", "CGC", "SGC", "TAG", "ACE", "MT", "NM", "GEM", "MINT", "LP", "MP",
             "GAME", "FREAK", "INC", "LLC", "TM", "LTD", "CO", "TEAM", "GAM", "RULE"}   # copyright / rule-box words
GRADERS = re.compile(r"\b(PSA|BGS|BECKETT|CGC|SGC|TAG|ACE)\b", re.I)
# A slab label's grader word: not the 'TAG TEAM' banner or 'ACE SPEC' / 'Ace Trainer' printed on raw cards
SLAB = re.compile(r"\b(PSA|BGS|BECKETT|CGC|SGC|TAG|ACE)\b(?!\s*(?:TEAM|SPEC|TRAINER))", re.I)
GRADE = re.compile(r"\b(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b")
JA = re.compile(r"[぀-ヿ一-鿿]")
KANA = re.compile(r"[぀-ヿ]")   # hiragana/katakana: on every Japanese card, never on a Chinese one
# Card-type labels and evolution lines that sit near the name ('BASIC', 'STAGE2', 'Evolves from…', 'たね', '…から進化',
# Chinese '基础' / '1阶进化')
STAGE = re.compile(r"^\s*(BASIC|STAGE\s*\w?|Evolves\s+from|TRA\w{0,2}NER|ITEM|SUPP?[O0D]RTER|STADIUM|ENERGY|TOOL|"
                   r"たね|1進化|2進化|基础|基礎|[12][阶階][进進]化)\b|[進进]化$|^\s*Special(\s+Energy)?(\s+Card)?\s*$", re.I)
# copyright / illustrator lines are never the name (an upside-down read put '©2014 Pokemon.' at the top)
FOOTER = re.compile(r"©|Pok[eé]mon\s*/|Nintendo|Creatures|GAME\s*FREAK|Wizards|Illus", re.I)
# DP/Platinum level ('Pikachu Lv.18' read as 'Pikachuv.18', 'Arceusw.10o')
LEVEL = re.compile(r"\s*[LlIi1|]?\s*[VvWw]\s*[.,]\s*\w{1,4}$")
YEAR = re.compile(r"(?<!\d)(199\d|20[0-3]\d)(?!\d)")
COPYRIGHT = re.compile(r"©|Pok.mon|Nintendo|Creatures|FREAK|Wizards", re.I)   # loose: '2014 Pokamon' still counts
# Chinese promo-style numbers ('0207/07'): 4-digit local number, read only for Chinese cards
ZH_NUMBER = re.compile(r"(?<![\d/])(\d{1,4})\s*/\s*(\d{2,3})(?!\d)")
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


def _lines(img, rot=0, crop=None, scale=1, left=1.0, hi=False, right=False):
    """[(text, cx, cy, h)] with coords normalised 0-1. img: path or RGB array; rot: degrees;
    crop: (top, bottom) fractions; left: keep this fraction of the width (from the left, or from the right
    when right=True)."""
    import numpy as np
    im = Image.fromarray(img) if not isinstance(img, (str, os.PathLike)) else ImageOps.exif_transpose(Image.open(img)).convert("RGB")
    im.thumbnail((2400, 2400))
    if rot:
        im = im.rotate(rot, expand=True)
    if crop or left < 1:
        c = crop or (0, 1)
        x0, x1 = (int(im.width * (1 - left)), im.width) if right else (0, int(im.width * left))
        im = im.crop((x0, int(im.height * c[0]), x1, int(im.height * c[1])))
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


_DIGIT_LIKE = str.maketrans({"O": "0", "Q": "0", "D": "0", "o": "0", "I": "1", "l": "1", "|": "1", "!": "1",
                             "S": "5", "s": "5", "B": "8", "Z": "2"})


def digits_fixed(t):
    """Pure. A digit look-alike between digits or next to '/' is that digit: '1S6/128' -> '156/128'
    (it was read as 'S6/128', i.e. card 006 instead of 156). 'TG05/TG30' and 'SV2a' are left alone."""
    return re.sub(r"(?<=\d)[OQDoIl|!SsBZ]+(?=\d|/)|(?<=/)[OQDoIl|!SsBZ]+(?=\d)",
                  lambda m: m[0].translate(_DIGIT_LIKE), t)


def parse_lines(lines):
    """Pure. lines: [(text, cx, cy, h)] -> dict(number, set_codes, name, language, graded, grader, grade, cert)."""
    texts = [t for t, *_ in lines]
    num_of = lambda ls: [(m[1].replace(" ", "") + "/" + m[2], cy) for t, _, cy, _ in ls
                         if "HP" not in t.upper() for m in [NUMBER.search(digits_fixed(t))] if m]
    # a number read in one piece wins; joining neighbours ('199/' + '165') only when none was ('33/34' + a stray
    # '3' made '33/343')
    nums = num_of(lines) or num_of([(a[0] + b[0], b[1], b[2], b[3]) for a, b in zip(lines, lines[1:])])
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
    # names: 3+ letters in the line ('Ho-Oh', 'Mr. Mime', 'Porygon-Z'), or 2+ Japanese/Chinese characters ('喵喵')
    namish = lambda t: not re.match(r"^\W*HP\s*\d", t, re.I) and (
        (len(re.findall(r"[A-Za-z]", t)) >= 3 and re.match(r"^\W*[A-Za-z]", t))
        or re.search(r"[A-Za-z぀-ヿ一-鿿]{3,}|[぀-ヿ一-鿿]{2,}", t))
    top = [l for l in lines if l[2] < 0.3 and not GRADERS.search(l[0]) and not FOOTER.search(l[0])
           and namish(l[0]) and not STAGE.search(l[0])]
    banner = any(l[2] < 0.3 and re.search(r"ENERGY|TRA\w{0,2}NER|SUPP?[O0D]RTER|STADIUM|ITEM", l[0], re.I)
                 for l in lines)
    if not top and banner:   # old trainer / energy cards: 'ENERGY' / 'TRAINER' banner on top, the name is the first
        # text row under the picture ('Potion' 'Energy' | 'Special Energy Card'), read word by word: join that row.
        # Only with that banner: a Pokémon whose name is lost in holo foil would get 'Flower Pokémon. Length…'
        mid = sorted((l for l in lines if 0.3 <= l[2] < 0.88 and not STAGE.search(l[0]) and not FOOTER.search(l[0])
                      and re.search(r"[A-Za-z]{2,}", l[0])), key=lambda l: l[2])
        if mid:
            row = sorted((l for l in mid if abs(l[2] - mid[0][2]) < 0.012), key=lambda l: l[1])
            text = " ".join(l[0] for l in row)
            if len(re.findall(r"[A-Za-z]", text)) >= 4 and len(text.split()) <= 4:
                top = [(text, row[0][1], row[0][2], max(l[3] for l in row))]
    name = ""
    if top:
        best = max(top, key=lambda l: l[3])
        # a long name wraps onto two lines ('Moltres&' / 'Zapdos&Articuno'): join the neighbour that continues it
        parts = [best] + [l for l in top if l is not best and abs(l[2] - best[2]) < 0.05 and abs(l[1] - best[1]) < 0.3
                          and any(x[0].endswith("&") or x[0].startswith("&") for x in (l, best))]
        name = " ".join(l[0] for l in sorted(parts, key=lambda l: l[2]))
        name = re.sub(r"\s*&\s*", " & ", re.sub(r"\bHP\s*\d+\b", "", name)).strip()
        name = LEVEL.sub("", name).strip() or name
        name = re.sub(r"(?<=[A-Za-z])\s*\d{2,3}\s*[A-Z]?$", "", name).strip() or name   # HP glued on: 'Morpekoex180C'
        name = re.sub(r"(?<=[A-Za-z]-)0\b|(?<=[a-z])0(?=[a-z])", "o", name)               # 'Kommo-0' -> 'Kommo-o'
    graded = next((SLAB.search(t) for t in texts if SLAB.search(t)), None)
    grader = grade = cert = None
    if graded:
        grader = {"BECKETT": "BGS"}.get(graded[1].upper(), graded[1].upper())
        for i, t in enumerate(texts):
            u = t.upper()
            if GRADE.search(u) and (re.search(r"MINT|MT|GEM|PRISTINE|NM|GRADE", u) or (i and SLAB.search(texts[i - 1]))):
                grade = GRADE.search(u)[1]
                break
        cert = next((m[0] for t in texts for m in [re.search(r"\b\d{7,10}\b", t)] if m), None)
        if not (grade or cert):   # a grader word with no grade and no cert number is not a slab label
            graded = grader = None
    # Japanese only if the name is, or several lines are: one stray misread character shouldn't flip it
    cjk = sum(bool(JA.search(t)) for t in texts)
    ja = bool(JA.search(name)) or cjk >= 3
    # Chinese: several lines of those characters and no kana anywhere (Japanese attack text always has some);
    # one stray character in an English read ('Gallade 四') stays English, minus the stray character
    # Chinese also needs a Chinese name: OCR noise from an e-Reader dot strip once made 'Golem ex' Chinese
    lang = "en" if not ja else "ja" if any(KANA.search(t) for t in texts) \
        else "zh" if cjk >= 3 and JA.search(name) else "en"
    if lang == "en" and JA.search(name):
        name = " ".join(JA.sub("", name).split())
    # copyright year (latest one: '©1995, 96, 98 Nintendo ©1999 Wizards' -> 1999): tells reprinted numbers apart
    years = [int(y) for t, _, cy, _ in lines if cy > 0.8 and COPYRIGHT.search(t) for y in YEAR.findall(t)]
    return {"number": number, "set_codes": codes, "name": name, "graded": bool(graded), "grader": grader,
            "grade": grade, "cert": cert, "language": lang, "year": max(years) if years else None}


class OcrRead(CardRead):
    """CardRead plus the catalog cards the OCR backend already resolved (first = best match)."""
    candidate_ids: Optional[list[str]] = None


# Characters OCR mixes up in the italic number font -> one canonical form ('TGBO' reads as 'TG30').
_LOOKALIKE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "!": "1",
                            "B": "3", "8": "3", "Z": "2", "S": "5"})
# Where the collector number is printed on a straightened card: (top, bottom, left, right) fractions.
NUMBER_BANDS = [(0.94, 0.98, 0.12, 0.36), (0.935, 0.985, 0.10, 0.40),   # Sword & Shield / Scarlet & Violet
                (0.935, 0.985, 0.62, 0.98),                            # older cards: bottom right
                (0.86, 0.915, 0.68, 0.97)]                             # e-Reader era: above the dot strip


def printed_full(c):
    """The number as printed: '199/165', gallery cards 'TG03/TG30', 'GG34/GG70'; promos just the id."""
    local, total = c.get("localId") or "", ((c.get("set") or {}).get("cardCount") or {}).get("official")
    m = re.match(r"^([A-Za-z]*)(\d+)$", local)
    if not (m and total) or ((c.get("set") or {}).get("id") or "").endswith("p"):   # promo sets: bare 'SM210'
        return local
    return f"{local}/{m[1]}{total}" if m[1] else f"{local}/{total}"


def _canon(s):
    return s.upper().replace(" ", "").translate(_LOOKALIKE)


def number_similarity(read, printed):
    """Pure. 0-1: how well the printed number appears somewhere in the OCR text, ignoring look-alike characters.
    Bare promo numbers ('148', '034', 'XY04', 'RC18') only count as a whole word, never inside other text."""
    from difflib import SequenceMatcher
    r, p = _canon(read), _canon(printed)
    if not p or not r:
        return 0.0
    if "/" not in p and len(p) < 5:
        bare = lambda s: s.lstrip("0") or "0"
        words = re.findall(r"[A-Z0-9]+", re.sub(r"[+×X-]\d+", " ", read.upper()))   # not damage '+10' / '×2' / '-20'
        return 1.0 if len(p) >= 2 and any(bare(_canon(w)) == bare(p) for w in words) else 0.0
    n = len(p)
    return max(SequenceMatcher(None, r[i:i + n], p).ratio() for i in range(max(1, len(r) - n + 1)))


def number_scores(cards, reads):
    """Pure. {card id: 0-1} how well each card's printed number appears in the OCR reads (< 0.6 counts as 0)."""
    def forms(c):   # promo ids ('SM210', 'SWSH039') are printed bare even when TCGdex counts the promo set
        local = c.get("localId") or ""
        return [printed_full(c)] + ([local] if re.match(r"^[A-Za-z]{1,4}\d{2,}$", local) else [])
    score = {c["id"]: max((number_similarity(t, p) for t in reads for p in forms(c)), default=0.0) for c in cards}
    return {k: (v if v >= 0.6 else 0.0) for k, v in score.items()}   # weak partial matches are noise


def pick_card(cards, reads, year=None):
    """Pure. cards: catalog cards sharing the OCR'd name; reads: OCR strings from the number bands.
    Returns (best or None, candidates best-first). Auto-picks only a clear winner; cards whose number fits equally
    well are told apart by the copyright year (released that year or the next) when exactly one fits it."""
    from .catalog import release

    score = number_scores(cards, reads)
    ranked = sorted(cards, key=lambda c: (round(score[c["id"]], 2), release(c)), reverse=True)
    if not ranked:
        return None, []
    top, second = score[ranked[0]["id"]], (score[ranked[1]["id"]] if len(ranked) > 1 else 0.0)
    if top >= 0.75 and top - second >= 0.15:
        return ranked[0], [ranked[0]]
    if top >= 0.75 and year:
        tied = [c for c in ranked if score[c["id"]] >= top - 0.15]
        dated = by_year(tied, year)
        if len(dated) == 1:
            return dated[0], dated
    return None, ranked


NAME_SUFFIX = re.compile(r"\s*(ex|EX|V|VMAX|VSTAR|GX)$")


_SMALL = str.maketrans("ツヤユヨアイウエオ", "ッャュョァィゥェォ")


def small_kana_variants(name):
    """Pure. OCR reads small kana as full size ('ブニャツト' for 'ブニャット' = Purugly): the name with one of them
    made small at a time (only tried after the name as read found nothing)."""
    return [name[:i] + ch.translate(_SMALL) + name[i + 1:] for i, ch in enumerate(name)
            if ch.translate(_SMALL) != ch and i > 0]


def rank_briefs(briefs, reads):
    """Pure. Catalog briefs whose number appears in what OCR read first (longest match first), so the right card
    survives the fetch limit: 'Mew' has 166 cards and GG10 was number 120-something."""
    text = _canon(" ".join(reads))
    def hit(b):
        local = _canon(b.get("localId") or "").lstrip("0") or "0"
        return len(local) if len(local) >= 2 and local in text else 0
    return sorted(briefs, key=hit, reverse=True)


def _name_cards(lang, name, reads=()):
    """Catalog cards for an OCR'd name: as read, then without an ex/V suffix ('Tinkatonex'), then the first word
    ('Moltres' for 'Moltres & Zapdos & Articuno'). reads: OCR text that may hold the number (ranks the cards)."""
    from . import catalog
    # XY Mega cards print 'M' as a glyph glued to the name: 'MPidgeot' is TCGdex's 'M Pidgeot EX'
    mega = re.sub(r"^M(?=[A-Z][a-z])", "M ", name)
    for q in dict.fromkeys([name, name.replace("'", "’"), mega, NAME_SUFFIX.sub("", name), NAME_SUFFIX.sub("", mega),
                            re.split(r"[\s&]+", name)[0], re.split(r"[\s&'’]+", name)[0]] + small_kana_variants(name)):
        if len(q) >= 3 or (lang != "en" and len(q) >= 2):
            briefs = catalog.search(lang, q)
            if briefs:
                return catalog._cards(lang, rank_briefs(briefs, reads), limit=80)
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


def number_variants(number):
    """Pure. The number as read, then with OCR debris removed, only tried when the reading finds no card:
    '88/950' -> '88/95' (a mark glued to the total), 'ORE073/088' -> '073/088' (letters glued in front)."""
    local, _, total = number.partition("/")
    out = [number]
    bare = re.sub(r"^[A-Z]+(?=\d)", "", local)
    for lo in dict.fromkeys([local, bare]):
        short = total[:2] if len(total) == 3 and lo.isdigit() and int(lo) <= int(total[:2]) * 1.6 else total
        for to in dict.fromkeys([total, short]):
            out.append(f"{lo}/{to}")
    return list(dict.fromkeys(out))


def _alnum(s):
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]", "", (s or "").lower().replace("é", "e"))


def narrow(cands, name, year):
    """Pure. Several catalog cards fit the number: keep those whose name matches what was read
    ('Raichu & AlolanRaichu' vs another set's card 241/236; Scarlet ex vs Violet ex 092/078), then those released
    in the copyright year or the year after (Ultra Ball 102/108: 2016 XY Evolutions, not 2012 Dark Explorers).
    A filter that would leave nothing is skipped."""
    from difflib import SequenceMatcher
    from .catalog import release
    if len(cands) < 2:
        return cands
    if name:
        sc = {c["id"]: SequenceMatcher(None, _alnum(name), _alnum(c.get("name"))).ratio() for c in cands}
        best = max(sc.values())
        if best >= 0.6:
            cands = [c for c in cands if sc[c["id"]] >= best - 0.05]
    if year and len(cands) > 1:
        cands = by_year(cands, year) or cands
    return cands


def by_year(cands, year):
    """Pure-ish. Cards from the earliest release on or after the copyright year (within 7 years: WotC-era cards
    print '©1995-2001' and came out in 2001). [] when none fits."""
    from .catalog import release
    yrs = {c["id"]: int(release(c)[:4]) for c in cands if release(c)[:4].isdigit()}
    after = [c for c in cands if 0 <= yrs.get(c["id"], -99) - year <= 7]
    if not after:
        return []
    first = min(yrs[c["id"]] for c in after)
    return [c for c in after if yrs[c["id"]] == first]


def _look(img):
    """Unit vector of a straightened card's overall look (32x44 colour thumbnail, mean removed)."""
    import cv2
    import numpy as np
    v = cv2.resize(np.ascontiguousarray(img), (32, 44), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    v -= v.mean()
    return v / (np.linalg.norm(v) + 1e-6)


@lru_cache(maxsize=4096)
def _ref_look(url):
    import io
    import numpy as np
    from . import ebay
    r = ebay._http.get(url, timeout=15)
    r.raise_for_status()
    return _look(np.array(Image.open(io.BytesIO(r.content)).convert("RGB")))


def art_pick(card, cands, margin=0.08, min_top=0.5):
    """Several cards still fit (same name and number, e.g. Base Set vs Triumphant Water Energy 98/102): compare the
    straightened photo with each candidate's catalog picture. Returns [winner] when one is clearly closest, else
    the candidates closest first (identical-art reprints stay a choice)."""
    from concurrent.futures import ThreadPoolExecutor
    from .catalog import image
    me = _look(card)
    def score(c):
        try:
            return float(me @ _ref_look(image(c, "low")))
        except Exception:
            return -1.0
    with ThreadPoolExecutor(6) as ex:
        sc = dict(zip([c["id"] for c in cands], ex.map(score, cands)))
    ranked = sorted(cands, key=lambda c: sc[c["id"]], reverse=True)
    if not ranked:
        return ranked
    top, second = sc[ranked[0]["id"]], (sc[ranked[1]["id"]] if len(ranked) > 1 else -1.0)
    return [ranked[0]] if top >= min_top and top - second >= margin else ranked


def check_pick(card, pick, lang, name, reads):
    """A single pick whose catalog picture looks only weakly like the photo (< 0.6; correct picks on real photos
    score 0.43-0.92 because of glare, so this is only a trigger) is compared with the other cards of that name.
    One that looks much more like the photo (+0.25) replaces it if its number also fits what was read, else both
    are offered. POP 2 Shroomish 3/17 read as '3/172' had matched Brilliant Stars 003/172 (0.48 vs 0.90)."""
    from .catalog import image
    try:
        mine = float(_look(card) @ _ref_look(image(pick, "low")))
    except Exception:
        return [pick]
    if mine >= 0.6 or not name:
        return [pick]
    others = [c for c in _name_cards(lang, name, reads) if c["id"] != pick["id"]]
    if not others or len(others) >= 80:
        return [pick]
    ranked = art_pick(card, others, margin=0)
    try:
        best = float(_look(card) @ _ref_look(image(ranked[0], "low")))
    except Exception:
        return [pick]
    if best < mine + 0.25:
        return [pick]
    fits = number_scores([ranked[0]], reads)[ranked[0]["id"]] >= 0.6
    return [ranked[0]] if fits else [ranked[0], pick]


def one_char_off(a, b):
    """Pure. Same length, exactly one character different ('135' / '155', '10' / '16'), leading zeros ignored."""
    a, b = a.upper().lstrip("0"), b.upper().lstrip("0")
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def digit_siblings(card, pick, lang, name):
    """The same Pokémon has a card whose number is one character off the one read: OCR confuses 3/5, 0/6/9, 1/7 in
    the small italic print, and the misread lands on a real card. The picture decides between them; when it can't
    tell, both are offered. Only the sibling cards are fetched (one catalog search otherwise)."""
    from . import catalog
    local = pick.get("localId") or ""
    size = lambda set_id: (catalog.sets_index(lang).get(set_id) or {}).get("cardCount", {}).get("official")
    mine = size((pick.get("set") or {}).get("id"))
    # same set size too: the '/91' was read, so Obsidian Flames 209/197 is no sibling of Paldean Fates 207/91
    briefs = [b for b in catalog.search(lang, name) if b["id"] != pick["id"]
              and one_char_off(b.get("localId") or "", local) and size(b["id"].rsplit("-", 1)[0]) == mine]
    if not briefs:
        return [pick]
    sibs = catalog._cards(lang, briefs, limit=10)
    return art_pick(card, [pick] + sibs) if sibs else [pick]


def _resolve(card, straight, quick=False):
    import numpy as np
    from . import catalog
    lines = _lines(card)
    p = parse_lines(lines)
    # straightened upside down, or no card found: try rotations. Not when a number was read: energy and some
    # trainer cards have no name at the top, and a rotated read then made a name out of noise ('861/EEZ')
    if not p["name"] and not p["number"] and not quick:
        for rot in (180, 90, 270):
            lines2 = _lines(card, rot=rot)
            p2 = parse_lines(lines2)
            if p2["name"] and p2["number"]:
                p, lines, card = p2, lines2, np.ascontiguousarray(np.rot90(card, rot // 90))   # same turn as PIL's rotate()
                break
    lang, number = p["language"], p["number"] or ""
    if lang == "zh":   # Simplified Chinese: TCGdex has no such cards, so English name + printed number, no match
        num = next((f"{m[1]}/{m[2]}" for t in reversed(bottom_reads(lines, straight)) for m in [ZH_NUMBER.search(t)] if m),
                   number)
        return OcrRead(
            pair_ok=True, language="zh", name_en=catalog.english_from_chinese(p["name"]) or p["name"] or "Unknown",
            name_printed=p["name"], number=num, set_code=None, set_name_en=None, variant="normal",
            graded=p["graded"], grader=p["grader"], grade=p["grade"], cert_number=p["cert"],
            condition=None if p["graded"] else "NM", condition_notes=[], condition_summary=GENERIC_SUMMARY,
            uncertain_fields=["photo pairing (not checked by OCR)", "Chinese card: not in the catalogue, check the price"]
            + ([] if num else ["number"]), candidate_ids=[])
    cands = []
    if number:                                        # full '199/165' read: set code + count narrow it down
        for num in number_variants(number):
            for code in p["set_codes"] + [None]:
                cands = catalog.find(lang, p["name"], num, code)
                if cands:
                    break
            if cands:
                number = num
                break
    if not cands and not p["name"]:   # no name read (holo foil, glare) but a promo / gallery code at the bottom
        # ('BW83', 'SM210', 'XY04', 'TG05'): look the code itself up
        code = next((m[1] + m[2] for t in reversed(bottom_reads(lines, straight))
                     for m in [re.search(r"\b([A-Z]{2,5})\s?(\d{2,3}[a-z]?)\b", digits_fixed(t))]
                     if m and m[1] not in NOT_CODES), None)
        if code:
            cands = [c for c in catalog._cards(lang, catalog.search(lang, "", code), limit=40)]
            if len(cands) > 1:
                cands = narrow(cands, "", p["year"])
                if straight and 1 < len(cands) <= 15:
                    cands = art_pick(card, cands)
            number = code if cands else number
    if not cands and p["name"]:                        # no plain number: match what was read within this name's cards
        # cheapest first, stopping at a clear winner: the first read's own bottom lines (free), then the fixed number
        # bands (recognition only, ~0.1 s), then a fine-detail read of the bottom strip (3-5 s, rarely needed)
        reads = bottom_reads(lines, straight) + ([number] if number else [])
        cards = _name_cards(lang, p["name"], reads)
        best, cands = pick_card(cards, reads, p["year"])
        if not best and straight:
            H, W = card.shape[:2]
            reads += [t for y0, y1, x0, x1 in NUMBER_BANDS
                      for t in (_engine("rec")(np.ascontiguousarray(card[int(H * y0):int(H * y1), int(W * x0):int(W * x1)]),
                                               use_det=False, use_cls=False, use_rec=True).txts or [])]
            best, cands = pick_card(cards, reads, p["year"])
        if not best and not quick:
            reads += [t for t, *_ in _lines(card, crop=(0.88, 1.0), left=0.55, scale=2, hi=True)]
            best, cands = pick_card(cards, reads, p["year"])
        if not best and not quick:   # XY / BW / EX / e-card print the number bottom right; e-Reader cards (EX
            # Ruby & Sapphire to Emerald, e-card) print it higher, above the dot strip (~88 % down)
            hi_r = [t for t, *_ in _lines(card, crop=(0.82, 1.0), left=0.6, scale=2, hi=True, right=True)]
            reads += hi_r
            p["year"] = p["year"] or max((int(y) for t in hi_r if COPYRIGHT.search(t) for y in YEAR.findall(t)),
                                         default=None)
            best, cands = pick_card(cards, reads, p["year"])
        if best:
            cands = [best]
        else:   # cards whose printed number was clearly read: only these may be told apart by their picture
            sc = number_scores(cands, reads)
            top = max(sc.values(), default=0)
            fit = [c for c in cands if top >= 0.75 and sc[c["id"]] >= top - 0.15]
            if fit and straight and 1 < len(fit) <= 15:
                fit = art_pick(card, fit)
            if len(fit) == 1:
                cands = fit
            else:   # no number (tiny or worn print on old cards), or a number several cards share: every printing
                # has its own artwork, so the picture decides, but only against this name's complete card list
                # (under 80, so the right card can't be missing), with a stricter margin, agreeing with the
                # copyright year if one was read
                yr = lambda c: int(catalog.release(c)[:4]) if catalog.release(c)[:4].isdigit() else -99
                era = [c for c in cands if p["year"] and 0 <= yr(c) - p["year"] <= 2]
                picked = art_pick(card, cards, margin=0.10, min_top=0.6) \
                    if straight and 1 < len(cards) < 80 else []
                if len(picked) == 1 and (not era or picked[0] in era):
                    cands = picked
                else:
                    first = fit or era
                    cands = (first + [c for c in cands if c not in first])[:15]
    elif len(cands) > 1:   # number lookup matched several sets: the name and copyright year decide
        cands = narrow(cands, p["name"], p["year"])
        if straight and 1 < len(cands) <= 15:   # still several: whichever catalog picture the photo looks like
            cands = art_pick(card, cands)
    if straight and len(cands) == 1 and p["name"]:   # a pick that doesn't look like its picture gets a second look
        cands = check_pick(card, cands[0], lang, p["name"], bottom_reads(lines, straight) + [number])
    if straight and len(cands) == 1 and p["name"]:   # one misread digit lands on a sibling ('155' read as '135')
        cands = digit_siblings(card, cands[0], lang, p["name"])
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
