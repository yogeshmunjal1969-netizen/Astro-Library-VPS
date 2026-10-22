#!/usr/bin/env bash
# Install (or update) the Astro Library server on Ubuntu / Debian.
#
#   sudo ./deploy/install.sh                          # first install - asks a few questions
#   sudo ./deploy/install.sh --tunnel-token TOKEN     # ... and connect the Cloudflare Tunnel in one go
#   sudo ./deploy/install.sh --lib /mnt/data/astrobooks   # keep the books on another disk
#   sudo astrobooks update                            # later: pull from GitHub and re-run this
#
# Safe to run again: existing settings, accounts and books are kept.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/astrobooks
CONF_DIR=/etc/astrobooks
ENV_FILE=$CONF_DIR/astrobooks.env
SVC_USER=astrobooks
TUNNEL_TOKEN="${CF_TUNNEL_TOKEN:-}"
LIB_ARG=""
ASSUME_YES=0

say()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
interactive() { [[ $ASSUME_YES -eq 0 && -t 0 ]]; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tunnel-token) TUNNEL_TOKEN="${2:?--tunnel-token needs a value}"; shift 2 ;;
        --lib)          LIB_ARG="${2:?--lib needs a folder}"; shift 2 ;;
        --yes|-y)       ASSUME_YES=1; shift ;;
        -h|--help)      sed -n '2,10p' "$0"; exit 0 ;;
        *)              die "unknown option: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run with sudo:  sudo $0 $*"
command -v apt-get >/dev/null || die "this installer is for Ubuntu / Debian (apt-get not found)"

# --------------------------------------------------------------------------- packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync sqlite3 ntfs-3g curl ca-certificates gnupg git >/dev/null
apt-get install -y -qq exfatprogs >/dev/null 2>&1 || apt-get install -y -qq exfat-fuse >/dev/null 2>&1 || true
python3 -c 'import sqlite3, sys; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)"); assert sys.version_info >= (3, 9)' \
    || die "Python 3.9+ with SQLite FTS5 is required"

# --------------------------------------------------------------------------- account + settings
say "Service account '$SVC_USER'"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/astrobooks --create-home --shell /usr/sbin/nologin "$SVC_USER"
    echo "created"
else
    echo "already exists"
fi

say "Settings ($ENV_FILE)"
mkdir -p "$CONF_DIR"
if [[ ! -f $ENV_FILE ]]; then
    lib="${LIB_ARG:-/srv/astrobooks/library}"
    if interactive && [[ -z $LIB_ARG ]]; then
        echo "Where should the books be stored? The collection needs about 120 GB (more as it grows)."
        df -h --output=target,size,avail -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || true
        read -rp "Library folder [$lib]: " ans
        lib="${ans:-$lib}"
    fi
    sed "s|^ASTRO_LIB=.*|ASTRO_LIB=$lib|" "$REPO_DIR/deploy/astrobooks.env.example" > "$ENV_FILE"
    echo "written"
elif [[ -n $LIB_ARG ]]; then
    sed -i "s|^ASTRO_LIB=.*|ASTRO_LIB=$LIB_ARG|" "$ENV_FILE"
    echo "library folder changed to $LIB_ARG"
else
    echo "kept existing settings"
fi
chown root:"$SVC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
set -a; . "$ENV_FILE"; set +a
: "${ASTRO_LIB:?ASTRO_LIB missing in $ENV_FILE}" "${ASTRO_DATA:=/var/lib/astrobooks}" "${ASTRO_PORT:=8765}"

mkdir -p "$ASTRO_LIB" "$ASTRO_DATA"
chown "$SVC_USER:$SVC_USER" "$ASTRO_LIB" "$ASTRO_DATA"
chmod 750 "$ASTRO_DATA"
free_gb=$(( $(df -B1 --output=avail "$ASTRO_LIB" | tail -1) / 1024 / 1024 / 1024 ))
used_gb=$(( $(du -sb "$ASTRO_LIB" 2>/dev/null | cut -f1) / 1024 / 1024 / 1024 ))
echo "library: $ASTRO_LIB  ($used_gb GB used, $free_gb GB free)"
(( used_gb + free_gb >= 120 )) || warn "less than 120 GB available for the library - the pen drive import will not fit there"

# --------------------------------------------------------------------------- application
say "Installing the application to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude '__pycache__' "$REPO_DIR/app/" "$APP_DIR/app/"
install -m 755 "$REPO_DIR/deploy/import-usb.sh" "$APP_DIR/import-usb.sh"
install -m 755 "$REPO_DIR/deploy/astrobooks" /usr/local/bin/astrobooks
echo "$REPO_DIR" > "$APP_DIR/repo-path"
[[ -x $APP_DIR/venv/bin/python ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
echo "ok"

# --------------------------------------------------------------------------- service
say "systemd service 'astrobooks'"
install -m 644 "$REPO_DIR/deploy/astrobooks.service" /etc/systemd/system/astrobooks.service
mkdir -p /etc/systemd/system/astrobooks.service.d
printf '[Unit]\nRequiresMountsFor=%s %s\n' "$ASTRO_LIB" "$ASTRO_DATA" > /etc/systemd/system/astrobooks.service.d/paths.conf
systemctl daemon-reload
systemctl enable astrobooks >/dev/null 2>&1
systemctl restart astrobooks
for _ in $(seq 1 20); do
    curl -fsS -H "Host: localhost" "http://127.0.0.1:$ASTRO_PORT/healthz" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -fsS -H "Host: localhost" "http://127.0.0.1:$ASTRO_PORT/healthz" >/dev/null 2>&1 \
    || die "the server did not start - see:  journalctl -u astrobooks -n 50"
echo "running on 127.0.0.1:$ASTRO_PORT"

# --------------------------------------------------------------------------- Cloudflare Tunnel
say "Cloudflare Tunnel (astrobooks.munjal.tech)"
if ! command -v cloudflared >/dev/null; then
    mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
        > /etc/apt/sources.list.d/cloudflared.list
    apt-get update -qq
    apt-get install -y -qq cloudflared >/dev/null
    echo "cloudflared installed"
fi
if [[ -z $TUNNEL_TOKEN ]] && interactive && ! systemctl is-enabled cloudflared >/dev/null 2>&1; then
    echo "Paste the tunnel token from the Cloudflare dashboard (see README, step 3),"
    read -rp "or press Enter to do this later with 'sudo astrobooks tunnel TOKEN': " TUNNEL_TOKEN
fi
if [[ -n $TUNNEL_TOKEN ]]; then
    /usr/local/bin/astrobooks tunnel "$TUNNEL_TOKEN"
elif systemctl is-active cloudflared >/dev/null 2>&1; then
    echo "tunnel already connected"
else
    warn "tunnel not connected yet - run:  sudo astrobooks tunnel TOKEN"
fi

# --------------------------------------------------------------------------- first admin
say "User accounts"
admins=$(cd / && runuser -u "$SVC_USER" -- env ASTRO_DATA="$ASTRO_DATA" "$APP_DIR/venv/bin/python" "$APP_DIR/app/astro_auth.py" count-admins)
if [[ $admins -eq 0 ]]; then
    if interactive; then
        read -rp "Create the first admin account - user name: " admin_name
        [[ -n $admin_name ]] && /usr/local/bin/astrobooks user add "$admin_name" --admin
    else
        warn "no admin account yet - create one:  sudo astrobooks user add NAME --admin"
    fi
else
    echo "$admins admin account(s) present"
fi

say "Done"
cat <<EOF
  Site (after the tunnel is connected):  https://astrobooks.munjal.tech
  Next steps:
    1. Plug the pen drive into this server and run:   sudo astrobooks import-usb
    2. Add readers:                                   sudo astrobooks user add NAME
    3. Check everything:                              sudo astrobooks status
EOF
