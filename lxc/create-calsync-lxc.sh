#!/usr/bin/env bash
#
# CalSync — Proxmox VE LXC installer (community-scripts style).
#
# One-liner, from any Proxmox node's Shell:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/calsync-lxc/main/lxc/create-calsync-lxc.sh)"
#
# It builds an unprivileged Debian 12 container, installs CalSync, and starts a
# one-time setup web UI so you can connect Microsoft 365 and Google from your
# browser. After that a systemd timer syncs every few minutes.
#
# Run from a checkout instead to install this exact copy:
#   bash lxc/create-calsync-lxc.sh
#
# Env overrides: CALSYNC_REPO=owner/name  CALSYNC_REF=main|vX.Y.Z
set -euo pipefail

CALSYNC_REPO="${CALSYNC_REPO:-KevinThibaut89/calsync-lxc}"
CALSYNC_REF="${CALSYNC_REF:-main}"
SETUP_PORT=8777

msg()  { echo -e "  \033[1;32m✔\033[0m $*"; }
info() { echo -e "  \033[1;34m•\033[0m $*"; }
warn() { echo -e "  \033[1;33m!\033[0m $*" >&2; }
die()  { echo -e "  \033[1;31m✘\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this on a Proxmox node as root."
command -v pct >/dev/null || die "This must run on a Proxmox VE node (pct not found)."

# ---------------------------------------------------------------------------
# Locate the app source: a local checkout if present, else download a tarball.
# ---------------------------------------------------------------------------
SRC_DIR=""
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/null}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/../calsync/__main__.py" ]; then
  SRC_DIR="$(cd "$SELF_DIR/.." && pwd)"
  info "Installing from checkout: $SRC_DIR"
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

if [ -z "$SRC_DIR" ]; then
  info "Downloading CalSync ($CALSYNC_REPO@$CALSYNC_REF)…"
  curl -fsSL "https://codeload.github.com/$CALSYNC_REPO/tar.gz/refs/heads/$CALSYNC_REF" \
       -o "$WORK/src.tgz" \
    || curl -fsSL "https://codeload.github.com/$CALSYNC_REPO/tar.gz/refs/tags/$CALSYNC_REF" \
       -o "$WORK/src.tgz" \
    || die "Could not download from GitHub. Check CALSYNC_REPO/CALSYNC_REF."
  tar -xzf "$WORK/src.tgz" -C "$WORK"
  SRC_DIR="$(find "$WORK" -maxdepth 1 -type d -name 'calsync-*' | head -1)"
  [ -n "$SRC_DIR" ] || die "Unexpected archive layout."
fi

# ---------------------------------------------------------------------------
# Handle --update <ctid>: push this source into an existing container.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--update" ]; then
  CTID="${2:?usage: --update <ctid>}"
  info "Updating CalSync in CT $CTID…"
  tar -C "$SRC_DIR" -czf "$WORK/app.tgz" calsync systemd lxc config.example.toml
  pct push "$CTID" "$WORK/app.tgz" /tmp/app.tgz
  pct exec "$CTID" -- bash -c '
    set -e
    tar -C /opt/calsync -xzf /tmp/app.tgz && rm -f /tmp/app.tgz
    systemctl restart calsync.timer
  '
  msg "CT $CTID updated."
  exit 0
fi

# ---------------------------------------------------------------------------
# Wizard.
# ---------------------------------------------------------------------------
DEFAULTS=0
[ "${1:-}" = "--defaults" ] && DEFAULTS=1

CTID="$(pvesh get /cluster/nextid)"
HOSTNAME="calsync"
CORES=1
RAM=512
DISK=3
BRIDGE="vmbr0"
STORAGE=""
INTERVAL=10

pick_storage() {
  # First storage that supports container rootdir.
  pvesm status -content rootdir 2>/dev/null | awk 'NR==2{print $1}'
}
STORAGE="$(pick_storage || true)"
[ -n "$STORAGE" ] || STORAGE="local-lvm"

if [ "$DEFAULTS" -eq 0 ]; then
  echo
  echo "  CalSync container setup — press Enter to accept the [default]."
  read -rp "  Container ID [$CTID]: " x; CTID="${x:-$CTID}"
  read -rp "  Hostname [$HOSTNAME]: " x; HOSTNAME="${x:-$HOSTNAME}"
  read -rp "  CPU cores [$CORES]: " x; CORES="${x:-$CORES}"
  read -rp "  RAM MB [$RAM]: " x; RAM="${x:-$RAM}"
  read -rp "  Disk GB [$DISK]: " x; DISK="${x:-$DISK}"
  read -rp "  Storage [$STORAGE]: " x; STORAGE="${x:-$STORAGE}"
  read -rp "  Bridge [$BRIDGE]: " x; BRIDGE="${x:-$BRIDGE}"
  read -rp "  Sync interval minutes [$INTERVAL]: " x; INTERVAL="${x:-$INTERVAL}"
fi

# ---------------------------------------------------------------------------
# Template.
# ---------------------------------------------------------------------------
TMPL_STORE="local"
TEMPLATE="$(pveam list "$TMPL_STORE" 2>/dev/null | awk '/debian-12-standard/{print $1}' | sort | tail -1 || true)"
if [ -z "$TEMPLATE" ]; then
  info "Downloading Debian 12 template…"
  pveam update >/dev/null 2>&1 || true
  NAME="$(pveam available -section system | awk '/debian-12-standard/{print $2}' | sort | tail -1)"
  [ -n "$NAME" ] || die "No debian-12-standard template available."
  pveam download "$TMPL_STORE" "$NAME" >/dev/null
  TEMPLATE="$TMPL_STORE:vztmpl/$NAME"
fi

# ---------------------------------------------------------------------------
# Create + start.
# ---------------------------------------------------------------------------
info "Creating CT $CTID ($HOSTNAME) on $STORAGE…"
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --unprivileged 1 \
  --cores "$CORES" --memory "$RAM" --swap "$RAM" \
  --rootfs "$STORAGE:$DISK" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --features nesting=1 \
  --onboot 1 >/dev/null

pct start "$CTID" >/dev/null
info "Waiting for the container network…"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
  sleep 2
done

# ---------------------------------------------------------------------------
# Install packages + app.
# ---------------------------------------------------------------------------
info "Installing packages…"
pct exec "$CTID" -- bash -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 ca-certificates openssl >/dev/null
'

info "Copying CalSync into the container…"
tar -C "$SRC_DIR" -czf "$WORK/app.tgz" calsync systemd lxc config.example.toml
pct push "$CTID" "$WORK/app.tgz" /tmp/app.tgz

pct exec "$CTID" -- bash -c "
  set -e
  id calsync >/dev/null 2>&1 || useradd --system --home /var/lib/calsync --shell /usr/sbin/nologin calsync
  mkdir -p /opt/calsync /var/lib/calsync
  tar -C /opt/calsync -xzf /tmp/app.tgz && rm -f /tmp/app.tgz

  # Config (non-secret), with the chosen interval.
  if [ ! -f /var/lib/calsync/config.toml ]; then
    sed 's/^interval_minutes = .*/interval_minutes = $INTERVAL/' \
      /opt/calsync/config.example.toml > /var/lib/calsync/config.toml
  fi

  # Self-signed cert for the setup UI (browser will warn once; that is fine).
  if [ ! -f /var/lib/calsync/setup-cert.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout /var/lib/calsync/setup-key.pem \
      -out /var/lib/calsync/setup-cert.pem \
      -subj '/CN=calsync' >/dev/null 2>&1
  fi

  chown -R calsync:calsync /opt/calsync /var/lib/calsync
  chmod 700 /var/lib/calsync

  # systemd units.
  cp /opt/calsync/systemd/calsync.service /etc/systemd/system/
  cp /opt/calsync/systemd/calsync.timer /etc/systemd/system/
  cp /opt/calsync/systemd/calsync-setup.service /etc/systemd/system/
  sed -i 's/^OnUnitActiveSec=.*/OnUnitActiveSec=${INTERVAL}min/' /etc/systemd/system/calsync.timer

  # 'calsync' command on PATH (no pip install; just a wrapper).
  cat > /usr/local/bin/calsync <<'WRAP'
#!/usr/bin/env bash
cd /opt/calsync && exec python3 -m calsync \"\$@\"
WRAP
  chmod 0755 /usr/local/bin/calsync

  # 'update' convenience command inside the container.
  install -m 0755 /opt/calsync/lxc/calsync-update /usr/local/bin/update 2>/dev/null || true

  systemctl daemon-reload
  systemctl enable --now calsync.timer >/dev/null 2>&1
  systemctl enable --now calsync-setup.service >/dev/null 2>&1
"

# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------
IP=""
for _ in $(seq 1 15); do
  IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$IP" ] && break
  sleep 1
done

# Google rejects raw IPs as OAuth redirect URIs (it requires a real public
# TLD), so we address the container through a wildcard-DNS hostname that maps
# straight back to its IP. nip.io needs no setup and resolves <ip>.nip.io -> <ip>.
HOSTADDR="${IP:-<container-ip>}"
NIPHOST="${IP:+${IP}.nip.io}"
NIPHOST="${NIPHOST:-<container-ip>.nip.io}"

echo
msg "CalSync is installed in CT $CTID."
echo
echo "  Finish setup in your browser (same network as Proxmox). Use the"
echo "  nip.io address below — Google will not accept a bare IP later:"
echo
echo -e "     \033[1mhttps://$NIPHOST:$SETUP_PORT/\033[0m"
echo
echo "  (Your browser will warn about the self-signed certificate — continue past it.)"
echo
echo "  On that page you will:"
echo "    1. Connect Microsoft 365 — sign in with the code it shows."
echo "    2. Connect Google — paste a Google OAuth client ID + secret."
echo
echo "  Google client, once (free): https://console.cloud.google.com/"
echo "    • New project → enable 'Google Calendar API'."
echo "    • Credentials → Create OAuth client ID → type 'Web application'."
echo "    • Leave 'Authorised JavaScript origins' empty; add this redirect URI:"
echo -e "        \033[1mhttps://$NIPHOST:$SETUP_PORT/oauth/google/callback\033[0m"
echo "    • OAuth consent screen → publish to 'In production'"
echo "      (otherwise Google expires the login after ~7 days)."
echo
if [ -n "$IP" ] && ! getent hosts "$NIPHOST" >/dev/null 2>&1; then
  warn "Note: $NIPHOST did not resolve from this node. If your browser can't"
  warn "reach it either (some routers block private IPs in public DNS), try"
  warn "$IP.sslip.io instead, or use an SSH tunnel to localhost:$SETUP_PORT."
fi
echo "  After both are connected the first sync runs within a couple of minutes."
echo "  Check it with:  pct exec $CTID -- calsync status"
