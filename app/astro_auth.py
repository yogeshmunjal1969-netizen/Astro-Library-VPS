#!/usr/bin/env python3
"""
astro_auth.py - User accounts and login sessions for the Astro Library server.

    python astro_auth.py add NAME [--admin] [--until YYYY-MM-DD]   # asks for the password
    python astro_auth.py valid NAME YYYY-MM-DD|none   # last day a reader may use the library
    python astro_auth.py limit NAME N|default         # downloads a reader may make per day (default 10)
    python astro_auth.py downloads [NAME]             # the latest downloads, of everyone or of one user
    python astro_auth.py passwd NAME            # change a password (signs the user out everywhere)
    python astro_auth.py admin NAME on|off      # grant / revoke admin rights
    python astro_auth.py delete NAME
    python astro_auth.py list

Accounts live in  $ASTRO_DATA/users.db  (default /var/lib/astrobooks), separate from the library
so that re-importing the catalogue from the pen drive never touches them.
Readers whose valid_until date has passed cannot sign in and are signed out; admins never expire.
Readers may complete DAILY_DOWNLOADS downloads a day unless their account has its own daily_limit;
admins have no limit. Every download (by anyone) is recorded in the downloads table.
Passwords are stored as PBKDF2-SHA256 hashes; session cookies are stored only as SHA-256 hashes.
"""

import argparse
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import time

DATA_DIR = os.environ.get("ASTRO_DATA") or "/var/lib/astrobooks"
ITERATIONS = 390_000
SESSION_DAYS = 30
NAME_RE = re.compile(r"^[A-Za-z0-9._@-]{2,40}$")
MIN_PASSWORD = 8
DAILY_DOWNLOADS = 10
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY COLLATE NOCASE, pw_hash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
    created TEXT, last_login TEXT, valid_until TEXT, daily_limit INTEGER);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user TEXT NOT NULL, csrf TEXT NOT NULL, created REAL, expires REAL, ip TEXT);
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY, user TEXT NOT NULL COLLATE NOCASE, book_id INTEGER, fileid INTEGER, fname TEXT,
    size INTEGER, at TEXT, day TEXT, ip TEXT, done INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS downloads_user_day ON downloads(user, day);
"""


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(os.path.join(DATA_DIR, "users.db"), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}      # accounts made before these columns
    for col, kind in (("valid_until", "TEXT"), ("daily_limit", "INTEGER")):
        if col not in cols:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} {kind}")
    return con


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def expired(row):
    """True for a reader whose last valid day is before today."""
    return not row["admin"] and bool(row["valid_until"]) and row["valid_until"] < time.strftime("%Y-%m-%d")


def check_date(until):
    """(date or None, error): '' / 'none' means no limit."""
    until = (until or "").strip()
    if until.lower() in ("", "none"):
        return None, None
    try:
        if DATE_RE.match(until):
            time.strptime(until, "%Y-%m-%d")
            return until, None
    except ValueError:
        pass
    return None, "Valid until: give a date as YYYY-MM-DD."


# ----------------------------------------------------------------------------- passwords
def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, it, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


_DUMMY = hash_password(secrets.token_hex(8))           # equalises timing for unknown user names


def validate(name, pw=None):
    if not NAME_RE.match(name or ""):
        return "User name: 2-40 letters, digits or . _ @ -"
    if pw is not None and len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    return None


# ----------------------------------------------------------------------------- users
def add_user(name, pw, admin=False, until=None):
    until, err = check_date(until)
    err = validate(name, pw) or err
    if err:
        return err
    con = db()
    if con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        return f"User '{name}' already exists."
    con.execute("INSERT INTO users(name, pw_hash, admin, created, valid_until) VALUES (?,?,?,?,?)",
                (name, hash_password(pw), int(bool(admin)), now(), until))
    con.commit()
    return None


def set_password(name, pw):
    err = validate(name, pw)
    if err:
        return err
    con = db()
    if not con.execute("UPDATE users SET pw_hash=? WHERE name=?", (hash_password(pw), name)).rowcount:
        return f"No user '{name}'."
    con.execute("DELETE FROM sessions WHERE user=?", (name,))
    con.commit()
    return None


def set_admin(name, admin):
    con = db()
    if not admin and con.execute("SELECT COUNT(*) FROM users WHERE admin=1 AND name<>?", (name,)).fetchone()[0] == 0:
        return "Cannot remove the last admin."
    if not con.execute("UPDATE users SET admin=? WHERE name=?", (int(bool(admin)), name)).rowcount:
        return f"No user '{name}'."
    con.commit()
    return None


def set_validity(name, until):
    until, err = check_date(until)
    if err:
        return err
    con = db()
    if not con.execute("UPDATE users SET valid_until=? WHERE name=?", (until, name)).rowcount:
        return f"No user '{name}'."
    con.commit()
    return None


def set_limit(name, limit):
    """limit: a whole number of downloads per day, or ''/'default' for DAILY_DOWNLOADS."""
    limit = str(limit if limit is not None else "").strip().lower()
    if limit in ("", "default"):
        limit = None
    elif limit.isdigit() and int(limit) <= 10000:
        limit = int(limit)
    else:
        return "Daily limit: give a number of downloads (0-10000), or leave it empty for the default."
    con = db()
    if not con.execute("UPDATE users SET daily_limit=? WHERE name=?", (limit, name)).rowcount:
        return f"No user '{name}'."
    con.commit()
    return None


def delete_user(name):
    con = db()
    row = con.execute("SELECT admin FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        return f"No user '{name}'."
    if row["admin"] and con.execute("SELECT COUNT(*) FROM users WHERE admin=1").fetchone()[0] == 1:
        return "Cannot delete the last admin."
    con.execute("DELETE FROM users WHERE name=?", (name,))
    con.execute("DELETE FROM sessions WHERE user=?", (name,))
    con.commit()
    return None


def list_users():
    today = time.strftime("%Y-%m-%d")
    return [dict(r) for r in db().execute(
        "SELECT name, admin, created, last_login, valid_until, daily_limit, "
        "(SELECT COUNT(*) FROM downloads d WHERE d.user=u.name AND d.day=? AND d.done=1) AS today, "
        "(SELECT COUNT(*) FROM downloads d WHERE d.user=u.name AND d.done=1) AS total "
        "FROM users u ORDER BY name", (today,))]


# ----------------------------------------------------------------------------- downloads
def limit_of(row):
    """Downloads a day the account may complete; None = no limit (admins)."""
    if row["admin"]:
        return None
    return DAILY_DOWNLOADS if row["daily_limit"] is None else row["daily_limit"]


def downloads_today(name):
    return db().execute("SELECT COUNT(*) FROM downloads WHERE user=? AND day=? AND done=1",
                        (name, time.strftime("%Y-%m-%d"))).fetchone()[0]


def download_started(name, book, ip=""):
    con = db()
    cur = con.execute("INSERT INTO downloads(user, book_id, fileid, fname, size, at, day, ip) VALUES (?,?,?,?,?,?,?,?)",
                      (name, book.get("id"), book.get("fileid"), book.get("fname"), book.get("size"), now(),
                       time.strftime("%Y-%m-%d"), ip))
    con.commit()
    return cur.lastrowid


def download_finished(rec_id, done):
    con = db()
    con.execute("UPDATE downloads SET done=? WHERE id=?", (int(bool(done)), rec_id))
    con.commit()


def download_history(name=None, limit=200):
    sql = "SELECT user, book_id, fname, size, at, ip, done FROM downloads"
    args = ()
    if name:
        sql, args = sql + " WHERE user=?", (name,)
    return [dict(r) for r in db().execute(sql + " ORDER BY id DESC LIMIT ?", args + (limit,))]


# ----------------------------------------------------------------------------- sessions
def _th(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(name, pw, ip=""):
    """Returns (session token, user row); (None, row) if the password is right but the access has expired;
    (None, None) for a wrong user name or password."""
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name or "",)).fetchone()
    if not check_password(pw or "", row["pw_hash"] if row else _DUMMY) or not row:
        return None, None
    if expired(row):
        return None, dict(row)
    token = secrets.token_urlsafe(32)
    t = time.time()
    con.execute("DELETE FROM sessions WHERE expires < ?", (t,))
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                (_th(token), row["name"], secrets.token_urlsafe(24), t, t + SESSION_DAYS * 86400, ip))
    con.execute("UPDATE users SET last_login=? WHERE name=?", (now(), row["name"]))
    con.commit()
    return token, dict(row)


def session(token):
    """dict(user, admin, csrf) for a valid session token, else None. Sessions slide: each use extends them."""
    if not token:
        return None
    con = db()
    r = con.execute("SELECT s.user, s.csrf, s.expires, u.admin, u.valid_until, u.daily_limit FROM sessions s JOIN users u ON u.name=s.user "
                    "WHERE s.token_hash=?", (_th(token),)).fetchone()
    t = time.time()
    if not r or r["expires"] < t or expired(r):
        return None
    if r["expires"] - t < (SESSION_DAYS - 1) * 86400:
        con.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (t + SESSION_DAYS * 86400, _th(token)))
        con.commit()
    return {"user": r["user"], "admin": bool(r["admin"]), "csrf": r["csrf"], "limit": limit_of(r)}


def logout(token):
    con = db()
    con.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token or ""),))
    con.commit()


# ----------------------------------------------------------------------------- CLI
def ask_password():
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < MIN_PASSWORD:
            print(f"At least {MIN_PASSWORD} characters, please.")
            continue
        if getpass.getpass("Repeat password: ") == pw:
            return pw
        print("The two passwords differ - try again.")


def main():
    ap = argparse.ArgumentParser(description="Manage Astro Library user accounts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--admin", action="store_true")
    a.add_argument("--until", help="last day a reader may use the library (YYYY-MM-DD)")
    sub.add_parser("passwd").add_argument("name")
    m = sub.add_parser("admin"); m.add_argument("name"); m.add_argument("state", choices=["on", "off"])
    n = sub.add_parser("limit"); n.add_argument("name"); n.add_argument("count", help="downloads per day, or default")
    sub.add_parser("downloads").add_argument("name", nargs="?")
    v = sub.add_parser("valid"); v.add_argument("name"); v.add_argument("until", help="YYYY-MM-DD or none")
    sub.add_parser("delete").add_argument("name")
    sub.add_parser("list")
    sub.add_parser("count-admins")
    a = ap.parse_args()

    if a.cmd == "list":
        users = list_users()
        for u in users:
            until = "" if u["admin"] else f"  valid until {u['valid_until'] or 'no limit'}" + \
                (" (EXPIRED)" if expired(u) else "")
            lim = limit_of(u)
            per_day = f"  downloads today {u['today']}/{'no limit' if lim is None else lim}, total {u['total']}"
            print(f"{u['name']:<24} {'admin' if u['admin'] else 'reader':<7} created {u['created'] or '-':<20} "
                  f"last login {u['last_login'] or 'never'}{until}{per_day}")
        if not users:
            print("No users yet.  Add one:  astrobooks user add NAME --admin")
        return
    if a.cmd == "downloads":
        rows = download_history(a.name)
        for d in reversed(rows):
            print(f"{d['at']}  {d['user']:<16} {'done' if d['done'] else 'broken off':<10} {d['fname']}")
        if not rows:
            print("No downloads recorded.")
        return
    if a.cmd == "count-admins":
        print(db().execute("SELECT COUNT(*) FROM users WHERE admin=1").fetchone()[0])
        return
    if a.cmd == "add":
        err = validate(a.name) or check_date(a.until)[1]
        if err:
            sys.exit(err)
        err = add_user(a.name, ask_password(), a.admin, a.until)
    elif a.cmd == "passwd":
        err = set_password(a.name, ask_password())
    elif a.cmd == "limit":
        err = set_limit(a.name, a.count)
    elif a.cmd == "valid":
        err = set_validity(a.name, a.until)
    elif a.cmd == "admin":
        err = set_admin(a.name, a.state == "on")
    else:
        err = delete_user(a.name)
    if err:
        sys.exit(err)
    print("Done.")


if __name__ == "__main__":
    main()
