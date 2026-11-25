"""Turn photos added to the iCloud "pokemon" Shared Album into drafts, without opening the app.

Run: .venv\\Scripts\\pythonw.exe watch_photos.py   (README: start it at logon with Task Scheduler)
Every 60 s it looks in the album's folder (Settings -> watch folder; blank = found automatically). A photo counts
once its size has stayed the same for 2 looks (iCloud may still be downloading it). New photos (by content hash)
are paired front + back like the List page does, then read, priced and saved as drafts with the same code
(listing.import_pairs) and your saved pricing / spreadsheet choices. A photo whose partner hasn't arrived yet
waits. Photos already imported are never imported again, and existing drafts are never touched.
The first run only notes what's already in the album (those cards are probably listed already);
run once with --import-existing to turn them into drafts too.
"""
import datetime
import hashlib
import logging
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core import db, listing, vision  # noqa: E402

EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
LOG = db.DATA / "watch.log"
log = logging.getLogger("watch_photos")
UNMATCHED_WAIT = 15 * 60   # a pair with no card back found waits this long for more photos, then goes in anyway


def find_folder():
    """The album folder: the setting, else a folder named 'pokemon' where iCloud for Windows keeps photos."""
    if db.setting("watch_folder"):
        return Path(db.setting("watch_folder"))
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    for root in (home / "iCloudPhotos", home / "Pictures" / "iCloud Photos", home / "OneDrive" / "Pictures" / "iCloud Photos",
                 home / "iCloud Photos", home / "iCloud Photos (2)"):   # the 'pokemon' Shared Album, once synced
        if root.is_dir():
            hit = next((p for p in root.rglob("*") if p.is_dir() and p.name.lower() == "pokemon"), None)
            if hit:
                return hit
    if (home / "iCloudDrive" / "pokemon").is_dir():   # else the iCloud Drive folder (Files app on the phone)
        return home / "iCloudDrive" / "pokemon"
    return None


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Watcher:
    """State between scans: sizes seen last time and when each waiting photo was first ready."""

    def __init__(self, folder):
        self.folder, self.sizes, self.first_ready, self.hashes, self.scans = Path(folder), {}, {}, {}, 0
        self.undeletable = set()

    def ready(self):
        """Photo files whose size is the same as at the previous scan."""
        self.scans += 1
        out, sizes = [], {}
        for p in sorted(self.folder.iterdir()) if self.folder.is_dir() else []:
            if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith("."):
                st = p.stat()
                sizes[p] = st.st_size
                if st.st_size and self.sizes.get(p) == st.st_size:
                    out.append((p, st.st_size, st.st_mtime))
        self.sizes = sizes
        return out

    def new(self, files):
        """Ready files whose content isn't in imported_photos yet: [(path, size, mtime, sha1)]. Files that already
        became a draft are deleted from the folder (the draft keeps its own copy in data/uploads)."""
        known = {r["sha1"]: r["draft_id"] for r in db.q("SELECT sha1, draft_id FROM imported_photos")}
        out, seen, gone = [], set(), 0
        for p, size, mtime in files:
            key = (p, size, mtime)
            sha = self.hashes.get(key) or self.hashes.setdefault(key, _sha1(p))
            if known.get(sha):
                if sha not in self.undeletable:
                    try:
                        p.unlink()
                        gone += 1
                    except OSError as e:
                        if getattr(e, "winerror", None) == 379:   # iCloud Shared Albums can't be deleted from Windows
                            self.undeletable.add(sha)
                            if len(self.undeletable) == 1:
                                log.info("this folder doesn't allow deleting (iCloud Shared Album): photos stay; "
                                         "delete them in the album on your phone, or use the iCloud Drive folder")
                        else:   # busy (still syncing): next scan
                            log.warning("couldn't delete %s yet: %s", p.name, e)
            elif sha not in known and sha not in seen:   # the same photo twice in the album: once
                seen.add(sha)
                out.append((p, size, mtime, sha))
        if gone:
            log.info("deleted %d photo(s) from the folder that are already drafts", gone)
        return out

    def scan(self, strategy=None, pct=None, to_sheet=None, seed=False):
        """One look at the folder. Returns the new draft ids. seed=True: note the photos there, import nothing."""
        new = self.new(self.ready())
        if not new:
            return []
        now = datetime.datetime.now().isoformat(timespec="seconds")
        rec = "INSERT OR IGNORE INTO imported_photos (sha1, filename, size, mtime, draft_id, imported_at) VALUES (?,?,?,?,?,?)"
        if seed:
            db.many(rec, [(sha, p.name, size, mtime, None, now) for p, size, mtime, sha in new])
            log.info("first run: noted %d photo(s) already in the album, imported none", len(new))
            return []
        pairs = self.pairs(new)
        if not pairs:
            return []
        done, ids = listing.import_pairs(
            [(new[f][0], new[b][0]) for f, b in pairs],
            strategy or db.setting("strategy"), float(pct if pct is not None else db.setting("pct_below")),
            bool(db.setting("sheet_add")) if to_sheet is None else to_sheet)
        rows = [(new[i][3], new[i][0].name, new[i][1], new[i][2], did, now)
                for (f, b), did in zip(pairs, ids) for i in (f, b)]
        db.many(rec, rows)
        for f, b in pairs:
            for i in (f, b):
                if any(new[i][3] == r[0] for r in rows if r[4]):   # became a draft: remove it from the folder
                    try:
                        new[i][0].unlink()
                    except OSError as e:
                        if getattr(e, "winerror", None) == 379:
                            self.undeletable.add(new[i][3])
                        else:
                            log.warning("couldn't delete %s yet: %s", new[i][0].name, e)
            self.first_ready.pop(new[f][3], None)
            self.first_ready.pop(new[b][3], None)
        log.info("%d new draft(s) from %d photo(s): %s; %d photo(s) waiting for a partner",
                 len(ids), 2 * len(pairs), done, len(new) - 2 * len(pairs))
        return ids

    def pairs(self, new):
        """[(front i, back i)] into `new`. Photos left out wait for the next scan: the odd one out (its partner
        isn't here yet), and a pair in which neither photo shows a card back (two fronts whose backs haven't
        synced) until it has waited UNMATCHED_WAIT."""
        for p, _, _, sha in new:
            self.first_ready.setdefault(sha, time.time())
        if len(new) < 2:
            return []
        pairs, how = vision.pair_photos_info([(p.name, p.read_bytes()) for p, _, _, _ in new])
        out = []
        for f, b in pairs:
            waited = time.time() - min(self.first_ready[new[f][3]], self.first_ready[new[b][3]])
            if waited >= UNMATCHED_WAIT or max(vision.back_points(vision.Image.open(new[i][0])) for i in (f, b)) \
                    >= vision.BACK_MIN:
                out.append((f, b))
        log.info("paired %d of %d photo(s) (%s)", 2 * len(out), len(new), how)
        return out


def notify(text):
    """Windows toast (no extra package: PowerShell's own notification API). Never raises."""
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
          "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          "$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode('PokeProj')) | Out-Null;"
          f"$x.Item(1).AppendChild($t.CreateTextNode('{text}')) | Out-Null;"
          "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log.warning("notification failed: %s", e)


def main(interval=60):
    logging.basicConfig(filename=LOG, level=logging.INFO, format="%(asctime)s %(message)s")
    secrets = ROOT / ".streamlit" / "secrets.toml"   # eBay keys for pricing, as the app loads them
    if secrets.exists():
        with open(secrets, "rb") as f:
            os.environ.update({k: str(v) for k, v in tomllib.load(f).items() if not isinstance(v, dict)})
    if "--import-existing" in sys.argv:
        db.set_setting("watch_seeded", True)
    w, folder = None, None
    log.info("started; watching %s", find_folder() or "nothing yet (no folder found)")
    while True:
        try:
            now_folder = find_folder()
            if now_folder != folder:
                folder, w = now_folder, Watcher(now_folder) if now_folder else None
                if folder:
                    log.info("watch folder: %s", folder)
            if w:
                seed = not db.setting("watch_seeded")
                ids = w.scan(seed=seed)
                if seed and w.scans >= 2:   # what was there at the first full look is noted: from now on, import
                    db.set_setting("watch_seeded", True)
                if ids:
                    notify(f"{len(ids)} new draft{'s' if len(ids) != 1 else ''}")
        except Exception as e:
            log.exception("scan failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
