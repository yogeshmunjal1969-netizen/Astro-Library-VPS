#!/usr/bin/env python3
"""
astro_auth.py - User accounts and login sessions for the Astro Library server.

    python astro_auth.py add NAME [--admin]     # asks for the password
    python astro_auth.py passwd NAME            # change a password (signs the user out everywhere)
    python astro_auth.py admin NAME on|off      # grant / revoke admin rights
    python astro_auth.py delete NAME
    python astro_auth.py list

Accounts live in  $ASTRO_DATA/users.db  (default /var/lib/astrobooks), separate from the library
so that re-importing the catalogue from the pen drive never touches them.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY COLLATE NOCASE, pw_hash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
    created TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user TEXT NOT NULL, csrf TEXT NOT NULL, created REAL, expires REAL, ip TEXT);
"""


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(os.path.join(DATA_DIR, "users.db"), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
def add_user(name, pw, admin=False):
    err = validate(name, pw)
    if err:
        return err
    con = db()
    if con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        return f"User '{name}' already exists."
    con.execute("INSERT INTO users(name, pw_hash, admin, created) VALUES (?,?,?,?)",
                (name, hash_password(pw), int(bool(admin)), now()))
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
    return [dict(r) for r in db().execute("SELECT name, admin, created, last_login FROM users ORDER BY name")]


# ----------------------------------------------------------------------------- sessions
def _th(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(name, pw, ip=""):
    """Returns (session token, user row) or (None, None)."""
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name or "",)).fetchone()
    if not check_password(pw or "", row["pw_hash"] if row else _DUMMY) or not row:
        return None, None
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
    r = con.execute("SELECT s.user, s.csrf, s.expires, u.admin FROM sessions s JOIN users u ON u.name=s.user "
                    "WHERE s.token_hash=?", (_th(token),)).fetchone()
    t = time.time()
    if not r or r["expires"] < t:
        return None
    if r["expires"] - t < (SESSION_DAYS - 1) * 86400:
        con.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (t + SESSION_DAYS * 86400, _th(token)))
        con.commit()
    return {"user": r["user"], "admin": bool(r["admin"]), "csrf": r["csrf"]}


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
    sub.add_parser("passwd").add_argument("name")
    m = sub.add_parser("admin"); m.add_argument("name"); m.add_argument("state", choices=["on", "off"])
    sub.add_parser("delete").add_argument("name")
    sub.add_parser("list")
    sub.add_parser("count-admins")
    a = ap.parse_args()

    if a.cmd == "list":
        users = list_users()
        for u in users:
            print(f"{u['name']:<24} {'admin' if u['admin'] else 'reader':<7} created {u['created'] or '-':<20} "
                  f"last login {u['last_login'] or 'never'}")
        if not users:
            print("No users yet.  Add one:  astrobooks user add NAME --admin")
        return
    if a.cmd == "count-admins":
        print(db().execute("SELECT COUNT(*) FROM users WHERE admin=1").fetchone()[0])
        return
    if a.cmd == "add":
        if validate(a.name):
            sys.exit(validate(a.name))
        err = add_user(a.name, ask_password(), a.admin)
    elif a.cmd == "passwd":
        err = set_password(a.name, ask_password())
    elif a.cmd == "admin":
        err = set_admin(a.name, a.state == "on")
    else:
        err = delete_user(a.name)
    if err:
        sys.exit(err)
    print("Done.")


if __name__ == "__main__":
    main()
