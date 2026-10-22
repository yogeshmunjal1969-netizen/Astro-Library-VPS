#!/usr/bin/env python3
"""
astro_maint.py - Housekeeping for the server copy of the library.

    python astro_maint.py finish-import [--old-db FILE]   # after the pen drive's library.db was copied in
    python astro_maint.py reconcile                       # mark books downloaded / missing from what is on disk
    python astro_maint.py check                           # integrity check + short summary

finish-import merges the pen drive's write-ahead log into library.db, checks the database,
brings back the web shortlists from the previous server database (--old-db) and then reconciles.
"""

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from astro_downloader import human  # noqa: E402

LIB = os.environ.get("ASTRO_LIB") or "/srv/astrobooks/library"


def open_lib(lib):
    path = os.path.join(lib, "_library", "library.db")
    if not os.path.exists(path):
        sys.exit(f"No catalogue database at {path}")
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    return db


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def check(db):
    res = db.execute("PRAGMA quick_check").fetchone()[0]
    if res != "ok":
        sys.exit(f"Database check FAILED: {res}")
    print("Database check: ok")


def reconcile(db, lib):
    """Sets books.downloaded / files.status from the files really present on this server's disk."""
    now_done = now_missing = 0
    for r in db.execute("SELECT fileid, local_path, size, status FROM files "
                        "WHERE status NOT IN ('skipped','removed') OR fileid < 0").fetchall():   # a pCloud sync
        # marks own books it does not know as 'removed' unless done; they come back when the file is there
        p = os.path.join(lib, r["local_path"])
        try:
            ok = os.path.getsize(p) == r["size"] or (r["fileid"] < 0 and os.path.isfile(p))
        except OSError:
            ok = False
        if ok and r["status"] != "done":
            db.execute("UPDATE files SET status='done', error=NULL WHERE fileid=?", (r["fileid"],))
            now_done += 1
        elif not ok and r["status"] == "done":
            # pCloud book -> pending, so the downloader fetches it again; own book (fileid < 0) -> missing
            db.execute("UPDATE files SET status=? WHERE fileid=?", ("pending" if r["fileid"] > 0 else "missing",
                                                                    r["fileid"]))
            now_missing += 1
    if has_table(db, "books"):
        db.execute("UPDATE books SET downloaded = COALESCE((SELECT status='done' FROM files f "
                   "WHERE f.fileid=books.fileid), 0)")
    db.commit()
    done, size = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='done'").fetchone()
    print(f"Reconciled with disk: {now_done} newly found, {now_missing} no longer on disk.  "
          f"On the server: {done} files, {human(size)}")


def restore_shortlists(db, old):
    if not old or not os.path.exists(old):
        return
    db.execute("CREATE TABLE IF NOT EXISTS web_shortlist (user TEXT, fileid INTEGER, added TEXT, "
               "PRIMARY KEY(user, fileid))")
    db.execute("ATTACH DATABASE ? AS old", (old,))
    try:
        if db.execute("SELECT 1 FROM old.sqlite_master WHERE name='web_shortlist'").fetchone():
            n = db.execute("INSERT OR IGNORE INTO web_shortlist SELECT user, fileid, added FROM old.web_shortlist").rowcount
            print(f"Restored {n} shortlist entries from the previous server database.")
        db.commit()
    finally:
        db.execute("DETACH DATABASE old")


def main():
    ap = argparse.ArgumentParser(description="Astro Library server housekeeping.")
    ap.add_argument("--lib", default=LIB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("finish-import").add_argument("--old-db")
    sub.add_parser("reconcile")
    sub.add_parser("check")
    a = ap.parse_args()
    lib = os.path.abspath(a.lib)

    db = open_lib(lib)
    if a.cmd == "finish-import":
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # fold the pen drive's -wal file into library.db
        check(db)
        restore_shortlists(db, a.old_db)
        reconcile(db, lib)
    elif a.cmd == "reconcile":
        reconcile(db, lib)
    else:
        check(db)
        books = db.execute("SELECT COUNT(*) FROM books").fetchone()[0] if has_table(db, "books") else 0
        print(f"Catalogue: {books} books" if books else "Catalogue not built yet (Tools -> Build catalogue).")


if __name__ == "__main__":
    main()
