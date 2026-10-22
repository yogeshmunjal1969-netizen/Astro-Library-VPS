#!/usr/bin/env python3
"""
astro_import.py - Bring books you already have (e.g. E:\\Astro Books) into the library on F:.

    python astro_import.py --src "E:\\Astro Books"            # copy
    python astro_import.py --src "E:\\Astro Books" --dry-run  # only report what would happen

For every file in --src:
  * if it is one of the pCloud books (same size + same name, same size and the size is unique, or
    same name with the size within 5%),
    it is copied to the exact place astro_downloader.py would put it and marked 'done' in the
    manifest, so it is never downloaded again;
  * otherwise it is copied to "<lib>\\My Books (<src name>)\\..." and added to the manifest as a
    local-only book, so the catalogue (astro_library.py build) can index and search it.
Files are copied, never moved: the source is left untouched. Re-running skips what is already copied.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from astro_downloader import DEFAULT_DEST, human, long_path, open_db, safe_name, should_skip  # noqa: E402

MIN_SIZE_ONLY = 200 * 1024          # a size-only match must be at least this big to be trusted
NEAR_PCT = 0.05                     # same file name and size within 5%: same book, re-saved (kept instead of downloading)


def norm(name):
    return safe_name(name).lower()


def scan(src):
    out = []
    for root, dirs, files in os.walk(long_path(src)):
        dirs[:] = [d for d in dirs if not d.startswith((".", "$"))]
        for f in files:
            if should_skip(f, None) or f.endswith(".part"):
                continue
            p = os.path.join(root, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size:
                rel = os.path.relpath(p, long_path(src)).replace(os.sep, "/")
                out.append((p, rel, f, size))
    return out


def copy(src, dst, size):
    """Copy via a .part file; returns True if bytes were copied, False if dst was already complete."""
    if os.path.exists(dst) and os.path.getsize(dst) == size:
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = dst + ".part"
    shutil.copyfile(src, part)
    if os.path.getsize(part) != size:
        os.remove(part)
        raise RuntimeError("size mismatch after copy")
    os.replace(part, dst)
    shutil.copystat(src, dst)
    return True


def main():
    ap = argparse.ArgumentParser(description="Import already-downloaded books into the library.")
    ap.add_argument("--src", required=True, help="folder with your existing books")
    ap.add_argument("--lib", default=DEFAULT_DEST, help=f"library folder (default {DEFAULT_DEST})")
    ap.add_argument("--dry-run", action="store_true", help="only report, copy nothing")
    ap.add_argument("--no-near", dest="near", action="store_false",
                    help=f"only exact-size matches (default also accepts same name with size within {NEAR_PCT:.0%})")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    src, lib = os.path.abspath(a.src), os.path.abspath(a.lib)
    db = open_db(lib)
    t0 = time.time()
    print(f"Scanning {src} ...", flush=True)
    files = scan(src)
    print(f"  {len(files)} files, {human(sum(f[3] for f in files))}  ({time.time() - t0:.0f}s)", flush=True)

    by_size, by_name = defaultdict(list), defaultdict(list)
    for fid, lp, name, size, status in db.execute("SELECT fileid, local_path, name, size, status FROM files "
                                                  "WHERE fileid > 0 AND status <> 'removed'"):
        by_size[size].append((fid, lp, name, status))
        by_name[norm(name)].append((fid, lp, name, status, size))

    def near(name, size):
        """Same name, size within NEAR_PCT, and the name means one book on pCloud (all copies of similar size)."""
        cands = [c for c in by_name.get(norm(name), []) if c[0] not in claimed]
        if cands and all(c[4] and abs(size - c[4]) <= c[4] * NEAR_PCT for c in cands):
            return [c[:4] for c in cands]
        return []

    matched, extra, claimed, n_near = [], [], set(), 0
    # pass 1: exact name+size (all of them first), then a unique size; pass 2: near matches for the rest
    for exact_only in (True, False):
        rest = []
        for p, rel, name, size in files:
            cands = [c for c in by_size.get(size, []) if c[0] not in claimed]
            named = [c for c in cands if norm(c[2]) == norm(name)]
            ext = os.path.splitext(name)[1].lower()
            if named:
                hit = named
            elif exact_only:
                hit = []
            elif len(cands) == 1 and size >= MIN_SIZE_ONLY and os.path.splitext(cands[0][2])[1].lower() == ext:
                hit = cands
            else:
                hit = near(name, size) if a.near else []
                n_near += bool(hit)
            # same file listed several times on pCloud with identical name+size: fill all of them
            if hit:
                claimed.update(c[0] for c in hit)
                matched.append((p, size, hit))
            else:
                rest.append((p, rel, name, size))
        files = rest
    extra = files

    need = [(p, s, [c for c in h if c[3] != "done"]) for p, s, h in matched]
    need = [x for x in need if x[2]]
    m_bytes = sum(s * len(h) for _, s, h in need)
    e_bytes = sum(f[3] for f in extra)
    pending = db.execute("SELECT COALESCE(SUM(size),0) FROM files WHERE status IN ('pending','failed')").fetchone()[0]
    free = shutil.disk_usage(lib).free
    print(f"\nMatched to pCloud books : {len(matched)} files ({len(matched) - len(need)} already in the library, "
          f"{n_near} same name with size within {NEAR_PCT:.0%})")
    print(f"  to copy               : {sum(len(h) for _, _, h in need)} files, {human(m_bytes)}  -> saves that much downloading")
    print(f"Not in pCloud collection: {len(extra)} files, {human(e_bytes)}  -> My Books ({os.path.basename(src)})")
    print(f"Disk: {human(free)} free on library drive; still to download after import: {human(pending - m_bytes)}")
    if m_bytes + e_bytes + max(0, pending - m_bytes) > free:
        print("WARNING: import + remaining downloads will not fit on the library drive.")
    if a.dry_run:
        for p, rel, name, size in extra[:30]:
            print(f"   extra {human(size):>10}  {rel}")
        return

    now = lambda: datetime.now().isoformat(timespec="seconds")
    has_books = db.execute("SELECT 1 FROM sqlite_master WHERE name='books'").fetchone() is not None
    done_n = done_b = 0
    total_b = m_bytes + e_bytes

    def progress(label):
        rate = done_b / max(time.time() - t1, 1)
        print(f"[{done_n}] {human(done_b)} of {human(total_b)} ({human(rate)}/s)  {label}", flush=True)

    t1 = time.time()
    for p, size, hits in need:                                      # 1) pCloud books
        first = None
        for fid, lp, name, status in hits:
            dst = long_path(os.path.join(lib, lp.replace("/", os.sep)))
            try:
                if first and os.path.exists(first):
                    try:                                            # duplicate entry: hard link, no extra space
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.link(first, dst)
                    except OSError:
                        copy(p, dst, size)
                else:
                    copy(p, dst, size)
                first = first or dst
            except Exception as e:
                print(f"FAILED {p}: {e}", flush=True)
                continue
            db.execute("UPDATE files SET status='done', error=NULL, downloaded_at=? WHERE fileid=?", (now(), fid))
            if has_books:
                db.execute("UPDATE books SET downloaded=1 WHERE fileid=?", (fid,))
            db.commit()
            done_n += 1
            done_b += size
            progress(lp)

    folder = safe_name(f"My Books ({os.path.basename(src)})")
    next_id = min(0, db.execute("SELECT COALESCE(MIN(fileid),0) FROM files").fetchone()[0]) - 1
    for p, rel, name, size in extra:                                # 2) books pCloud does not have
        lp = folder + "/" + "/".join(safe_name(x) for x in rel.split("/"))
        dst = long_path(os.path.join(lib, lp.replace("/", os.sep)))
        try:
            copy(p, dst, size)
        except Exception as e:
            print(f"FAILED {p}: {e}", flush=True)
            continue
        if db.execute("SELECT 1 FROM files WHERE local_path=?", (lp,)).fetchone() is None:
            db.execute("INSERT INTO files(fileid,remote_path,local_path,name,size,status,downloaded_at,seen_at)"
                       " VALUES (?,?,?,?,?,'done',?,?)", (next_id, rel, lp, name, size, now(), now()))
            next_id -= 1
        db.commit()
        done_n += 1
        done_b += size
        progress(lp)

    print(f"\nImport finished: {done_n} files, {human(done_b)} in {(time.time() - t1) / 60:.0f} min.")
    print("Next: Tools -> Build catalogue, so the imported books become searchable.")


if __name__ == "__main__":
    main()
