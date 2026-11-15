"""Get a long-lived eBay PRODUCTION refresh token (~18 months) and save it in the app's local database
(data/app.db, git-ignored) — not secrets.toml, so an editor saving an old copy of secrets.toml can't overwrite it.

Run:  .venv\\Scripts\\python get_ebay_token.py
1. It prints an eBay sign-in link. Open it, sign in with your SELLER account, press "Agree".
2. eBay lands on a page saying authorisation succeeded. Copy that page's full address (URL) from the browser bar.
3. Paste it here within 5 minutes. The script swaps the code in it for tokens and saves the refresh token.
Needs EBAY_APP_ID, EBAY_CERT_ID and your RuName (developer.ebay.com -> User Tokens -> the "RuName" shown next to
your redirect URL, e.g. Your_Name-YourName-Pokepr-abcdefg). Nothing is sent anywhere except eBay."""
import base64
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

SECRETS = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
SCOPES = " ".join(["https://api.ebay.com/oauth/api_scope"] + [
    "https://api.ebay.com/oauth/api_scope/" + s for s in ("sell.inventory", "sell.account", "sell.fulfillment")])


def set_secret(text, key, value):
    """Pure. Replace (or append) `KEY = "value"` in a secrets.toml text."""
    line = f'{key} = "{value}"'
    pat = re.compile(rf"^\s*#?\s*{key}\s*=.*$", re.M)
    return pat.sub(lambda _: line, text, count=1) if pat.search(text) else text.rstrip("\n") + "\n" + line + "\n"


def main():
    text = SECRETS.read_text(encoding="utf-8")
    s = tomllib.loads(text)
    app, cert = s.get("EBAY_APP_ID"), s.get("EBAY_CERT_ID")
    if not app or not cert:
        raise SystemExit("EBAY_APP_ID / EBAY_CERT_ID missing in .streamlit/secrets.toml")
    import sys
    sys.path.insert(0, str(SECRETS.parent.parent))
    from core import db
    runame = s.get("EBAY_RUNAME") or db.setting("ebay_runame", "") or \
        input("Your RuName (developer.ebay.com -> User Tokens, next to your redirect URL): ").strip()
    print("\n1) Open this link, sign in with your eBay SELLER account and press Agree:\n")
    print(f"https://auth.ebay.com/oauth2/authorize?client_id={quote(app)}&response_type=code"
          f"&redirect_uri={quote(runame)}&scope={quote(SCOPES)}\n")
    pasted = input("2) Paste the full address of the page eBay sent you to: ").strip()
    code = (parse_qs(urlparse(pasted).query).get("code") or [unquote(pasted)])[0]
    basic = base64.b64encode(f"{app}:{cert}".encode()).decode()
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token", timeout=30,
                      headers={"Authorization": f"Basic {basic}"},
                      data={"grant_type": "authorization_code", "code": code, "redirect_uri": runame})
    if r.status_code >= 400:
        raise SystemExit(f"eBay refused the code ({r.status_code}): {r.text[:300]}\n"
                         "Codes expire after 5 minutes and work once: run the script again.")
    j = r.json()
    db.set_setting("ebay_refresh_token", j["refresh_token"])
    db.set_setting("ebay_runame", runame)
    days = int(j.get("refresh_token_expires_in", 0)) // 86400
    print(f"\nSaved the refresh token in the app database (valid ~{days} days). It takes priority over "
          f"EBAY_REFRESH_TOKEN in secrets.toml. Restart Streamlit to use it.")


if __name__ == "__main__":
    main()
