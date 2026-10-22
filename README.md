# Astro Library — home-server edition

The server version of the **Astro Library** (search, read and download the astrology book collection) for an
Ubuntu / Debian home server, published on the internet at **https://astrobooks.munjal.tech** through a
Cloudflare Tunnel.

It is built from the Windows Astro Library (`astro_library.py`, `astro_downloader.py`, `astro_import.py` are the
same code) with a new web server, `app/astro_server.py`:

| Windows edition (pen drive) | This server edition |
|---|---|
| runs on `127.0.0.1`, only on that PC | runs on the home server, reachable from anywhere via `astrobooks.munjal.tech` |
| no sign-in | **user accounts** – everyone signs in; *readers* and *admins* |
| "Open" / "Show in folder" | **Read** in the browser (fast page-by-page loading for big PDFs) and **Download** |
| copy shortlist to a folder | **download your shortlist as one ZIP** – each user has their own shortlist |
| Downloads / Tools tabs | same, **admins only** (pCloud sync, build catalogue, export, user management) |
| — | **`astrobooks import-usb`**: copies the library from the pen drive to the server's disk |

```
 phone / laptop ──HTTPS──▶ Cloudflare ──tunnel──▶ cloudflared ──▶ astro_server.py (127.0.0.1:8765) ──▶ /srv/astrobooks/library
                                                        (home server, no open router ports)
```

---

## What you need

* A home server running **Ubuntu 22.04+ or Debian 11+** (Raspberry Pi OS / Proxmox containers work too) with
  **at least 120 GB free** for the books (the pen drive currently holds ~102 GB).
* The **`munjal.tech` domain on Cloudflare** (free plan is fine). If its DNS is somewhere else, add the domain
  in the Cloudflare dashboard and change the nameservers at your registrar to the two Cloudflare gives you.
* The pen drive with `AstroBooksCollection` (it is NTFS; the server reads it read-only, nothing on it is changed).

No router port-forwarding and no public IP are needed — the tunnel works behind Jio / Airtel CGNAT.

---

## 1. Install

On the server:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/yogeshmunjal1969-netizen/Astro-Library-VPS.git
cd Astro-Library-VPS
sudo ./deploy/install.sh
```

The installer asks where to keep the books (default `/srv/astrobooks/library`; give a path on a big data disk
if your system disk is small, e.g. `/mnt/data/astrobooks`), sets everything up, and asks you for the
**first admin user name and password**. It can be run again at any time; settings, accounts and books are kept.

It installs:

| What | Where |
|---|---|
| application + Python virtual env | `/opt/astrobooks` |
| settings | `/etc/astrobooks/astrobooks.env` |
| books + catalogue | `ASTRO_LIB` (default `/srv/astrobooks/library`) |
| user accounts | `/var/lib/astrobooks/users.db` |
| web server service | `astrobooks.service` (starts at boot, runs as user `astrobooks`) |
| tunnel | `cloudflared` (Cloudflare's official package) |
| admin command | `/usr/local/bin/astrobooks` |

## 2. Copy the books from the pen drive

1. On Windows, close Astro Library and **Eject** the pen drive (so NTFS is left clean).
2. Plug it into the server and run (inside `tmux` if you are on SSH — the copy takes 1–2 hours on USB 3):

```bash
tmux new -s import          # optional, keeps running if SSH disconnects; re-attach with: tmux attach -t import
sudo astrobooks import-usb
```

What it does:

1. finds the USB drive (asks if there is more than one; or name it: `sudo astrobooks import-usb /dev/sdb1`),
   mounts it **read-only**, and finds `AstroBooksCollection` on it;
2. checks there is enough space, then copies all books with `rsync` — **interrupted? just run it again**, it
   resumes and skips what is already copied (the `_Browse` hard-link folders are not copied);
3. stops the web server for a moment, installs the pen drive's catalogue (`_library/library.db`, merging its
   `-wal` journal so no recent changes are lost), keeps the previous server database as
   `library.db.before-import-<date>` and carries its users' shortlists over;
4. marks every book as on the server / missing according to what was really copied, restarts the web server
   and unmounts the pen drive.

`sudo astrobooks import-usb --dry-run` shows what would be copied without copying anything;
`--no-db` copies new books but keeps the server's catalogue (run `sudo astrobooks build` afterwards).

After the first import the **server becomes the main copy**: new books can be fetched straight from pCloud
on the server (*pCloud sync* tab, or `sudo astrobooks sync`). Re-importing from the pen drive later is fine too.

## 3. Connect astrobooks.munjal.tech (Cloudflare Tunnel)

1. Open the [Cloudflare dashboard](https://dash.cloudflare.com) → **Zero Trust** → **Networks** → **Tunnels** →
   **Create a tunnel** → type **Cloudflared** → name it `astrobooks` → **Save**.
2. On the "Install and run connector" page, choose **Debian**, and copy the long token at the end of the
   command shown (the part after `service install`, starting `eyJ…`). On the server:
   ```bash
   sudo astrobooks tunnel eyJhIjoi...
   ```
3. Back in the dashboard → **Next** → **Public hostname**:
   * Subdomain: `astrobooks`  Domain: `munjal.tech`
   * Service type: `HTTP`   URL: `localhost:8765`
   * **Save tunnel**.

Open **https://astrobooks.munjal.tech** and sign in. Cloudflare provides the HTTPS certificate automatically.

*Optional extra lock:* in Zero Trust → **Access** → **Applications** you can add `astrobooks.munjal.tech` and
allow only listed e-mail addresses (one-time code by e-mail) before the site's own sign-in page appears.

## 4. Add people

```bash
sudo astrobooks user add priya            # reader: search, read, download, own shortlist
sudo astrobooks user add yogesh --admin   # admin: also pCloud sync, Tools, user management
sudo astrobooks user list
```

Admins can also add users, reset passwords and change roles on the **Tools** tab. Everyone can change their own
password with the **Password** link at the top right.

---

## Everyday commands

| Command | |
|---|---|
| `sudo astrobooks status` | service, tunnel, disk space, catalogue and users |
| `sudo astrobooks logs` | live server log (sign-in failures appear here) |
| `sudo astrobooks restart` | restart the web server (after editing `/etc/astrobooks/astrobooks.env`) |
| `sudo astrobooks import-usb` | copy from the pen drive (again) |
| `sudo astrobooks build` | rebuild the catalogue after adding books |
| `sudo astrobooks sync` | fetch new / missing books from pCloud, then rebuild |
| `sudo astrobooks check` | database check + mark books present / missing from the disk |
| `sudo astrobooks user …` | `list`, `add NAME [--admin]`, `passwd NAME`, `admin NAME on/off`, `delete NAME` |
| `sudo astrobooks tunnel TOKEN` | (re)connect the Cloudflare Tunnel |
| `sudo astrobooks update` | pull the newest version of this repository and reinstall |

## Backups

Everything that cannot be re-downloaded is small:

* `/var/lib/astrobooks/users.db` – accounts
* `$ASTRO_LIB/_library/` – catalogue, shortlists, download progress
* `$ASTRO_LIB/My Books (…)` – your own books that are not on pCloud

The pen drive itself remains a full offline backup of the collection and still works with the Windows Astro Library.

## Security notes

* The web server listens on `127.0.0.1` only; the internet reaches it solely through the outbound Cloudflare
  Tunnel — no ports are opened on the router.
* Every page and book requires sign-in. Passwords are stored as PBKDF2-SHA256 hashes; sign-in cookies are
  `HttpOnly`, `SameSite`, `Secure` over HTTPS and last 30 days. Repeated wrong passwords are blocked for 15
  minutes (per IP address and per user name).
* All actions are protected against cross-site requests; HTML/Word/other non-PDF files are always sent as
  downloads, never displayed inside the site.
* Only admins can start downloads, run tools or manage users.
* The service runs as the unprivileged `astrobooks` user with systemd hardening.

## Using it on the home network without the internet

Set `ASTRO_BIND=0.0.0.0` and add the server's LAN address to `ASTRO_ALLOWED_HOSTS` in
`/etc/astrobooks/astrobooks.env`, then `sudo astrobooks restart` and open `http://<server-ip>:8765`.

## Repository layout

```
app/astro_server.py      web server + page (sign-in, search, read/download, shortlist ZIP, admin tabs)
app/astro_auth.py        user accounts and sessions (also a CLI: astrobooks user …)
app/astro_maint.py       post-import database merge, disk reconcile, integrity check
app/astro_library.py     catalogue builder / search (shared with the Windows edition)
app/astro_downloader.py  pCloud downloader (shared with the Windows edition)
app/astro_import.py      import a folder of existing books (shared with the Windows edition)
deploy/install.sh        installer / updater
deploy/import-usb.sh     pen drive → server copy
deploy/astrobooks        admin command
deploy/astrobooks.service, deploy/astrobooks.env.example
```
