#!/usr/bin/env python3
"""
astro_downloader.py - Mirror the pCloud astrology books collection to a local drive.

Source : https://u.pcloud.link/publink/show?code=kZt3skVZjtqVNME6G04IBuulkQSpbF0S8Rv7
Folder : "astrology books-magazines" (folderid 30709259648, ~25,000 files, ~83 GB)
Target : F:\\AstroBooksCollection

Features
  * Uses the official pCloud public-link API (showpublink / getpublinkdownload).
  * Resumable: re-run any time; finished files are skipped, partial files resume
    (HTTP Range) from their .part file.
  * Incremental sync: files added/changed on pCloud later are picked up on re-run.
  * Size verification after every download, retries with exponential back-off,
    automatic pause when pCloud reports traffic/rate limits.
  * Windows-safe file names (illegal characters replaced) and long-path support.
  * Every file is recorded in an SQLite manifest (_library/library.db) that the
    companion script astro_library.py uses to build the searchable catalogue.

Usage examples
  python astro_downloader.py                  # download everything (default target F:\\AstroBooksCollection)
  python astro_downloader.py --dry-run        # only show what would be downloaded
  python astro_downloader.py --only pdf       # only PDFs
  python astro_downloader.py --workers 4      # parallel downloads (default 3)
  python astro_downloader.py --status         # progress summary from the manifest
  python astro_downloader.py --retry-failed   # retry only files that failed earlier
  python astro_downloader.py --ids-file x.txt # only the files whose ids are listed (one per line)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is required:  pip install requests")

PUBLINK_CODE = "kZt3skVZjtqVNME6G04IBuulkQSpbF0S8Rv7"
DEFAULT_FOLDER_ID = 30709259648          # "astrology books-magazines"
DEFAULT_DEST = os.environ.get("ASTRO_LIB") or r"F:\AstroBooksCollection"
API_HOSTS = ["https://api.pcloud.com", "https://eapi.pcloud.com"]

# Files never worth downloading (macOS metadata, shortcuts, executables).
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r"}
SKIP_EXTS = {".webloc", ".plist", ".alias", ".com", ".exe", ".dmg", ".app", ".bat", ".cmd", ".msi"}
SKIP_SUFFIXES = (" alias",)               # e.g. "something.pdf alias" (macOS aliases)

# pCloud result codes that mean "slow down / come back later"
RATE_LIMIT_CODES = {4000, 7005, 7008, 7009, 7010}

CHUNK = 1024 * 1024
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- paths
_ILLEGAL = {'<': '‹', '>': '›', ':': '꞉', '"': '\u201d', '/': '∕', '\\': '⧵', '|': '¦', '?': '？', '*': '＊'}
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """Make a single path component valid on Windows/NTFS, keeping it readable."""
    name = "".join(_ILLEGAL.get(ch, ch) for ch in name)
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.rstrip(" .") or "_"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 240:                                  # NTFS component limit is 255
        stem, dot, ext = name.rpartition(".")
        name = (stem[:230] + "~." + ext) if dot and len(ext) < 10 else name[:240]
    return name


def long_path(p: str) -> str:
    """Prefix with \\\\?\\ on Windows so paths longer than 260 chars work everywhere."""
    if os.name == "nt":
        p = os.path.abspath(p)
        if not p.startswith("\\\\?\\"):
            p = "\\\\?\\" + p
    return p


def should_skip(name: str, only_exts) -> bool:
    low = name.lower()
    if low in SKIP_NAMES or low.startswith("._") or low.endswith(SKIP_SUFFIXES):
        return True
    ext = os.path.splitext(low)[1]
    if ext in SKIP_EXTS:
        return True
    if only_exts and ext.lstrip(".") not in only_exts:
        return True
    return False


# --------------------------------------------------------------------------- pCloud API
class PCloud:
    def __init__(self, code):
        self.code = code
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 AstroLibraryDownloader/1.0"
        self.api = None

    def call(self, method, **params):
        params["code"] = self.code
        hosts = [self.api] if self.api else API_HOSTS
        last = None
        for host in hosts:
            try:
                r = self.s.get(f"{host}/{method}", params=params, timeout=120)
                data = r.json()
            except Exception as e:                          # network/JSON problems
                last = {"result": -1, "error": str(e)}
                continue
            if data.get("result") == 0:
                self.api = host
                return data
            last = data
            if data.get("result") != 7001:                  # 7001 = wrong region; try the other host
                break
        return last

    def tree(self):
        data = self.call("showpublink")
        if data.get("result") != 0:
            raise RuntimeError(f"showpublink failed: {data}")
        return data["metadata"]

    def file_url(self, fileid):
        data = self.call("getpublinkdownload", fileid=fileid, forcedownload=1)
        if data.get("result") != 0:
            return None, data
        host = data["hosts"][0]
        return f"https://{host}{data['path']}", data


def find_folder(node, folderid):
    if node.get("folderid") == folderid:
        return node
    for c in node.get("contents", []):
        if c.get("isfolder"):
            r = find_folder(c, folderid)
            if r:
                return r
    return None


def walk(node, remote="", local=""):
    for c in node.get("contents", []):
        if c.get("isfolder"):
            yield from walk(c, remote + c["name"] + "/", local + safe_name(c["name"]) + "/")
        else:
            yield remote + c["name"], local + safe_name(c["name"]), c


# --------------------------------------------------------------------------- manifest
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    fileid        INTEGER PRIMARY KEY,
    remote_path   TEXT NOT NULL,
    local_path    TEXT NOT NULL,
    name          TEXT NOT NULL,
    size          INTEGER,
    hash          TEXT,
    created       TEXT,
    modified      TEXT,
    contenttype   TEXT,
    status        TEXT DEFAULT 'pending',   -- pending | done | failed | skipped | removed
    attempts      INTEGER DEFAULT 0,
    error         TEXT,
    downloaded_at TEXT,
    seen_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_status ON files(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_db(dest):
    lib = os.path.join(dest, "_library")
    os.makedirs(lib, exist_ok=True)
    db = sqlite3.connect(os.path.join(lib, "library.db"), check_same_thread=False, timeout=60)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def sync_manifest(db, items, only_exts):
    """Insert new files, re-queue changed ones, flag ones that disappeared on pCloud."""
    now = datetime.now().isoformat(timespec="seconds")
    used_local = {}
    new = changed = 0
    for remote, local, f in items:
        # guarantee unique local paths after sanitising (case-insensitive on Windows)
        key = local.lower()
        if key in used_local and used_local[key] != f["fileid"]:
            stem, ext = os.path.splitext(local)
            local = f"{stem} [{f['fileid']}]{ext}"
        used_local[local.lower()] = f["fileid"]

        skip = should_skip(f["name"], only_exts)
        row = db.execute("SELECT hash, size, status FROM files WHERE fileid=?", (f["fileid"],)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO files(fileid,remote_path,local_path,name,size,hash,created,modified,contenttype,status,seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f["fileid"], remote, local, f["name"], f.get("size"), str(f.get("hash")), f.get("created"),
                 f.get("modified"), f.get("contenttype"), "skipped" if skip else "pending", now))
            new += 1
        else:
            status = row[2]
            if str(f.get("hash")) != row[0] or f.get("size") != row[1]:
                status = "pending"
                changed += 1
            if skip and status != "done":
                status = "skipped"
            elif not skip and status in ("skipped", "removed"):
                status = "pending"
            db.execute("UPDATE files SET remote_path=?, local_path=?, name=?, size=?, hash=?, modified=?, status=?, seen_at=?"
                       " WHERE fileid=?",
                       (remote, local, f["name"], f.get("size"), str(f.get("hash")), f.get("modified"), status, now,
                        f["fileid"]))
    removed = db.execute("UPDATE files SET status='removed' WHERE seen_at<>? AND status<>'done'", (now,)).rowcount
    db.commit()
    return new, changed, removed


# --------------------------------------------------------------------------- download
class RateLimited(Exception):
    pass


def download_one(pc, dest, fileid, local_rel, size):
    target = long_path(os.path.join(dest, local_rel.replace("/", os.sep)))
    if os.path.exists(target) and os.path.getsize(target) == size:
        return "exists"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have > size:
        os.remove(part)
        have = 0

    url, info = pc.file_url(fileid)
    if not url:
        code = info.get("result")
        text = str(info.get("error", "")).lower()
        if code in RATE_LIMIT_CODES or any(w in text for w in ("traffic", "limit", "too many", "try again")):
            raise RateLimited(f"{code}: {info.get('error')}")
        raise RuntimeError(f"pCloud {code}: {info.get('error')}")

    headers = {"Range": f"bytes={have}-"} if have else {}
    with pc.s.get(url, headers=headers, stream=True, timeout=(30, 300)) as r:
        if r.status_code == 416:                           # already complete
            pass
        elif r.status_code in (429, 503, 509):
            raise RateLimited(f"HTTP {r.status_code}")
        elif r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        else:
            mode = "ab" if (have and r.status_code == 206) else "wb"
            with open(part, mode) as fh:
                for chunk in r.iter_content(CHUNK):
                    fh.write(chunk)

    got = os.path.getsize(part)
    if got != size:
        raise RuntimeError(f"size mismatch {got} != {size}")
    os.replace(part, target)
    return "downloaded"


def display_name(local_rel, folder_width=40):
    """Full file name (never cut) followed by its folder, shortened in the middle if long."""
    folder, _, name = local_rel.rpartition("/")
    if not folder:
        return name
    if len(folder) > folder_width:
        half = (folder_width - 1) // 2
        folder = folder[:half] + "…" + folder[-half:]
    return f"{name}   [in {folder}]"


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def show_status(db):
    print("\nStatus           Files        Size")
    for status, n, s in db.execute("SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY status ORDER BY status"):
        print(f"  {status:<12} {n:>8}  {human(s):>12}")
    for (err, n) in db.execute("SELECT error, COUNT(*) FROM files WHERE status='failed' GROUP BY error ORDER BY 2 DESC LIMIT 10"):
        print(f"    failed: {n} x {err}")


def main():
    ap = argparse.ArgumentParser(description="Download the pCloud astrology library.")
    ap.add_argument("--dest", default=DEFAULT_DEST, help=f"target folder (default {DEFAULT_DEST})")
    ap.add_argument("--folder-id", type=int, default=DEFAULT_FOLDER_ID,
                    help="pCloud folder id inside the public link (default: astrology books-magazines)")
    ap.add_argument("--workers", type=int, default=3, help="parallel downloads (default 3, keep it modest)")
    ap.add_argument("--only", help="comma separated extensions to download, e.g. pdf,doc,docx,epub")
    ap.add_argument("--limit", type=int, help="download at most N files this run (testing)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be downloaded, then exit")
    ap.add_argument("--status", action="store_true", help="show manifest summary and exit")
    ap.add_argument("--retry-failed", action="store_true", help="only retry files that failed before")
    ap.add_argument("--max-pauses", type=int, default=10,
                    help="give up for this run after this many consecutive rate-limit pauses (default 10, ~4 h)")
    ap.add_argument("--no-refresh", action="store_true", help="reuse cached folder listing instead of fetching it again")
    ap.add_argument("--ids-file", help="download only the pCloud file ids listed in this file (one per line); "
                                       "skips refreshing the folder listing")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)
    db = open_db(dest)
    if args.status:
        show_status(db)
        return

    only = {e.strip().lower().lstrip(".") for e in args.only.split(",")} if args.only else None
    pc = PCloud(PUBLINK_CODE)
    cache = os.path.join(dest, "_library", "pcloud_tree.json")

    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as fh:
            ids = sorted({int(x) for x in re.findall(r"\d+", fh.read())})
        db.execute("CREATE TEMP TABLE wanted (fileid INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO wanted VALUES (?)", [(i,) for i in ids])
        already = db.execute("SELECT COUNT(*) FROM files JOIN wanted USING(fileid) WHERE status='done'").fetchone()[0]
        log(f"Selected {len(ids)} file(s); {already} already downloaded")
        todo = db.execute("SELECT fileid, local_path, size FROM files JOIN wanted USING(fileid) "
                          "WHERE status IN ('pending','failed','skipped') ORDER BY local_path").fetchall()
    elif args.no_refresh and os.path.exists(cache):
        log("Using cached folder listing")
        with open(cache, encoding="utf-8") as fh:
            root = json.load(fh)
    else:
        log("Fetching folder listing from pCloud (about 25 MB of metadata)...")
        root = pc.tree()
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(root, fh)

    if not args.ids_file:
        folder = find_folder(root, args.folder_id)
        if folder is None:
            sys.exit(f"Folder id {args.folder_id} not found in public link.")
        log(f"Source folder: {folder['name']}")

        new, changed, removed = sync_manifest(db, walk(folder), only)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('source_folder', ?)", (folder["name"],))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (datetime.now().isoformat(timespec="seconds"),))
        db.commit()
        log(f"Manifest updated: {new} new, {changed} changed, {removed} no longer on pCloud")

        where = "status='failed'" if args.retry_failed else "status IN ('pending','failed')"
        todo = db.execute(f"SELECT fileid, local_path, size FROM files WHERE {where} ORDER BY local_path").fetchall()
    if args.limit:
        todo = todo[: args.limit]
    total_bytes = sum(t[2] or 0 for t in todo)
    log(f"To download: {len(todo)} files, {human(total_bytes)}")

    if args.dry_run:
        for _, lp, sz in todo[:200]:
            print(f"  {human(sz or 0):>10}  {lp}")
        if len(todo) > 200:
            print(f"  ... and {len(todo) - 200} more")
        show_status(db)
        return

    db_lock = threading.Lock()
    pause_until = [0.0]
    done_bytes = [0]
    counter = [0]
    t0 = time.time()

    stop = threading.Event()
    rate_hits = [0]
    # keep the catalogue's "downloaded" flag current without needing a rebuild
    has_books = db.execute("SELECT 1 FROM sqlite_master WHERE name='books'").fetchone() is not None

    def worker(item):
        fileid, local_rel, size = item
        size = size or 0
        attempt = 0
        while attempt < 6:
            if stop.is_set():
                return False                              # quota exhausted: leave as 'pending'
            wait = pause_until[0] - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                result = download_one(pc, dest, fileid, local_rel, size)
                rate_hits[0] = 0
                with db_lock:
                    db.execute("UPDATE files SET status='done', error=NULL, downloaded_at=?, attempts=attempts+1 WHERE fileid=?",
                               (datetime.now().isoformat(timespec="seconds"), fileid))
                    if has_books:
                        db.execute("UPDATE books SET downloaded=1 WHERE fileid=?", (fileid,))
                    db.commit()
                    counter[0] += 1
                    done_bytes[0] += size
                    rate = done_bytes[0] / max(time.time() - t0, 1)
                    eta = (total_bytes - done_bytes[0]) / rate if rate else 0
                    log(f"[{counter[0]}/{len(todo)}] {result:<10} {human(size):>9}  {display_name(local_rel)}"
                        f"   ({human(rate)}/s, ETA {eta / 3600:.1f} h)")
                return True
            except RateLimited as e:
                # Not the file's fault: pause everyone, don't count it as a failed attempt.
                with db_lock:
                    if time.time() >= pause_until[0]:
                        rate_hits[0] += 1
                        delay = min(60 * 2 ** (rate_hits[0] - 1), 1800)
                        pause_until[0] = time.time() + delay
                        log(f"pCloud traffic/rate limit ({e}); pausing all downloads {delay // 60} min")
                        if rate_hits[0] > args.max_pauses:
                            log("pCloud keeps refusing (link traffic quota probably used up for now). "
                                "Stopping; re-run later to resume where it left off.")
                            stop.set()
                continue
            except Exception as e:                       # network error, size mismatch, ...
                attempt += 1
                err = str(e)[:300]
                time.sleep(min(5 * 2 ** attempt, 120))
        with db_lock:
            db.execute("UPDATE files SET status='failed', error=?, attempts=attempts+1 WHERE fileid=?", (err, fileid))
            db.commit()
        log(f"FAILED {local_rel}: {err}")
        return False

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futures = [ex.submit(worker, t) for t in todo]
            for _ in as_completed(futures):
                pass
    except KeyboardInterrupt:
        log("Interrupted - progress saved. Re-run the same command to resume.")
        os._exit(1)

    show_status(db)
    log("Finished. Next step:  python astro_library.py build")


if __name__ == "__main__":
    main()
