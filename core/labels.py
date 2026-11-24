"""Address labels: when something sells on eBay, make a label-sized PDF (buyer's address, your return address,
the order reference) and email it to you. Free: you add stamps as usual. Covers every eBay order not yet posted,
not only cards listed by this app. Email goes through your own mail account (SMTP_* in secrets.toml)."""
import datetime
import io
import os
import smtplib
import threading
import time
from email.message import EmailMessage

from PIL import Image, ImageDraw, ImageFont

from . import db, ebay

DPI = 300
SIZE_MM = (100, 60)   # ponytail: one fixed sticker size; make it a setting if you change label stock
FONT, BOLD = r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"
COUNTRIES = {"DE": "GERMANY", "FR": "FRANCE", "IE": "IRELAND", "US": "UNITED STATES", "AU": "AUSTRALIA", "CA": "CANADA",
             "NL": "NETHERLANDS", "BE": "BELGIUM", "ES": "SPAIN", "IT": "ITALY", "AT": "AUSTRIA", "CH": "SWITZERLAND",
             "SE": "SWEDEN", "DK": "DENMARK", "NO": "NORWAY", "FI": "FINLAND", "PL": "POLAND", "PT": "PORTUGAL",
             "NZ": "NEW ZEALAND", "JP": "JAPAN", "SG": "SINGAPORE", "HK": "HONG KONG"}
_worker = None


def address_lines(ship_to):
    """Pure. eBay shipTo -> printed address lines (name first; country in capitals last, left out for the UK)."""
    a = ship_to.get("contactAddress") or {}
    cc = a.get("countryCode") or "GB"
    lines = [ship_to.get("fullName"), a.get("addressLine1"), a.get("addressLine2"), a.get("city"),
             a.get("stateOrProvince") if cc != "GB" else None, (a.get("postalCode") or "").upper(),
             None if cc == "GB" else COUNTRIES.get(cc, cc)]
    return [str(x).strip() for x in lines if x and str(x).strip()]


def _font(path, px):
    return ImageFont.truetype(path, px)


def render(address, sender="", ref=""):
    """Pure. A PDF exactly the label's size (nothing to crop): small 'From' line, big address, small reference."""
    w, h = (round(mm / 25.4 * DPI) for mm in SIZE_MM)
    img = Image.new("L", (w, h), 255)
    g = ImageDraw.Draw(img)
    pad = round(3 / 25.4 * DPI)
    small = _font(FONT, 26)
    top = pad
    if sender:
        g.text((pad, top), "From: " + sender, font=small, fill=0)
        top += 40
        g.line((pad, top, w - pad, top), fill=0, width=2)
        top += 14
    bottom = h - pad - (34 if ref else 0)
    px = 72
    while px > 24:   # largest size where every line fits the width and all lines fit the height
        f, fb = _font(FONT, px), _font(BOLD, px)
        step = round(px * 1.18)
        if step * len(address) <= bottom - top and all(
                g.textlength(t, font=fb if i == 0 else f) <= w - 2 * pad for i, t in enumerate(address)):
            break
        px -= 2
    for i, t in enumerate(address):
        g.text((pad, top + i * step), t, font=fb if i == 0 else f, fill=0)
    if ref:
        g.text((pad, h - pad - 26), ref, font=small, fill=0)
    out = io.BytesIO()
    img.save(out, "PDF", resolution=DPI)
    return out.getvalue()


def send(subject, body, attachments):
    """Email via SMTP (default Gmail with an app password). attachments: [(filename, pdf bytes)]."""
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("email not set up: add SMTP_USER and SMTP_PASSWORD to .streamlit/secrets.toml")
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = user, os.environ.get("LABEL_TO") or user, subject
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", 465)),
                          timeout=30) as s:
        s.login(user, pw)
        s.send_message(m)


def due(orders, sent):
    """Pure. Paid, not cancelled, not yet posted orders that haven't had a label emailed."""
    return [o for o in orders if o["orderId"] not in sent
            and o.get("orderPaymentStatus") in (None, "PAID")
            and (o.get("cancelStatus") or {}).get("cancelState") != "CANCELED"
            and o.get("orderFulfillmentStatus") in ("NOT_STARTED", "IN_PROGRESS")]


def check():
    """Email a label for each new sale. Returns how many were sent; a failed email is retried next time."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    orders = ebay.orders(since)
    if db.setting("labels_sent") is None:   # first run: sales from before labels were turned on are yours to handle
        db.set_setting("labels_sent", sorted(o["orderId"] for o in orders))
    sent = set(db.setting("labels_sent"))
    n = 0
    for o in due(orders, sent):
        step = o["fulfillmentStartInstructions"][0]["shippingStep"]
        ref = f"eBay {o.get('salesRecordReference') or o['orderId']}"
        pdf = render(address_lines(step["shipTo"]), db.setting("return_address") or "", ref)
        items = "\n".join(f"- {li.get('quantity', 1)} x {li.get('title')}" + (f"  [{li['sku']}]" if li.get("sku") else "")
                          for li in o.get("lineItems", []))
        by = (o.get("lineItems") or [{}])[0].get("lineItemFulfillmentInstructions", {}).get("shipByDate", "")[:10]
        send(f"Label: {ref} sold, post by {by}" if by else f"Label: {ref} sold",
             f"Sold on eBay:\n{items}\n\nPost by: {by or 'see eBay'}\nPostage paid by buyer: £"
             f"{((o.get('pricingSummary') or {}).get('deliveryCost') or {}).get('value', '?')}\n\n"
             "The attached PDF is the address label at its exact size: print at 100% (actual size).",
             [(f"label {ref}.pdf", pdf)])
        sent.add(o["orderId"])
        db.set_setting("labels_sent", sorted(sent)[-500:])
        n += 1
    db.set_setting("label_last", {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                  "msg": f"{n} label(s) emailed" if n else "no new sales"})
    return n


def start_background(interval=120):
    """Check for new sales every `interval` s while the app runs (one thread per process), if turned on."""
    global _worker
    if _worker and _worker.is_alive():
        return _worker

    def loop():
        while True:
            if db.setting("label_auto") and os.environ.get("EBAY_REFRESH_TOKEN"):
                try:
                    check()
                except Exception as e:
                    db.set_setting("label_last", {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                                                  "msg": f"failed: {e}"[:300]})
            time.sleep(interval)

    _worker = threading.Thread(target=loop, daemon=True, name="labels")
    _worker.start()
    return _worker
