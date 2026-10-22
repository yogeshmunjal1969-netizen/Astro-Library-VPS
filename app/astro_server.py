#!/usr/bin/env python3
"""
astro_server.py - Astro Library web server for a home server, reachable at https://astrobooks.munjal.tech

    ASTRO_LIB=/srv/astrobooks/library ASTRO_DATA=/var/lib/astrobooks python astro_server.py

Configuration comes from environment variables (normally /etc/astrobooks/astrobooks.env, read by systemd):
  ASTRO_LIB            library folder copied from the pen drive        (default /srv/astrobooks/library)
  ASTRO_DATA           user accounts and sessions (users.db)           (default /var/lib/astrobooks)
  ASTRO_BIND           listen address; keep 127.0.0.1 behind Cloudflare Tunnel        (default 127.0.0.1)
  ASTRO_PORT           listen port                                                     (default 8765)
  ASTRO_ALLOWED_HOSTS  comma-separated host names the site answers to   (default astrobooks.munjal.tech,localhost,127.0.0.1)

Everyone must sign in. Readers can search, read books in the browser and download them one at a time, up to
their daily limit (10 unless the admin sets another); their access ends on the account's valid-until date.
Every download is recorded per user. Admins can also run the pCloud downloader, rebuild the catalogue, export it and
manage user accounts.  Accounts are managed with astro_auth.py (or the Users card in Tools).

Uses only the Python standard library (plus the companion scripts in the same folder).
"""

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from difflib import SequenceMatcher
from collections import defaultdict, deque
from email.utils import formatdate
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import astro_auth as AUTH  # noqa: E402
import astro_library as AL  # noqa: E402

LIB = os.path.abspath(os.environ.get("ASTRO_LIB") or "/srv/astrobooks/library")
BIND = os.environ.get("ASTRO_BIND") or "127.0.0.1"
PORT = int(os.environ.get("ASTRO_PORT") or 8765)
ALLOWED_HOSTS = {h.strip().lower() for h in (os.environ.get("ASTRO_ALLOWED_HOSTS")
                                             or "astrobooks.munjal.tech,localhost,127.0.0.1").split(",") if h.strip()}
COOKIE = "astro_sid"
PAGE_SIZE = 50
MAX_BODY = 1 << 20
CHUNK = 256 * 1024
# shown in the browser; everything else (html, doc, epub…) is always sent as a download
INLINE_TYPES = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "mp3": "audio/mpeg",
                "mp4": "video/mp4", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
# best-match ranking: how much a hit in each books_fts column counts (title, authors, publisher, topic, subtopic,
# tags, language, tradition, doc_type, filename)
FTS_WEIGHTS = "10.0, 4.0, 1.0, 2.0, 2.0, 2.0, 0.5, 1.0, 0.5, 3.0"
EXPORTS = {"xlsx": "Astro Library Catalogue.xlsx", "csv": "Astro Library Catalogue.csv",
           "html": "Astro Library Catalogue.html"}


# ----------------------------------------------------------------------------- background processes
class Job:
    """Runs one of the companion scripts as a subprocess and keeps the first and last lines of its output."""

    HEAD = 6

    def __init__(self, name):
        self.name = name
        self.proc = None
        self.lines = deque(maxlen=400)
        self.head, self.total = [], 0
        self.started = None
        self.finished = None
        self.returncode = None
        self.label = ""

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args, label):
        if self.running:
            return False
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.lines.clear()
        self.head, self.total = [], 0
        self.add(f"$ {label}")
        self.label = label
        self.started, self.finished, self.returncode = time.time(), None, None
        self.proc = subprocess.Popen([sys.executable, "-u", *args], cwd=HERE, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def add(self, line):
        self.lines.append(line)
        self.total += 1
        if self.total <= self.HEAD:
            self.head.append(line)

    def _pump(self):
        for line in self.proc.stdout:
            self.add(line.rstrip("\n"))
        self.returncode = self.proc.wait()
        self.finished = time.time()
        self.add(f"[finished with exit code {self.returncode}]")

    def stop(self):
        if self.running:
            self.proc.terminate()                       # downloads resume from their .part files next time
            self.add("[stopped by user - progress is saved, press Start to resume]")
            return True
        return False

    def state(self, tail=150):
        log = list(self.lines)[-tail:]
        hidden = self.total - len(self.head) - len(log)
        if hidden > 0:                                   # keep the run's opening lines visible
            log = self.head + [f"   … {hidden} more line(s) …"] + log
        return {"running": self.running, "label": self.label, "started": self.started,
                "finished": self.finished, "returncode": self.returncode, "log": log}


class Throttle:
    """Counts failed sign-ins per key (client IP, user name) in a sliding window."""

    def __init__(self, limit, window=900):
        self.limit, self.window = limit, window
        self.fails = defaultdict(deque)
        self.lock = threading.Lock()

    def blocked(self, key):
        with self.lock:
            q = self.fails[key]
            while q and q[0] < time.time() - self.window:
                q.popleft()
            return len(q) >= self.limit

    def fail(self, key):
        with self.lock:
            self.fails[key].append(time.time())

    def reset(self, key):
        with self.lock:
            self.fails.pop(key, None)


IP_THROTTLE, USER_THROTTLE = Throttle(10), Throttle(20)


class OneAtATime:
    """Gives each reader one download slot; it frees when the transfer finishes or breaks off."""

    def __init__(self):
        self.busy = set()
        self.lock = threading.Lock()

    def take(self, key):
        with self.lock:
            if key in self.busy:
                return False
            self.busy.add(key)
            return True

    def give(self, key):
        with self.lock:
            self.busy.discard(key)


READER_DOWNLOADS = OneAtATime()


# ----------------------------------------------------------------------------- data access
class Library:
    def __init__(self, lib):
        self.lib = lib
        self.real_lib = os.path.realpath(lib)
        self.db_path = os.path.join(lib, "_library", "library.db")
        self.download = Job("download")
        self.task = Job("task")
        self._vocab = (None, {}, {})                        # (catalogue mtime, term -> books, first letter -> terms)

    def db(self):
        if not os.path.exists(self.db_path):
            return None
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def has_catalogue(self, db):
        return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE name='books'").fetchone() is not None

    def path_of(self, local_path):
        """Absolute path of a library file, or None if it would point outside the library."""
        p = os.path.realpath(os.path.join(self.lib, local_path))
        return p if p.startswith(self.real_lib + os.sep) else None

    # --- overview + filter values
    def facets(self, admin):
        db = self.db()
        out = {"manifest": db is not None, "catalogue": self.has_catalogue(db)}
        if admin:
            out["lib"] = self.lib
        if not out["catalogue"]:
            return out

        def group(col):
            return [[k, n] for k, n in db.execute(
                f"SELECT {col}, COUNT(*) FROM books WHERE {col} IS NOT NULL AND {col}<>'' GROUP BY {col} ORDER BY 2 DESC")]
        out.update(topics=group("topic"), types=group("doc_type"), traditions=group("tradition"),
                   languages=group("language"))
        tags = {}
        for (t,) in db.execute("SELECT tags FROM books WHERE tags IS NOT NULL AND tags<>''"):
            for x in t.split(", "):
                tags[x] = tags.get(x, 0) + 1
        out["tags"] = sorted(([k, n] for k, n in tags.items()), key=lambda x: -x[1])
        total, size, dl, authors = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(downloaded),0), COUNT(DISTINCT authors) FROM books").fetchone()
        ymin, ymax = db.execute("SELECT MIN(year), MAX(year) FROM books").fetchone()
        out["stats"] = {"total": total, "size": size, "downloaded": dl, "authors": authors, "ymin": ymin, "ymax": ymax}
        return out

    # --- shortlist: one per user, keyed by pCloud fileid so it survives catalogue rebuilds
    def shortlist_db(self):
        db = self.db()
        if db is not None:
            db.execute("CREATE TABLE IF NOT EXISTS web_shortlist (user TEXT, fileid INTEGER, added TEXT, "
                       "PRIMARY KEY(user, fileid))")
        return db

    def shortlist_summary(self, user, db=None):
        db = db or self.shortlist_db()
        if not self.has_catalogue(db):
            return {"count": 0, "pending": 0, "pending_bytes": 0, "ready_bytes": 0}
        n, pend, pbytes, rbytes = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(b.downloaded=0),0), COALESCE(SUM(CASE WHEN b.downloaded=0 THEN b.size END),0), "
            "COALESCE(SUM(CASE WHEN b.downloaded=1 THEN b.size END),0) "
            "FROM web_shortlist s JOIN books b USING(fileid) WHERE s.user=?", (user,)).fetchone()
        return {"count": n, "pending": pend, "pending_bytes": pbytes, "ready_bytes": rbytes}

    def shortlist_change(self, user, data):
        db = self.shortlist_db()
        if not self.has_catalogue(db):
            return {"error": "Catalogue not built yet."}
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if data.get("clear"):
            db.execute("DELETE FROM web_shortlist WHERE user=?", (user,))
        ids = [int(i) for i in data.get("add") or []]
        if isinstance(data.get("add_query"), dict):          # every book matching the current search
            w, params, _, _ = self.where(data["add_query"], user, db)
            ids = [r[0] for r in db.execute(f"SELECT id FROM books {w}", params)]
        for i in ids:
            db.execute("INSERT OR IGNORE INTO web_shortlist SELECT ?, fileid, ? FROM books WHERE id=?", (user, now, i))
        for i in data.get("remove") or []:
            db.execute("DELETE FROM web_shortlist WHERE user=? AND fileid=(SELECT fileid FROM books WHERE id=?)",
                       (user, int(i)))
        db.commit()
        return self.shortlist_summary(user, db)

    def shortlist_files(self, user):
        """(absolute path, path inside the ZIP) of the user's shortlisted books that are on the server."""
        db = self.shortlist_db()
        if not self.has_catalogue(db):
            return []
        out = []
        for (rel,) in db.execute("SELECT b.local_path FROM web_shortlist s JOIN books b USING(fileid) "
                                 "WHERE s.user=? AND b.downloaded=1 ORDER BY b.local_path", (user,)):
            p = self.path_of(rel)
            if p and os.path.isfile(p):
                out.append((p, rel))
        return out

    def fileids_to_download(self, user, book_ids=None):
        """pCloud file ids of the user's shortlisted (or the given) books that are not on the server yet."""
        db = self.shortlist_db()
        if not self.has_catalogue(db):
            return []
        if book_ids is None:
            sql = "SELECT b.fileid FROM web_shortlist s JOIN books b USING(fileid) WHERE s.user=? AND b.downloaded=0"
            params = [user]
        else:
            params = [int(i) for i in book_ids][:5000]
            sql = f"SELECT fileid FROM books WHERE downloaded=0 AND id IN ({','.join('?' * len(params)) or 'NULL'})"
        return [r[0] for r in db.execute(sql, params) if r[0] > 0]

    # --- search
    # --- search words: forgive misspellings by also searching close, much commoner words of the catalogue
    def vocab(self, db):
        mtime = os.path.getmtime(self.db_path)
        if self._vocab[0] != mtime:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab(main, books_fts, row)")
            terms = dict(db.execute("SELECT term, doc FROM temp.vocab"))
            first = defaultdict(list)
            for t in terms:
                first[t[0]].append(t)
            self._vocab = (mtime, terms, first)
        return self._vocab[1], self._vocab[2]

    def similar(self, db, word):
        """Up to two common catalogue words spelled like `word`, when `word` itself is rare."""
        terms, first = self.vocab(db)
        hits = sum(n for t, n in terms.items() if t.startswith(word))
        if len(word) < 4 or hits >= 20 or not word.isalpha():
            return []
        need = max(10, 5 * (hits + 1))
        scored = []
        for t in first.get(word[0], ()):
            if terms[t] >= need and abs(len(t) - len(word)) <= 2 and not t.startswith(word):
                ratio = SequenceMatcher(None, word, t).ratio()
                if ratio >= 0.85:
                    scored.append((ratio, terms[t], t))
        return [t for _, _, t in sorted(scored, reverse=True)[:2]]

    def fts_query(self, db, text):
        """(FTS5 query, {typed word: [words also searched]}) - like AL.fts_query, plus spelling help."""
        out, also = [], {}
        for t in re.findall(r'"[^"]+"|\S+', text):
            if t.startswith('"'):
                out.append(t)
                continue
            t = re.sub(r'[^\w\u0080-\uFFFF-]', "", t)
            if not t:
                continue
            alts = self.similar(db, t.lower())
            if alts:
                also[t] = alts
                out.append("(" + " OR ".join(f'"{w}"*' for w in [t] + alts) + ")")
            else:
                out.append(f'"{t}"*')
        return " AND ".join(out), also

    def where(self, q, user, db=None):
        """WHERE clause, params, ORDER BY and search details ({"fts": query or None, "also": spellings})
        for the filters in q (parse_qs output or a plain dict)."""
        g = lambda k: (lambda v: str((v[0] if isinstance(v, list) else v) or ""))(q.get(k, "")).strip()
        where, params, info = [], [], {"fts": None, "also": {}}
        if g("q"):
            fq, info["also"] = self.fts_query(db or self.db(), g("q"))
            if fq:
                where.append("id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH ?)")
                params.append(fq)
                info["fts"] = fq
        for key, col in (("author", "authors"), ("publisher", "publisher"), ("title", "title")):
            if g(key):
                where.append(f"{col} LIKE ?"); params.append(f"%{g(key)}%")
        for key, col in (("topic", "topic"), ("type", "doc_type"), ("tradition", "tradition"), ("lang", "language")):
            if g(key):
                where.append(f"{col} = ?"); params.append(g(key))
        if g("tag"):
            where.append("(', ' || tags || ',') LIKE ?"); params.append(f"%, {g('tag')},%")
        for key, op in (("from", ">="), ("to", "<=")):
            if re.fullmatch(r"-?\d{1,4}", g(key)):
                where.append(f"year {op} ?"); params.append(int(g(key)))
        avail = g("avail")
        if avail == "done":
            where.append("downloaded = 1")
        elif avail == "missing":
            where.append("downloaded = 0")
        elif avail == "short":
            where.append("fileid IN (SELECT fileid FROM web_shortlist WHERE user=?)"); params.append(user)
        order = {"title": "title COLLATE NOCASE", "author": "authors IS NULL, authors COLLATE NOCASE",
                 "year": "year IS NULL, year", "year_desc": "year IS NULL, year DESC", "pages": "pages IS NULL, pages DESC",
                 "size": "size DESC", "added": "added DESC"}.get(g("sort"), "title COLLATE NOCASE")
        if g("sort") in ("", "best") and info["fts"]:
            order = "f.rank, title COLLATE NOCASE"            # needs the ranking join added by search()
        w = ("WHERE " + " AND ".join(where)) if where else ""
        return w, params, order, info

    def search(self, q, user):
        db = self.shortlist_db()
        if not self.has_catalogue(db):
            return {"total": 0, "rows": [], "error": "The catalogue has not been built yet."}
        g = lambda k: (q.get(k, [""])[0] or "").strip()
        try:
            w, params, order, info = self.where(q, user, db)
            rank, rank_params = "", []
            if order.startswith("f.rank"):
                rank = (f"JOIN (SELECT rowid AS rid, bm25(books_fts, {FTS_WEIGHTS}) AS rank FROM books_fts "
                        f"WHERE books_fts MATCH ?) f ON f.rid = books.id")
                rank_params = [info["fts"]]
            total = db.execute(f"SELECT COUNT(*) FROM books {w}", params).fetchone()[0]
            page = max(0, int(g("page") or 0))
            rows = db.execute(f"SELECT id,title,authors,year,year_text,publisher,doc_type,tradition,language,tags,"
                              f"topic,subtopic,pages,size,ext,downloaded,local_path,"
                              f"fileid IN (SELECT fileid FROM web_shortlist WHERE user=?) AS short "
                              f"FROM books {rank} {w} ORDER BY {order} LIMIT ? OFFSET ?",
                              [user] + rank_params + params + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
        except (sqlite3.OperationalError, ValueError) as e:
            return {"total": 0, "rows": [], "error": f"Search error: {e}"}
        out = []
        for r in rows:
            d = dict(r)
            d["fname"] = os.path.basename(d.pop("local_path") or "")
            out.append(d)
        return {"total": total, "page_size": PAGE_SIZE, "rows": out, "also": info["also"],
                "shortlist": self.shortlist_summary(user, db)}

    def book(self, bid, user):
        db = self.shortlist_db()
        r = db.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone() if self.has_catalogue(db) else None
        if not r:
            return None
        d = dict(r)
        d["short"] = db.execute("SELECT 1 FROM web_shortlist WHERE user=? AND fileid=?",
                                (user, d["fileid"])).fetchone() is not None
        d["fname"] = os.path.basename(d["local_path"])
        p = self.path_of(d["local_path"])
        d["exists"] = bool(p) and os.path.isfile(p)
        pm = db.execute("SELECT * FROM pdfmeta WHERE fileid=?", (d["fileid"],)).fetchone()
        d["pdf"] = dict(pm) if pm else None
        return d

    # --- pCloud download progress
    def download_status(self):
        out = self.download.state(80)
        db = self.db()
        counts = {}
        if db is not None:
            try:
                for status, n, s in db.execute("SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY status"):
                    counts[status] = {"files": n, "bytes": s}
                out["failed"] = [dict(r) for r in db.execute(
                    "SELECT local_path, error FROM files WHERE status='failed' ORDER BY local_path LIMIT 50")]
                last = db.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
                out["last_sync"] = last[0] if last else None
            except sqlite3.OperationalError:
                pass
        out["counts"] = counts
        du = shutil.disk_usage(self.lib)
        out["disk"] = {"total": du.total, "free": du.free}
        return out


def disposition(kind, name):
    fallback = re.sub(r'[^A-Za-z0-9._ ()\-]', "_", name) or "file"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    L: Library = None
    protocol_version = "HTTP/1.1"
    server_version = "AstroLibrary"
    timeout = 120                       # a stalled client must not hold a reader's download slot for ever
    sys_version = ""

    def log_message(self, fmt, *a):
        if os.environ.get("ASTRO_ACCESS_LOG"):
            sys.stderr.write(f"{self.client_ip()} {fmt % a}\n")

    # --- request helpers
    def trusted_proxy(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def client_ip(self):
        if self.trusted_proxy():                                  # cloudflared runs on this machine
            fwd = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            if fwd.strip():
                return fwd.strip()
        return self.client_address[0]

    def https(self):
        return self.trusted_proxy() and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower().rsplit(":", 1)[0].strip("[]")
        return "*" in ALLOWED_HOSTS or host in ALLOWED_HOSTS

    def token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def cookie(self, value, max_age):
        return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                + ("; Secure" if self.https() else ""))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    # --- responses
    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if self.https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def send(self, obj, code=200, ctype="application/json", headers=()):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.common_headers()
        if ctype == "text/html":
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; "
                             "form-action 'self'; base-uri 'none'")
            self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location, headers=()):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.common_headers()
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def message(self, text, code):
        page = LOGIN_PAGE.replace("__BODY__", f'<p>{esc(text)}</p><p><a href="/">Back to the library</a></p>')
        self.send(page.encode("utf-8"), code, "text/html")

    def stream(self, path, name, ctype, kind, cache="private, max-age=3600"):
        """Sends a file with HTTP Range support (PDF viewers fetch large books piece by piece).
        Returns True when everything asked for was sent."""
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m[1] or m[2]) and size:
            if m[1]:
                start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
            else:
                start = max(0, size - int(m[2]))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", disposition(kind, name))
        self.send_header("Cache-Control", cache)
        self.send_header("Last-Modified", formatdate(os.path.getmtime(path), usegmt=True))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.common_headers()
        self.end_headers()
        if self.command == "HEAD":
            return False
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return left == 0
        except (BrokenPipeError, ConnectionResetError, TimeoutError):   # viewer cancelled a range request
            self.close_connection = True
            return False

    def serve_book(self, sess, parts, q):
        try:
            bid = int(parts[2])
        except (IndexError, ValueError):
            return self.message("Unknown book.", 404)
        b = self.L.book(bid, sess["user"])
        path = b and self.L.path_of(b["local_path"])
        if not b or not path or not os.path.isfile(path):
            return self.message("This book is not on the server yet.", 404)
        ctype = None if q.get("dl") else INLINE_TYPES.get((b["ext"] or "").lower())
        kind = "inline" if ctype else "attachment"
        if kind == "inline" or self.command == "HEAD":
            return self.stream(path, b["fname"], ctype or "application/octet-stream", kind)
        if sess["admin"]:
            return self.recorded_download(sess, b, path)
        key = sess["user"].lower()
        if not READER_DOWNLOADS.take(key):
            return self.message("You are already downloading a book. Wait until it has finished, then try again.", 429)
        try:
            if AUTH.downloads_today(sess["user"]) >= sess["limit"]:
                return self.message(f"You have used today's {sess['limit']} downloads. You can download again "
                                    "tomorrow; reading books in the browser still works.", 429)
            self.recorded_download(sess, b, path)
        finally:
            READER_DOWNLOADS.give(key)

    def recorded_download(self, sess, b, path):
        """Streams a book as an attachment and records it; only a completed download counts towards the limit."""
        rec = AUTH.download_started(sess["user"], b, self.client_ip())
        AUTH.download_finished(rec, self.stream(path, b["fname"], "application/octet-stream", "attachment"))

    def serve_zip(self, sess):
        if not sess["admin"]:
            return self.message("Readers download books one at a time.", 403)
        files = self.L.shortlist_files(sess["user"])
        if not files:
            return self.message("None of your shortlisted books are on the server.", 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", disposition("attachment", f"Astro books {time.strftime('%Y-%m-%d')}.zip"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")                # length unknown: the end of the stream ends the ZIP
        self.common_headers()
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for path, arc in files:                       # PDFs are already compressed: store, don't deflate
                    zf.write(path, arc)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.host_ok():
            return self.send({"error": "unknown host"}, 400)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":
            return self.send(b"ok", ctype="text/plain")
        if u.path == "/favicon.ico":
            return self.send(FAVICON, ctype="image/svg+xml")
        sess = AUTH.session(self.token())
        if u.path == "/login":
            if sess:
                return self.redirect("/")
            return self.send(LOGIN_PAGE.replace("__BODY__", LOGIN_FORM.replace("__ERR__", "")).encode("utf-8"),
                             ctype="text/html")
        if not sess:
            if u.path.startswith("/api/"):
                return self.send({"error": "signed out"}, 401)
            return self.redirect("/login")
        if u.path == "/":
            page = PAGE.replace("__CSRF__", sess["csrf"]).replace("__USER__", json.dumps(sess["user"])) \
                       .replace("__ADMIN__", "true" if sess["admin"] else "false")
            return self.send(page.encode("utf-8"), ctype="text/html")
        parts = u.path.split("/")
        if u.path.startswith("/file/"):
            return self.serve_book(sess, parts, q)
        if u.path == "/zip/shortlist":
            return self.serve_zip(sess)
        if u.path.startswith("/export/"):
            name = EXPORTS.get(parts[2] if len(parts) > 2 else "")
            path = name and os.path.join(self.L.lib, name)
            if not sess["admin"] or not path:
                return self.message("Not found.", 404)
            if not os.path.isfile(path):
                return self.message("That export has not been made yet - run it on the Tools tab first.", 404)
            return self.stream(path, name, "application/octet-stream", "attachment", cache="no-store")
        if not u.path.startswith("/api/"):
            return self.message("Not found.", 404)
        if self.headers.get("X-CSRF") != sess["csrf"]:
            return self.send({"error": "forbidden"}, 403)
        user, L = sess["user"], self.L
        if u.path == "/api/facets":
            return self.send(L.facets(sess["admin"]))
        if u.path == "/api/search":
            return self.send(L.search(q, user))
        if u.path == "/api/book":
            b = L.book(int((q.get("id") or ["0"])[0] or 0), user)
            if b and not sess["admin"]:
                del b["local_path"]
            return self.send(b or {"error": "not found"}, 200 if b else 404)
        if u.path == "/api/shortlist":
            return self.send(L.shortlist_summary(user))
        if not sess["admin"]:
            return self.send({"error": "admins only"}, 403)
        if u.path == "/api/download":
            return self.send(L.download_status())
        if u.path == "/api/task":
            return self.send(L.task.state())
        if u.path == "/api/users":
            return self.send({"users": AUTH.list_users(), "default_limit": AUTH.DAILY_DOWNLOADS})
        if u.path == "/api/downloads":
            return self.send({"rows": AUTH.download_history((q.get("user") or [""])[0] or None, 500)})
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        if not self.host_ok():
            return self.send({"error": "unknown host"}, 400)
        path = urlparse(self.path).path
        try:
            raw = self.body()
        except ValueError:
            return self.send({"error": "request too large"}, 413)
        if path == "/login":
            return self.do_login(raw)
        token = self.token()
        sess = AUTH.session(token)
        if not sess:
            return self.send({"error": "signed out"}, 401)
        if self.headers.get("X-CSRF") != sess["csrf"]:
            return self.send({"error": "forbidden"}, 403)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        user, L = sess["user"], self.L
        if path == "/api/logout":
            AUTH.logout(token)
            return self.send({"ok": True}, headers=[("Set-Cookie", self.cookie("", 0))])
        if path == "/api/shortlist":
            return self.send(L.shortlist_change(user, data))
        if path == "/api/password":
            con = AUTH.db()
            row = con.execute("SELECT pw_hash FROM users WHERE name=?", (user,)).fetchone()
            if not row or not AUTH.check_password(str(data.get("old") or ""), row["pw_hash"]):
                return self.send({"ok": False, "error": "Current password is wrong."})
            err = AUTH.set_password(user, str(data.get("new") or ""))
            return self.send({"ok": err is None, "error": err})
        if not sess["admin"]:
            return self.send({"error": "admins only"}, 403)
        if path == "/api/users":
            return self.send(self.users_action(user, data))
        if path == "/api/download/start":
            if L.task.running:
                return self.send({"ok": False, "error": "Wait for the running tool task to finish first."})
            if L.download.running:
                return self.send({"ok": False, "error": "A download is already running - stop it first (progress is kept)."})
            args = ["astro_downloader.py", "--dest", L.lib, "--workers", str(max(1, min(8, int(data.get("workers") or 3))))]
            if data.get("shortlist") or data.get("ids"):          # selective: shortlisted books / one book
                fids = L.fileids_to_download(user, None if data.get("shortlist") else data["ids"])
                if not fids:
                    return self.send({"ok": False, "error": "Nothing to fetch - those books are already on the server."})
                ids_file = os.path.join(L.lib, "_library", "download_selection.txt")
                with open(ids_file, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(map(str, fids)))
                what = "shortlisted" if data.get("shortlist") else "selected"
                ok = L.download.start(args + ["--ids-file", ids_file], f"Download {len(fids)} {what} book(s)")
                return self.send({"ok": ok, "count": len(fids), "error": None if ok else "Already running"})
            only = re.sub(r"[^a-z0-9,]", "", (data.get("only") or "").lower())
            if only:
                args += ["--only", only]
            if data.get("retry_failed"):
                args.append("--retry-failed")
            ok = L.download.start(args, "Download: " + " ".join(args[1:]))
            return self.send({"ok": ok, "error": None if ok else "Already running"})
        if path == "/api/download/stop":
            return self.send({"ok": L.download.stop()})
        if path == "/api/task/start":
            kind = data.get("kind")
            base = ["astro_library.py", "--lib", L.lib]
            if kind == "build":
                args = base + ["build"] + (["--no-pdf-meta"] if data.get("no_pdf_meta") else [])
            elif kind == "html":
                args = base + ["html"]
            elif kind in ("xlsx", "csv"):
                args = base + ["export", f"--{kind}", os.path.join(L.lib, EXPORTS[kind])]
            elif kind == "reconcile":
                args = ["astro_maint.py", "--lib", L.lib, "reconcile"]
            else:
                return self.send({"ok": False, "error": "unknown task"})
            ok = L.task.start(args, " ".join(args[3:]))
            if ok and L.download.running and kind == "build":
                L.task.add("Note: download still running - the catalogue will include what is finished so far.")
            return self.send({"ok": ok, "error": None if ok else "Another task is running"})
        if path == "/api/task/stop":
            return self.send({"ok": L.task.stop()})
        self.send({"error": "not found"}, 404)

    def do_login(self, raw):
        form = parse_qs(raw.decode("utf-8", "replace"))
        name = (form.get("user") or [""])[0].strip()
        pw = (form.get("password") or [""])[0]
        ip = self.client_ip()
        err = None
        if IP_THROTTLE.blocked(ip) or USER_THROTTLE.blocked(name.lower()):
            err = "Too many failed attempts. Wait 15 minutes and try again."
        else:
            token, row = AUTH.login(name, pw, ip)
            if row and not token:
                IP_THROTTLE.reset(ip)
                USER_THROTTLE.reset(name.lower())
                err = f"Your access ended on {row['valid_until']}. Please contact the library admin."
            elif token:
                IP_THROTTLE.reset(ip)
                USER_THROTTLE.reset(name.lower())
                return self.redirect("/", [("Set-Cookie", self.cookie(token, AUTH.SESSION_DAYS * 86400))])
            else:
                IP_THROTTLE.fail(ip)
                USER_THROTTLE.fail(name.lower())
                sys.stderr.write(f"failed sign-in for {name!r} from {ip}\n")
                err = "Wrong user name or password."
        body = LOGIN_FORM.replace("__ERR__", f'<div class="err">{esc(err)}</div>').replace(
            'name="user" ', f'name="user" value="{esc(name)}" ')
        self.send(LOGIN_PAGE.replace("__BODY__", body).encode("utf-8"), 401, "text/html")

    @staticmethod
    def users_action(me, data):
        act, name = data.get("action"), str(data.get("name") or "").strip()
        if act == "add":
            err = AUTH.add_user(name, str(data.get("password") or ""), bool(data.get("admin")),
                                str(data.get("valid_until") or ""))
            if not err and str(data.get("daily_limit") or "").strip():
                err = AUTH.set_limit(name, data.get("daily_limit"))
        elif act == "limit":
            err = AUTH.set_limit(name, data.get("daily_limit"))
        elif act == "valid":
            err = AUTH.set_validity(name, str(data.get("valid_until") or ""))
        elif act == "passwd":
            err = AUTH.set_password(name, str(data.get("password") or ""))
        elif act == "admin":
            err = "You cannot remove your own admin rights." if name.lower() == me.lower() and not data.get("admin") \
                else AUTH.set_admin(name, bool(data.get("admin")))
        elif act == "delete":
            err = "You cannot delete your own account." if name.lower() == me.lower() else AUTH.delete_user(name)
        else:
            err = "unknown action"
        return {"ok": err is None, "error": err, "users": AUTH.list_users(), "default_limit": AUTH.DAILY_DOWNLOADS}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# ----------------------------------------------------------------------------- pages
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#6b3fa0" stroke-width="2">'
           b'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.2"/>'
           b'<path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21"/></svg>')

LOGIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Astro Library</title><link rel="icon" href="/favicon.ico">
<style>
:root{--bg:#f7f5f0;--panel:#fff;--fg:#1d1b18;--mute:#6d675d;--line:#e6e1d6;--acc:#6b3fa0;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141318;--panel:#1c1b22;--fg:#ece9f2;--mute:#9d98a8;--line:#2d2b35;--acc:#c49cf0;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--fg);font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:16px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:28px 26px;width:100%;max-width:360px}
.brand{font-weight:700;font-size:20px;display:flex;gap:9px;align-items:center;margin-bottom:4px}.brand svg{color:var(--acc)}
.sub{color:var(--mute);font-size:13px;margin-bottom:20px}label{display:block;font-size:12px;font-weight:600;color:var(--mute);margin:14px 0 4px}
input{width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
input:focus{outline:2px solid var(--acc);outline-offset:-1px}
button{margin-top:20px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
@media (prefers-color-scheme:dark){button{color:#1a1420}}
.err{background:color-mix(in srgb,var(--bad) 12%,transparent);color:var(--bad);border-radius:8px;padding:8px 11px;font-size:13px}a{color:var(--acc)}
</style></head><body><div class="box">
<div class="brand"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.2"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21"/></svg>Astro Library</div>
<div class="sub">astrobooks.munjal.tech</div>
__BODY__
</div></body></html>"""

LOGIN_FORM = """__ERR__<form method="post" action="/login">
<label for="u">User name</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Astro Library</title><link rel="icon" href="/favicon.ico">
<style>
:root{--bg:#f7f5f0;--panel:#fff;--fg:#1d1b18;--mute:#6d675d;--line:#e6e1d6;--acc:#6b3fa0;--acc2:#efe8f7;--ok:#2e7d4f;--warn:#b26a00;--bad:#b3261e;--shadow:0 1px 3px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#141318;--panel:#1c1b22;--fg:#ece9f2;--mute:#9d98a8;--line:#2d2b35;--acc:#c49cf0;--acc2:#2b2337;--ok:#6fcf97;--warn:#f2b552;--bad:#ff8a80;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:14px/1.45 "Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:20px;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-weight:700;font-size:17px;display:flex;gap:8px;align-items:center}.brand svg{color:var(--acc)}
nav{display:flex;gap:4px}nav button{border:0;background:none;color:var(--mute);padding:7px 14px;border-radius:7px;font:inherit;cursor:pointer}
nav button.on{background:var(--acc2);color:var(--acc);font-weight:600}
.hstat{margin-left:auto;color:var(--mute);font-size:12px;text-align:right}
.who{color:var(--mute);font-size:12px;white-space:nowrap}.who a{margin-left:8px}
main{flex:1;min-height:0;display:flex}.tab{display:none;flex:1;min-height:0}.tab.on{display:flex}
/* library */
aside.filters{width:250px;flex:none;overflow:auto;padding:14px 16px;border-right:1px solid var(--line);background:var(--panel)}
.filters label{display:block;font-size:11px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.04em;margin:12px 0 4px}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
input:focus,select:focus{outline:2px solid var(--acc);outline-offset:-1px}
.row2{display:flex;gap:6px}.chk{display:flex;align-items:center;gap:6px;margin-top:12px;color:var(--fg);font-size:13px}.chk input{width:auto}
.results{flex:1;min-width:0;display:flex;flex-direction:column}
.searchbar{padding:14px 18px 10px;display:flex;gap:10px;align-items:center}
.searchbar input{font-size:15px;padding:10px 12px}.searchbar select{width:170px}#ftog{display:none}
.meta{padding:0 18px 8px;color:var(--mute);font-size:12px;display:flex;gap:10px;align-items:center}
.list{flex:1;overflow:auto;padding:0 18px 18px}
.item{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 14px;margin-bottom:8px;cursor:pointer;box-shadow:var(--shadow);display:flex;gap:12px}
.item:hover{border-color:var(--acc)}.item.sel{border-color:var(--acc);outline:1px solid var(--acc)}
.item .ic{flex:none;width:34px;height:42px;border-radius:4px;background:var(--acc2);color:var(--acc);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;text-transform:uppercase}
.item.nd{opacity:.6}.item .t{font-weight:600}.item .a{color:var(--mute);font-size:13px}.item .s{color:var(--mute);font-size:12px;margin-top:2px}
.chip{display:inline-block;background:var(--acc2);color:var(--acc);border-radius:10px;padding:0 8px;font-size:11px;margin:3px 4px 0 0}
.chip.g{background:transparent;border:1px solid var(--line);color:var(--mute)}
.star{flex:none;align-self:center;border:0;background:none;color:var(--mute);font-size:20px;line-height:1;padding:4px;cursor:pointer}
.star:hover{color:var(--acc)}.star.on{color:var(--warn)}
.sbar{margin:0 18px 10px;padding:8px 12px;border:1px solid var(--line);border-radius:9px;background:var(--panel);display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}
.sbar b{color:var(--warn)}.sbar .sp{flex:1}.sbar .b{padding:4px 10px}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;padding:6px}
.b{display:inline-block;text-decoration:none;border:1px solid var(--line);background:var(--panel);color:var(--fg);padding:7px 13px;border-radius:7px;cursor:pointer;font:inherit}
.b:hover{border-color:var(--acc)}.b.p{background:var(--acc);border-color:var(--acc);color:#fff}
@media (prefers-color-scheme:dark){.b.p{color:#1a1420}}
.b:disabled,.b.off{opacity:.45;cursor:default;pointer-events:none}.b.danger{color:var(--bad)}
.detail{width:380px;flex:none;border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:18px;display:none}
.detail.on{display:block}.detail h2{font-size:17px;margin:0 0 4px}.detail .by{color:var(--mute);margin-bottom:12px}
.detail dl{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;margin:14px 0;font-size:13px}.detail dt{color:var(--mute)}.detail dd{margin:0;word-break:break-word}
.detail .acts{display:flex;gap:8px;flex-wrap:wrap}.detail .path{font-size:11px;color:var(--mute);word-break:break-all;margin-top:14px}
.empty{color:var(--mute);text-align:center;padding:60px 20px}
a.lnk{color:var(--acc);cursor:pointer;text-decoration:underline}
/* downloads / tools */
.page{flex:1;overflow:auto;padding:22px 26px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow)}
.card .k{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}.card .v{font-size:22px;font-weight:700;margin-top:4px}.card .v small{font-size:12px;font-weight:400;color:var(--mute)}
.bar{height:12px;background:var(--line);border-radius:6px;overflow:hidden;margin:6px 0 4px}.bar div{height:100%;background:var(--acc);transition:width .6s}
.ctl{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin:14px 0}.ctl .f{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--mute)}.ctl input{width:130px}.ctl .chk input{width:auto}
pre.log{background:#0f0e13;color:#d8d4e3;border-radius:9px;padding:12px 14px;height:330px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;margin:0}
.pill{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}.pill.run{background:#e3f4ea;color:var(--ok)}.pill.idle{background:var(--acc2);color:var(--mute)}
@media (prefers-color-scheme:dark){.pill.run{background:#173325}}
h3{margin:22px 0 8px;font-size:15px}.note{color:var(--mute);font-size:13px;max-width:780px}
.tools{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.tools .card p{color:var(--mute);font-size:13px;margin:6px 0 12px}
.tools .card label{display:inline-flex;gap:5px;align-items:center;margin:0 10px 6px 0;font-size:13px}.tools .card label input{width:auto}
table.fl{border-collapse:collapse;font-size:12px;width:100%}table.fl td{border-bottom:1px solid var(--line);padding:4px 6px;vertical-align:top}
.uform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}.uform input[type=text],.uform input[type=password]{width:160px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:8;padding:16px}.modal.on{display:flex}
.modal .card{width:100%;max-width:340px}.modal label{display:block;font-size:12px;color:var(--mute);margin:10px 0 3px}
@media (max-width:900px){header{flex-wrap:wrap;gap:8px 14px;padding:10px 16px}.hstat{display:none}.who{margin-left:auto}
 aside.filters{display:none;position:fixed;inset:0;z-index:6;width:auto;padding-top:56px}aside.filters.on{display:block}
 #fclose{display:block!important;position:absolute;top:14px;right:16px}#ftog{display:inline-block}
 .searchbar{padding:12px 16px 8px;flex-wrap:wrap}.searchbar input{flex:1 1 100%}.searchbar select{flex:1;width:auto}
 .meta,.list{padding-left:16px;padding-right:16px}.sbar{margin:0 16px 10px}.page{padding:18px 16px}
 .detail{position:fixed;inset:0;width:auto;z-index:7}}
</style></head><body>
<header>
 <div class="brand"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.2"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21"/></svg>Astro Library</div>
 <nav><button data-t="lib" class="on">Library</button><button data-t="dl" class="adm">pCloud sync</button><button data-t="tools" class="adm">Tools</button></nav>
 <div class="hstat" id="hstat"></div>
 <div class="who"><span id="me"></span><a class="lnk" id="pw-open">Password</a><a class="lnk" id="logout">Sign out</a></div>
</header>
<main>
 <section class="tab on" id="t-lib">
  <aside class="filters" id="filters">
   <button class="b" id="fclose" style="display:none">Done</button>
   <label>Author</label><input id="f-author" placeholder="e.g. B V Raman">
   <label>Publisher / series</label><input id="f-publisher" placeholder="e.g. Sagar">
   <label>Year</label><div class="row2"><input id="f-from" placeholder="from" inputmode="numeric"><input id="f-to" placeholder="to" inputmode="numeric"></div>
   <label>Topic</label><select id="f-topic"><option value="">All topics</option></select>
   <label>Tradition</label><select id="f-tradition"><option value="">All</option></select>
   <label>Type</label><select id="f-type"><option value="">All</option></select>
   <label>Language</label><select id="f-lang"><option value="">All</option></select>
   <label>Tag</label><select id="f-tag"><option value="">Any</option></select>
   <label>Show</label><select id="f-avail"><option value="">All books</option><option value="done">On the server (readable)</option><option value="missing">Not on the server yet</option><option value="short">★ My shortlist</option></select>
   <div style="margin-top:16px"><button class="b" id="reset" style="width:100%">Clear filters</button></div>
  </aside>
  <div class="results">
   <div class="searchbar"><input id="q" placeholder="Search titles, authors, topics, publishers… (e.g. navamsa, &quot;medical astrology&quot;)" autofocus>
    <button class="b" id="ftog">Filters</button>
    <select id="sort"><option value="best" title="Most relevant first when words are typed; by title otherwise">Sort: Best match</option><option value="title">Sort: Title</option><option value="author">Sort: Author</option><option value="year">Sort: Year ↑</option><option value="year_desc">Sort: Year ↓</option><option value="pages">Sort: Pages</option><option value="size">Sort: Size</option><option value="added">Sort: Recently added</option></select></div>
   <div class="sbar"><span>★ Shortlist: <b id="sl-count">0</b> book(s)<span id="sl-pend"></span></span>
    <button class="b" id="sl-view">View</button><a class="b p adm" id="sl-zip" href="/zip/shortlist" title="Download the shortlisted books as one ZIP file">Download ZIP</a>
    <button class="b adm" id="sl-dl" title="Fetch the shortlisted books that are not on the server yet from pCloud">Fetch from pCloud</button>
    <span class="sp"></span><button class="b danger" id="sl-clear">Clear</button></div>
   <div class="meta"><span id="count"></span><span id="also" style="color:var(--mute)"></span><a class="lnk" id="sl-addall" style="display:none">☆ Add all results to shortlist</a></div>
   <div class="list" id="list"></div>
   <div class="pager"><button class="b" id="prev">‹ Previous</button><span id="pg"></span><button class="b" id="next">Next ›</button></div>
  </div>
  <div class="detail" id="detail"></div>
 </section>

 <section class="tab" id="t-dl"><div class="page">
  <div style="display:flex;align-items:center;gap:12px"><h2 style="margin:0;font-size:19px">pCloud sync</h2><span id="dl-pill" class="pill idle">Idle</span></div>
  <p class="note">Fetches the pCloud collection to the server folder <b id="dl-lib"></b>. You can stop at any time; pressing Start again resumes where it left off (finished files are skipped, partial files continue).</p>
  <div class="card" style="margin:14px 0 16px"><div style="display:flex;justify-content:space-between;font-size:13px"><span id="dl-pct">–</span><span id="dl-speed" style="color:var(--mute)"></span></div><div class="bar"><div id="dl-bar" style="width:0"></div></div><div id="dl-bytes" style="color:var(--mute);font-size:12px"></div></div>
  <div class="cards" id="dl-cards"></div>
  <div class="ctl">
   <div class="f">Parallel downloads<input id="dl-workers" type="number" min="1" max="8" value="3"></div>
   <div class="f">Only these types (optional)<input id="dl-only" placeholder="pdf,doc,docx" style="width:170px"></div>
   <label class="chk" style="margin:0 0 8px"><input type="checkbox" id="dl-retry"> retry failed only</label>
   <button class="b p" id="dl-start">Start / Resume</button><button class="b danger" id="dl-stop">Stop</button>
  </div>
  <h3>Activity</h3><pre class="log" id="dl-log"></pre>
  <div id="dl-failed"></div>
 </div></section>

 <section class="tab" id="t-tools"><div class="page">
  <h2 style="margin:0 0 6px;font-size:19px">Tools</h2>
  <p class="note">Run these after importing from the pen drive or after a pCloud sync. One task runs at a time; output appears below.</p>
  <div class="tools">
   <div class="card"><b>Build catalogue</b><p>Reads file names, folders and PDF metadata to index author, year, publisher, topic, language… Later runs are quicker.</p>
    <label><input type="checkbox" id="tb-nopdf"> skip reading PDF metadata (faster)</label><br><button class="b p" data-task="build">Build / update</button></div>
   <div class="card"><b>Check disk</b><p>Marks every book as on the server or missing by looking at the files really in the library folder.</p>
    <button class="b" data-task="reconcile">Check now</button></div>
   <div class="card"><b>Export</b><p>Save the full catalogue as a spreadsheet, then download it to your device.</p>
    <button class="b" data-task="xlsx">Make Excel</button> <a class="b" href="/export/xlsx">Download .xlsx</a><br><br>
    <button class="b" data-task="csv">Make CSV</button> <a class="b" href="/export/csv">Download .csv</a></div>
   <div class="card"><b>Offline catalogue page</b><p>A single HTML file listing every book — opens on any computer, no internet needed.</p>
    <button class="b" data-task="html">Make page</button> <a class="b" href="/export/html">Download</a></div>
  </div>
  <div style="display:flex;align-items:center;gap:12px;margin-top:20px"><h3 style="margin:0">Task output</h3><span id="tk-pill" class="pill idle">Idle</span><button class="b danger" id="tk-stop" style="padding:3px 10px">Stop</button></div>
  <pre class="log" id="tk-log" style="margin-top:8px"></pre>
  <h3>Users</h3>
  <div class="card"><p class="note" style="margin:0 0 8px">Readers can search, read and download books (one at a time, up to their <i>downloads a day</i> – 10 if left empty) until their <i>valid until</i> date (empty = no end). Click a user name to fill the form, <i>downloads</i> to see what they downloaded. Admins can also use pCloud sync, Tools and this list.</p>
   <table class="fl" id="u-list"></table>
   <div class="uform"><input type="text" id="u-name" placeholder="user name" autocomplete="off" autocapitalize="none"><input type="password" id="u-pw" placeholder="password (8+ characters)" autocomplete="new-password">
    <label class="chk" style="margin:0">valid until <input type="date" id="u-until"></label>
    <label class="chk" style="margin:0">downloads a day <input type="number" id="u-limit" min="0" max="10000" placeholder="10" style="width:70px"></label>
    <label class="chk" style="margin:0"><input type="checkbox" id="u-admin"> admin</label>
    <button class="b p" id="u-add">Add user</button><button class="b" id="u-pass" title="Set a new password for the user name typed here">Set password</button><button class="b" id="u-valid" title="Set the valid-until date for the user name typed here (empty date = no limit)">Set validity</button><button class="b" id="u-lim" title="Set the downloads a day for the user name typed here (empty = default)">Set limit</button></div>
   <div id="u-err" role="alert" style="display:none;margin-top:8px;padding:7px 10px;border-radius:6px;border:1px solid var(--bad);color:var(--bad);font-size:13px"></div>
   <div id="u-hist" style="display:none;margin-top:14px"><div style="display:flex;align-items:center;gap:10px"><b id="u-hist-t"></b><a class="lnk" id="u-hist-x">close</a></div>
    <div style="max-height:360px;overflow:auto;margin-top:6px"><table class="fl" id="u-hist-l"></table></div></div></div>
 </div></section>
</main>
<div class="modal" id="pw-box"><div class="card"><b>Change your password</b>
 <label>Current password</label><input type="password" id="pw-old" autocomplete="current-password">
 <label>New password (8+ characters)</label><input type="password" id="pw-new" autocomplete="new-password">
 <label>Repeat new password</label><input type="password" id="pw-new2" autocomplete="new-password">
 <div style="display:flex;gap:8px;margin-top:14px"><button class="b p" id="pw-go">Change</button><button class="b" id="pw-x">Cancel</button></div></div></div>
<div id="toast" style="position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:9px 16px;border-radius:8px;display:none;z-index:9;max-width:calc(100% - 32px)"></div>
<script>
const CSRF="__CSRF__",USER=__USER__,ADMIN=__ADMIN__;
const $=id=>document.getElementById(id);
const esc=s=>(s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const human=n=>{n=+n||0;for(const u of['B','KB','MB','GB','TB']){if(n<1024)return n.toFixed(u==='B'?0:1)+' '+u;n/=1024}return n.toFixed(1)+' PB'};
async function api(url,body){const o={headers:{'X-CSRF':CSRF}};if(body!==undefined){o.method='POST';o.body=JSON.stringify(body);o.headers['Content-Type']='application/json'}
 const r=await fetch(url,o);if(r.status===401){location.href='/login';throw new Error('signed out')}return r.json()}
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';clearTimeout(toast.h);toast.h=setTimeout(()=>t.style.display='none',3500)}
const fileUrl=(b,dl)=>'/file/'+b.id+'/'+encodeURIComponent(b.fname||'book')+(dl?'?dl=1':'');
$('me').textContent=USER+(ADMIN?' (admin)':'');
if(!ADMIN)document.querySelectorAll('.adm').forEach(x=>x.style.display='none');
$('logout').onclick=async()=>{await api('/api/logout',{});location.href='/login'};

// tabs
let tab='lib';document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.t;document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('.tab').forEach(s=>s.classList.toggle('on',s.id==='t-'+tab));if(tab==='tools')loadUsers();poll()});

// library
let page=0,total=0,sel=null,timer;
const F=['author','publisher','from','to','topic','tradition','type','lang','tag','avail'];
async function loadFacets(){const f=await api('/api/facets');if(f.lib)$('dl-lib').textContent=f.lib;
 if(!f.catalogue){$('list').innerHTML='<div class="empty">'+(ADMIN?(f.manifest?'The catalogue has not been built yet.<br><br><a class="lnk" onclick="goTools()">Go to Tools → Build catalogue</a>':
  'No library data on the server yet.<br>Import it from the pen drive with <b>sudo astrobooks import-usb</b>, or start a pCloud sync.'):'The library is being set up — please check back later.')+'</div>';$('hstat').textContent='';return false}
 const fill=(id,arr)=>{const s=$(id);s.length=1;arr.forEach(([k,n])=>{const o=document.createElement('option');o.value=k;o.textContent=k+' ('+n.toLocaleString()+')';s.appendChild(o)})};
 fill('f-topic',f.topics);fill('f-tradition',f.traditions);fill('f-type',f.types);fill('f-lang',f.languages);fill('f-tag',f.tags);
 const s=f.stats;$('hstat').innerHTML=s.total.toLocaleString()+' items · '+s.downloaded.toLocaleString()+' on the server<br>'+human(s.size)+' · years '+(s.ymin??'?')+'–'+(s.ymax??'?');return true}
function goTools(){document.querySelector('nav button[data-t=tools]').click()}
function query(){const o={q:$('q').value,sort:$('sort').value};F.forEach(k=>o[k]=$('f-'+k).value);return o}
async function search(){const p=new URLSearchParams({...query(),page});const r=await api('/api/search?'+p);
 if(r.error){$('list').innerHTML='<div class="empty">'+esc(r.error)+'</div>';$('count').textContent=$('also').textContent='';$('sl-addall').style.display='none';return}
 total=r.total;const ps=r.page_size;$('count').textContent=total.toLocaleString()+' result'+(total===1?'':'s');
 const al=Object.entries(r.also||{});$('also').textContent=al.length?' · also searched '+al.map(([w,a])=>w+' → '+a.join(', ')).join('; '):'';showShort(r.shortlist);
 $('sl-addall').style.display=total&&$('f-avail').value!=='short'?'':'none';$('sl-addall').textContent='☆ Add '+(total===1?'this result':'all '+total.toLocaleString()+' results')+' to shortlist';
 $('pg').textContent='Page '+(page+1)+' of '+Math.max(1,Math.ceil(total/ps));$('prev').disabled=page===0;$('next').disabled=(page+1)*ps>=total;
 rows=new Map(r.rows.map(b=>[b.id,b]));
 $('list').innerHTML=r.rows.length?r.rows.map(b=>'<div class="item'+(b.downloaded?'':' nd')+(b.id===sel?' sel':'')+'" data-id="'+b.id+'"><button class="star'+(b.short?' on':'')+'" title="'+(b.short?'Remove from':'Add to')+' shortlist">'+(b.short?'★':'☆')+'</button><div class="ic">'+esc(b.ext)+'</div><div style="min-width:0">'+
  '<div class="t">'+esc(b.title)+'</div><div class="a">'+[b.authors,b.year_text||b.year,b.publisher].filter(Boolean).map(esc).join(' · ')+'</div>'+
  '<div class="s">'+esc(b.topic)+(b.subtopic?' › '+esc(b.subtopic):'')+'</div><div>'+
  [b.doc_type,b.tradition,b.language].filter(Boolean).map(x=>'<span class="chip g">'+esc(x)+'</span>').join('')+
  (b.tags?b.tags.split(', ').map(x=>'<span class="chip">'+esc(x)+'</span>').join(''):'')+(b.downloaded?'':'<span class="chip g">not on server</span>')+
  '</div></div></div>').join(''):'<div class="empty">No matches. Try fewer words or clear some filters.</div>';
 $('list').scrollTop=0}
let rows=new Map();
$('list').onclick=e=>{const it=e.target.closest('.item');if(!it)return;
 if(e.target.closest('.star'))return toggleShort(+it.dataset.id,!e.target.classList.contains('on'));showBook(+it.dataset.id)};
$('list').ondblclick=e=>{const it=e.target.closest('.item');const b=it&&rows.get(+it.dataset.id);if(b&&b.downloaded&&!e.target.closest('.star'))window.open(fileUrl(b),'_blank')};
$('ftog').onclick=()=>$('filters').classList.add('on');$('fclose').onclick=()=>$('filters').classList.remove('on');

// shortlist
let short={count:0,pending:0};
function showShort(s){if(!s||s.error)return;short=s;$('sl-count').textContent=s.count.toLocaleString();const ready=s.count-s.pending;
 $('sl-pend').textContent=s.count?' · '+ready.toLocaleString()+' on the server'+(s.pending?' · '+s.pending.toLocaleString()+' not yet':''):' — click ☆ on any book to add it';
 $('sl-zip').textContent=ready?'Download ZIP ('+human(s.ready_bytes)+')':'Download ZIP';$('sl-zip').classList.toggle('off',!ready);
 $('sl-dl').textContent=s.pending?'Fetch '+s.pending.toLocaleString()+' from pCloud':'Fetch from pCloud';
 $('sl-dl').disabled=!s.pending;$('sl-view').disabled=$('sl-clear').disabled=!s.count}
$('sl-zip').onclick=e=>{if(short.ready_bytes>2*1073741824&&!confirmBox('The ZIP will be '+human(short.ready_bytes)+'.'))e.preventDefault()};
async function shortChange(body){const s=await api('/api/shortlist',body);if(s.error)return toast(s.error);showShort(s);return s}
async function toggleShort(id,on){if(!await shortChange(on?{add:[id]}:{remove:[id]}))return;
 document.querySelectorAll('.item[data-id="'+id+'"] .star').forEach(x=>{x.classList.toggle('on',on);x.textContent=on?'★':'☆';x.title=(on?'Remove from':'Add to')+' shortlist'});
 if(sel===id)showBook(id);if(!on&&$('f-avail').value==='short')search()}
$('sl-addall').onclick=async()=>{if(total>500&&!confirmBox('Add all '+total.toLocaleString()+' results to the shortlist?'))return;
 const s=await shortChange({add_query:query()});if(s){toast('Shortlist now has '+s.count.toLocaleString()+' book(s)');search()}};
$('sl-view').onclick=()=>{clearFilters();$('f-avail').value='short';go()};
$('sl-clear').onclick=async()=>{if(!confirmBox('Remove all '+short.count+' book(s) from the shortlist?'))return;await shortChange({clear:true});search()};
$('sl-dl').onclick=()=>startSelective({shortlist:true});
async function startSelective(body){const r=await api('/api/download/start',{...body,workers:+$('dl-workers').value||3});
 if(!r.ok)return toast(r.error);toast('Fetching '+r.count+' book(s) — progress is on the pCloud sync tab');pollDl()}
// in-page confirm (native dialogs block the page); "yes" on a second click
function confirmBox(msg){const t=Date.now();if(confirmBox.m===msg&&t-confirmBox.t<4000){confirmBox.m=null;return true}
 confirmBox.m=msg;confirmBox.t=t;toast(msg+' Click again to confirm.');return false}
async function showBook(id){sel=id;document.querySelectorAll('.item').forEach(x=>x.classList.toggle('sel',+x.dataset.id===id));
 const b=await api('/api/book?id='+id);if(b.error)return toast(b.error);const d=$('detail');d.classList.add('on');
 const rows=[['Year',b.year_text||b.year],['Publisher',b.publisher],['Type',b.doc_type],['Tradition',b.tradition],['Language',b.language],
  ['Topic',b.topic],['Sub-topic',b.subtopic],['Tags',b.tags],['Pages',b.pages],['Size',human(b.size)],['Format',(b.ext||'').toUpperCase()],
  ['PDF title',b.pdf&&b.pdf.pdf_title],['PDF author',b.pdf&&b.pdf.pdf_author]].filter(r=>r[1]!=null&&r[1]!=='');
 d.innerHTML='<div style="text-align:right"><a class="lnk" id="d-close">close ✕</a></div><h2>'+esc(b.title)+'</h2><div class="by">'+
  (b.authors?b.authors.split('; ').map(a=>'<a class="lnk" data-author="'+esc(a)+'">'+esc(a)+'</a>').join(', '):'Unknown author')+'</div>'+
  '<div class="acts">'+(b.exists?'<a class="b p" target="_blank" rel="noopener" href="'+esc(fileUrl(b))+'">Read</a><a class="b" href="'+esc(fileUrl(b,1))+'">Download</a>':
  (ADMIN?'<button class="b p" id="d-fetch">Fetch from pCloud</button>':''))+
  '<button class="b" id="d-short">'+(b.short?'★ Remove from shortlist':'☆ Add to shortlist')+'</button></div>'+
  (b.exists?'':'<div class="chip g" style="margin-top:8px">Not on the server yet</div>')+
  '<dl>'+rows.map(r=>'<dt>'+r[0]+'</dt><dd>'+esc(r[1])+'</dd>').join('')+'</dl>'+
  '<div><a class="lnk" data-topic="'+esc(b.topic)+'">More in this topic</a></div>'+(b.local_path?'<div class="path">'+esc(b.local_path)+'</div>':'');
 $('d-close').onclick=()=>d.classList.remove('on');$('d-short').onclick=()=>toggleShort(b.id,!b.short);
 if($('d-fetch'))$('d-fetch').onclick=()=>startSelective({ids:[b.id]});
 d.querySelectorAll('[data-author]').forEach(a=>a.onclick=()=>{clearFilters();$('f-author').value=a.dataset.author;d.classList.remove('on');go()});
 d.querySelector('[data-topic]').onclick=()=>{clearFilters();$('f-topic').value=b.topic;d.classList.remove('on');go()}}
function go(){page=0;search()}
function clearFilters(){$('q').value='';F.forEach(k=>$('f-'+k).value='')}
document.querySelectorAll('.filters input,.filters select,#q,#sort').forEach(e=>e.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(go,250)}));
$('reset').onclick=()=>{clearFilters();go()};$('prev').onclick=()=>{if(page>0){page--;search()}};$('next').onclick=()=>{page++;search()};

// password
$('pw-open').onclick=()=>{['pw-old','pw-new','pw-new2'].forEach(k=>$(k).value='');$('pw-box').classList.add('on');$('pw-old').focus()};
$('pw-x').onclick=()=>$('pw-box').classList.remove('on');
$('pw-go').onclick=async()=>{if($('pw-new').value!==$('pw-new2').value)return toast('The new passwords differ');
 const r=await api('/api/password',{old:$('pw-old').value,new:$('pw-new').value});if(!r.ok)return toast(r.error);
 toast('Password changed — please sign in again');setTimeout(()=>location.href='/login',1500)};

// pCloud sync (admin)
let lastBytes=null,lastT=null,speed=0;
async function pollDl(){if(!ADMIN)return false;const s=await api('/api/download');const c=s.counts||{},g=k=>c[k]||{files:0,bytes:0};
 const done=g('done'),pend=g('pending'),fail=g('failed'),skip=g('skipped');const totB=done.bytes+pend.bytes+fail.bytes,totF=done.files+pend.files+fail.files;
 const pct=totB?done.bytes/totB*100:0;$('dl-bar').style.width=pct.toFixed(2)+'%';$('dl-pct').textContent=totF?pct.toFixed(1)+'% on the server':'Not started yet';
 $('dl-bytes').textContent=totF?human(done.bytes)+' of '+human(totB)+' · '+done.files.toLocaleString()+' of '+totF.toLocaleString()+' files':'Press Start to fetch the file list and begin.';
 const now=Date.now();if(s.running&&lastBytes!=null&&now>lastT){const v=(done.bytes-lastBytes)/((now-lastT)/1000);speed=speed?speed*.7+v*.3:v}if(!s.running)speed=0;lastBytes=done.bytes;lastT=now;
 $('dl-speed').textContent=s.running&&speed>0?human(speed)+'/s · about '+((totB-done.bytes)/speed/3600).toFixed(1)+' h left':'';
 $('dl-cards').innerHTML=[['On the server',done],['Waiting',pend],['Failed',fail],['Skipped (junk)',skip]].map(([k,v])=>'<div class="card"><div class="k">'+k+'</div><div class="v">'+v.files.toLocaleString()+' <small>'+human(v.bytes)+'</small></div></div>').join('')+
  (s.disk?'<div class="card"><div class="k">Server disk free</div><div class="v">'+human(s.disk.free)+' <small>of '+human(s.disk.total)+'</small></div></div>':'');
 $('dl-pill').className='pill '+(s.running?'run':'idle');$('dl-pill').textContent=s.running?'Downloading…':'Idle';
 $('dl-start').disabled=s.running;$('dl-stop').disabled=!s.running;
 const lg=$('dl-log'),atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;lg.textContent=(s.log||[]).join('\n')||'No activity since the server started.'+(s.last_sync?'\nLast sync: '+s.last_sync:'');if(atEnd)lg.scrollTop=lg.scrollHeight;
 $('dl-failed').innerHTML=s.failed&&s.failed.length?'<h3>Failed files</h3><p class="note">Tick “retry failed only” and press Start to try them again.</p><table class="fl">'+s.failed.map(f=>'<tr><td>'+esc(f.local_path)+'</td><td style="color:var(--bad)">'+esc(f.error)+'</td></tr>').join('')+'</table>':'';
 if(s.running||dlWasRunning)showShort(await api('/api/shortlist'));                 // books are marked downloaded as they finish
 if(dlWasRunning&&!s.running){toast('pCloud sync finished');if(await loadFacets())search();if(sel)showBook(sel)}
 dlWasRunning=s.running;return s.running}
let dlWasRunning=false;
$('dl-start').onclick=async()=>{const r=await api('/api/download/start',{workers:+$('dl-workers').value||3,only:$('dl-only').value,retry_failed:$('dl-retry').checked});if(!r.ok)toast(r.error);else toast('pCloud sync started');pollDl()};
$('dl-stop').onclick=async()=>{await api('/api/download/stop',{});toast('Stopped — progress saved');pollDl()};

// tools (admin)
let taskWasRunning=false;
document.querySelectorAll('[data-task]').forEach(b=>b.onclick=async()=>{const r=await api('/api/task/start',{kind:b.dataset.task,no_pdf_meta:$('tb-nopdf').checked});
 if(!r.ok)toast(r.error);else pollTask()});
$('tk-stop').onclick=()=>api('/api/task/stop',{}).then(pollTask);
async function pollTask(){if(!ADMIN)return false;const s=await api('/api/task');$('tk-pill').className='pill '+(s.running?'run':'idle');$('tk-pill').textContent=s.running?'Running…':(s.returncode==null?'Idle':s.returncode===0?'Finished':'Finished with errors');
 $('tk-stop').disabled=!s.running;document.querySelectorAll('[data-task]').forEach(b=>b.disabled=s.running);
 const lg=$('tk-log');lg.textContent=(s.log||[]).join('\n')||'No task run since the server started.';lg.scrollTop=lg.scrollHeight;
 if(taskWasRunning&&!s.running){if(await loadFacets())go()}taskWasRunning=s.running;return s.running}
const today=()=>{const d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')};
const validity=u=>u.admin?'—':!u.valid_until?'no limit':u.valid_until<today()?'<span style="color:var(--bad)">expired '+esc(u.valid_until)+'</span>':esc(u.valid_until);
let defLimit=10;
const perDay=u=>u.today+' / '+(u.admin?'no limit':(u.daily_limit==null?defLimit:u.daily_limit));
function showUsers(us){$('u-list').innerHTML='<tr><td><b>User</b></td><td><b>Role</b></td><td><b>Valid until</b></td><td><b>Downloads today</b></td><td><b>Last sign-in</b></td><td></td></tr>'+us.map(u=>'<tr><td><a class="lnk" data-pick="'+esc(u.name)+'" data-until="'+esc(u.valid_until||'')+'" data-limit="'+(u.daily_limit==null?'':u.daily_limit)+'" title="Fill the form with this user">'+esc(u.name)+'</a></td><td>'+(u.admin?'admin':'reader')+'</td><td>'+validity(u)+'</td><td>'+perDay(u)+' · <a class="lnk" data-hist="'+esc(u.name)+'" title="Books this user downloaded">downloads ('+u.total+')</a></td><td>'+esc(u.last_login||'never')+'</td><td style="text-align:right">'+
  (u.name.toLowerCase()===USER.toLowerCase()?'<i style="color:var(--mute)">you</i>':'<a class="lnk" data-u="'+esc(u.name)+'" data-act="admin" data-admin="'+(u.admin?0:1)+'">'+(u.admin?'make reader':'make admin')+'</a> · <a class="lnk" data-u="'+esc(u.name)+'" data-act="delete" style="color:var(--bad)">delete</a>')+'</td></tr>').join('')}
async function loadUsers(){if(!ADMIN)return;const r=await api('/api/users');defLimit=r.default_limit||10;showUsers(r.users||[])}
function userErr(m){const e=$('u-err');e.textContent=m||'';e.style.display=m?'block':'none'}
async function userAct(body){const r=await api('/api/users',body);if(!r.ok){userErr(r.error);return false}userErr('');showUsers(r.users);toast('Done');return true}
['u-name','u-pw','u-until','u-limit'].forEach(id=>$(id).addEventListener('input',()=>userErr('')));
async function showHist(name){const r=await api('/api/downloads?user='+encodeURIComponent(name)),rows=r.rows||[];
 $('u-hist-t').textContent='Downloads by '+name+' ('+rows.length+(rows.length>=500?', latest':'')+')';$('u-hist').style.display='block';
 $('u-hist-l').innerHTML='<tr><td><b>When</b></td><td><b>Book</b></td><td><b>Size</b></td><td><b>Result</b></td><td><b>From</b></td></tr>'+(rows.map(d=>'<tr><td style="white-space:nowrap">'+esc(d.at)+'</td><td>'+esc(d.fname||'#'+d.book_id)+'</td><td style="white-space:nowrap">'+(d.size?human(d.size):'')+'</td><td>'+(d.done?'done':'<span style="color:var(--mute)">broken off</span>')+'</td><td>'+esc(d.ip||'')+'</td></tr>').join('')||'<tr><td colspan="5"><i>No downloads yet.</i></td></tr>')}
$('u-hist-x').onclick=()=>$('u-hist').style.display='none';
$('u-list').onclick=e=>{const h=e.target.closest('[data-hist]');if(h)return showHist(h.dataset.hist);
 const p=e.target.closest('[data-pick]');if(p){$('u-name').value=p.dataset.pick;$('u-until').value=p.dataset.until;$('u-limit').value=p.dataset.limit;return}
 const a=e.target.closest('[data-act]');if(!a)return;
 if(a.dataset.act==='delete'&&!confirmBox('Delete user '+a.dataset.u+'?'))return;userAct({action:a.dataset.act,name:a.dataset.u,admin:a.dataset.admin==='1'})};
$('u-add').onclick=async()=>{if(await userAct({action:'add',name:$('u-name').value.trim(),password:$('u-pw').value,admin:$('u-admin').checked,valid_until:$('u-until').value,daily_limit:$('u-limit').value})){$('u-name').value=$('u-pw').value=$('u-until').value=$('u-limit').value=''}};
$('u-lim').onclick=()=>userAct({action:'limit',name:$('u-name').value.trim(),daily_limit:$('u-limit').value});
$('u-valid').onclick=()=>userAct({action:'valid',name:$('u-name').value.trim(),valid_until:$('u-until').value});
$('u-pass').onclick=async()=>{if(await userAct({action:'passwd',name:$('u-name').value.trim(),password:$('u-pw').value}))$('u-pw').value=''};

async function poll(){clearTimeout(poll.h);let busy=false;
 try{const d=await pollDl(),t=await pollTask();busy=d||t}catch(e){}
 if(ADMIN)poll.h=setTimeout(poll,busy?2000:(tab==='lib'?15000:5000))}
(async()=>{if(await loadFacets())go();poll()})();
</script></body></html>"""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mimetypes.init()
    os.makedirs(LIB, exist_ok=True)
    AUTH.db()                                                   # create users.db early so permission problems show
    Handler.L = Library(LIB)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"Astro Library server on http://{BIND}:{PORT}/   library: {LIB}   accounts: {AUTH.DATA_DIR}", flush=True)
    print(f"Answering for: {', '.join(sorted(ALLOWED_HOSTS))}", flush=True)
    if not AUTH.list_users():
        print("No user accounts yet - create one:  astrobooks user add NAME --admin", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in (Handler.L.download, Handler.L.task):
            job.stop()
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
