#!/usr/bin/env bash
# Copy the Astro Library from the pen drive (plugged into this server) to the server's storage.
#
#   sudo astrobooks import-usb                 # finds the pen drive; asks if more than one USB drive is plugged in
#   sudo astrobooks import-usb /dev/sdb1       # use this partition (see: lsblk)
#   sudo astrobooks import-usb --dry-run       # only show what would be copied
#   sudo astrobooks import-usb --no-db         # copy books only, keep the server's catalogue database
#   sudo astrobooks import-usb --src-dir "Some Folder"   # library folder on the drive (default: found automatically)
#
# The drive is mounted READ-ONLY - nothing on it is changed. Safe to re-run: files already copied are
# skipped and an interrupted copy resumes. The copy of ~100 GB takes 1-2 hours on USB 3 (longer on USB 2),
# so run it inside "tmux" (or "screen") when you are connected over SSH.
#
# Steps: 1. copy all books with rsync   2. stop the web server, replace the catalogue database with the
# pen drive's one (the previous server database is kept as a backup and its shortlists are carried over)
# 3. mark which books are on the server   4. start the web server again.
set -euo pipefail

ENV_FILE=/etc/astrobooks/astrobooks.env
APP_DIR=/opt/astrobooks
SVC_USER=astrobooks
MNT=/mnt/astrobooks-usb

say()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
human() { numfmt --to=iec --suffix=B "$1"; }

DEV="" SRC_SUB="" DRY=0 NODB=0 FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n) DRY=1; shift ;;
        --no-db)      NODB=1; shift ;;
        --src-dir)    SRC_SUB="${2:?--src-dir needs a folder}"; shift 2 ;;
        --force)      FORCE=1; shift ;;
        -h|--help)    sed -n '2,18p' "$0"; exit 0 ;;
        /dev/*)       DEV="$1"; shift ;;
        *)            die "unknown option: $1  (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run with sudo:  sudo astrobooks import-usb"
[[ -f $ENV_FILE ]] || die "$ENV_FILE not found - run deploy/install.sh first"
set -a; . "$ENV_FILE"; set +a
LIB="${ASTRO_LIB:?}"
[[ -n ${TMUX:-}${STY:-} || ! -n ${SSH_CONNECTION:-} ]] || warn "you are on SSH without tmux/screen - if the connection drops the copy stops (re-running resumes it)"

# --------------------------------------------------------------------------- find the pen drive
if [[ -z $DEV ]]; then
    say "Looking for USB drives"
    mapfile -t disks < <(lsblk -dpnP -o NAME,TRAN,HOTPLUG | grep -E 'TRAN="usb"|HOTPLUG="1"' | sed -E 's/^NAME="([^"]+)".*/\1/' || true)
    parts=()
    for d in "${disks[@]}"; do
        while IFS= read -r line; do
            parts+=("$line")
        done < <(lsblk -pnP -o NAME,FSTYPE,SIZE,LABEL "$d" | grep -v 'FSTYPE=""' || true)
    done
    [[ ${#parts[@]} -gt 0 ]] || die "no USB drive found. Plug in the pen drive, wait a few seconds and try again,
       or name the partition yourself (find it with 'lsblk'):  sudo astrobooks import-usb /dev/sdb1"
    if [[ ${#parts[@]} -eq 1 ]]; then
        choice=0
    else
        for i in "${!parts[@]}"; do echo "  $((i + 1))) ${parts[$i]}"; done
        read -rp "Which one holds the library? [1-${#parts[@]}]: " n
        [[ $n =~ ^[0-9]+$ && $n -ge 1 && $n -le ${#parts[@]} ]] || die "no drive chosen"
        choice=$((n - 1))
    fi
    DEV="$(sed -E 's/^NAME="([^"]+)".*/\1/' <<<"${parts[$choice]}")"
    echo "using ${parts[$choice]}"
fi
[[ -b $DEV ]] || die "$DEV is not a block device"

# --------------------------------------------------------------------------- mount read-only
ROOT="$(lsblk -no MOUNTPOINT "$DEV" | head -n1)"
MOUNTED_HERE=0
cleanup() {
    if [[ $MOUNTED_HERE -eq 1 ]]; then
        sync; umount "$MNT" 2>/dev/null && rmdir "$MNT" 2>/dev/null && echo "Pen drive unmounted - you can unplug it."
    fi
    systemctl is-active --quiet astrobooks || systemctl start astrobooks || true
}
trap cleanup EXIT
if [[ -n $ROOT ]]; then
    echo "already mounted at $ROOT"
else
    mkdir -p "$MNT"
    fstype="$(lsblk -no FSTYPE "$DEV" | head -n1)"
    if [[ $fstype == ntfs ]]; then
        mount -t ntfs-3g -o ro "$DEV" "$MNT" 2>/dev/null || mount -o ro "$DEV" "$MNT" \
            || die "could not mount $DEV. On Windows use 'Safely Remove / Eject' before unplugging, then try again."
    else
        mount -o ro "$DEV" "$MNT" || die "could not mount $DEV ($fstype)"
    fi
    MOUNTED_HERE=1
    ROOT="$MNT"
    echo "mounted $DEV ($fstype) read-only at $MNT"
fi

# --------------------------------------------------------------------------- locate the library folder
if [[ -n $SRC_SUB ]]; then
    SRC="$ROOT/${SRC_SUB#/}"
elif [[ -f $ROOT/AstroBooksCollection/_library/library.db ]]; then
    SRC="$ROOT/AstroBooksCollection"
elif [[ -f $ROOT/_library/library.db ]]; then
    SRC="$ROOT"
else
    db="$(find "$ROOT" -maxdepth 4 -path '*/_library/library.db' -print -quit 2>/dev/null || true)"
    [[ -n $db ]] || die "no Astro Library found on $DEV (looked for AstroBooksCollection/_library/library.db)"
    SRC="$(dirname "$(dirname "$db")")"
fi
[[ -d $SRC ]] || die "$SRC does not exist"
echo "library on the pen drive: $SRC"

# --------------------------------------------------------------------------- space check
say "Checking space"
mkdir -p "$LIB"
src_b=$(du -sb --exclude=_Browse "$SRC" | cut -f1)
have_b=$(du -sb "$LIB" | cut -f1)
free_b=$(df -B1 --output=avail "$LIB" | tail -n1)
need_b=$(( src_b > have_b ? src_b - have_b : 0 ))
echo "pen drive: $(human "$src_b")   already on the server: $(human "$have_b")   free: $(human "$free_b")"
if (( need_b > free_b )) && [[ $FORCE -eq 0 ]]; then
    die "not enough space in $LIB (need about $(human "$need_b")). Free some space or use --force to try anyway."
fi

# --------------------------------------------------------------------------- 1. books
RSYNC=(rsync -rt --modify-window=2 --partial --partial-dir=.rsync-partial --chmod=D755,F644 --human-readable
       --exclude=/_library/ --exclude=/_Browse/ --exclude='*.part' --exclude=.rsync-partial/
       --exclude='/System Volume Information/' --exclude='/$RECYCLE.BIN/')
if [[ $DRY -eq 1 ]]; then
    say "Dry run - files that would be copied"
    "${RSYNC[@]}" --dry-run --stats --itemize-changes "$SRC/" "$LIB/" | tail -n 40
    exit 0
fi
say "1/3  Copying books to $LIB  (Ctrl+C to stop; run again to resume)"
"${RSYNC[@]}" --info=progress2 --no-inc-recursive "$SRC/" "$LIB/"

# --------------------------------------------------------------------------- 2. catalogue database
DST_DB_DIR="$LIB/_library"
SRC_DB="$SRC/_library/library.db"
OLD_DB=""
mkdir -p "$DST_DB_DIR"
if [[ $NODB -eq 1 || ! -f $SRC_DB ]]; then
    [[ -f $SRC_DB ]] || warn "no catalogue database on the pen drive - keeping the server's"
    say "2/3  Keeping the server's catalogue database"
else
    say "2/3  Installing the pen drive's catalogue database"
    systemctl stop astrobooks
    if [[ -f $DST_DB_DIR/library.db ]]; then
        OLD_DB="$DST_DB_DIR/library.db.before-import-$(date +%Y%m%d-%H%M%S)"
        sqlite3 "$DST_DB_DIR/library.db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
        mv "$DST_DB_DIR/library.db" "$OLD_DB"
        echo "previous server database kept as $(basename "$OLD_DB")"
    fi
    # a stale -wal next to a new database would corrupt it: remove first, then copy db + its own -wal
    rm -f "$DST_DB_DIR/library.db-wal" "$DST_DB_DIR/library.db-shm"
    cp "$SRC_DB" "$DST_DB_DIR/library.db.importing"
    [[ -f $SRC_DB-wal ]] && cp "$SRC_DB-wal" "$DST_DB_DIR/library.db.importing-wal"
    mv "$DST_DB_DIR/library.db.importing" "$DST_DB_DIR/library.db"
    [[ -f $DST_DB_DIR/library.db.importing-wal ]] && mv "$DST_DB_DIR/library.db.importing-wal" "$DST_DB_DIR/library.db-wal"
    rsync -rt --exclude='library.db*' --exclude=download_selection.txt "$SRC/_library/" "$DST_DB_DIR/"
fi

# --------------------------------------------------------------------------- 3. permissions + reconcile
say "3/3  Finishing"
chown -R "$SVC_USER:$SVC_USER" "$LIB"
if [[ -f $DST_DB_DIR/library.db ]]; then
    args=(--lib "$LIB" finish-import)
    [[ -n $OLD_DB ]] && args+=(--old-db "$OLD_DB")
    (cd / && runuser -u "$SVC_USER" -- env ASTRO_LIB="$LIB" ASTRO_DATA="${ASTRO_DATA:-/var/lib/astrobooks}" \
        "$APP_DIR/venv/bin/python" "$APP_DIR/app/astro_maint.py" "${args[@]}")
fi
systemctl start astrobooks
echo
echo "Import complete. Open https://astrobooks.munjal.tech"
echo "If you added or renamed books on the pen drive since its last catalogue build, run:  sudo astrobooks build"
