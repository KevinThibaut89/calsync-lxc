# CalSync

One-way sync from a **work Outlook / Microsoft 365 calendar** into **Google
Calendar**, running entirely inside a small Proxmox LXC. No Mac, no browser
extension, nothing to install on your work machine — the container is the
calendar client.

It exists for the case where you **can't add apps to your Microsoft tenant**:
instead of registering an Azure app, CalSync signs in as the public *Microsoft
Office* client using the device-code flow (the same technique DavMail and
similar gateways use), so it needs no admin and no app registration.

- **Source:** Microsoft 365 calendar, read-only, via Microsoft Graph.
- **Destination:** a dedicated calendar on your own Google account (created
  automatically). Your Outlook calendar is never modified.
- **What's copied:** title, time, location and description (full details), for a
  rolling window (default 7 days back, 60 days ahead). Recurring meetings are
  expanded server-side, so no repeat-rule translation.
- **Kept in sync:** changed meetings update in place, cancelled/removed ones are
  deleted, and re-runs never create duplicates (each copy is tagged with its
  Outlook id).
- Pure Python standard library — nothing to `pip install`.

## Install (one line, community-scripts style)

Open the **Shell** of any Proxmox node (web UI: *node → Shell*) and paste:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/calsync-lxc/main/lxc/create-calsync-lxc.sh)"
```

A short wizard asks **Default** or **Advanced** settings, then builds an
unprivileged Debian 12 container and starts a one-time setup page. Add
`_ --defaults` after the closing quote to accept every default.

**Default settings:** 1 CPU, 512 MB RAM, 3 GB disk, DHCP on `vmbr0`, next free
ID. **Advanced** lets you pick the ID, resources, storage, bridge and interval.

### Finish setup in your browser

When the installer finishes it prints a URL like `https://<ct-ip>:8777/`. Open
it from any browser on the same network (your browser warns once about the
self-signed certificate — continue past it). On that page:

1. **Connect Microsoft 365** — click the button, open the Microsoft link it
   shows and enter the code. That's the whole Microsoft side.
2. **Connect Google** — paste a Google OAuth client ID + secret, then sign in.

Once both show *Connected*, the first sync runs within a couple of minutes.

### Google OAuth client (one-time, free)

You need a Google OAuth client because the container writes to your Google
account. At <https://console.cloud.google.com/>:

1. Create a project, then **enable the "Google Calendar API"**.
2. **Credentials → Create OAuth client ID → type "Web application"**.
3. Add the **Authorized redirect URI** the installer printed:
   `https://<ct-ip>:8777/oauth/google/callback`
4. **OAuth consent screen → publish to "In production".** If you leave it in
   *Testing*, Google expires the login after ~7 days and sync stops. In
   production you (the only user) will see an "unverified app" screen once and
   can click through — no Google review needed for personal use.

Copy the client ID and secret into the setup page.

## How the Microsoft sign-in works (and its limits)

CalSync authenticates as the public **Microsoft Office** client
(`d3590ed6-52b3-4102-aeff-aad2292ab01c`) with the OAuth 2.0 **device-code**
grant, requesting only `Calendars.Read offline_access`. This needs no Azure app
registration and, in most tenants, no admin consent.

Two things to know:

- **Your sign-in appears as "Microsoft Office"** in the tenant's Entra sign-in
  logs (because that's the client id used).
- **A tenant can block this** with a Conditional Access policy. If sign-in is
  refused, set a different client id in the config — the Azure CLI client
  (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) is another pre-consented option, or
  register your own public client if you're allowed to. Edit
  `/var/lib/calsync/config.toml` (`[microsoft] client_id = "…"`) and reconnect.

CalSync only ever **reads** your Outlook calendar.

## Everyday use

```bash
pct exec <ctid> -- calsync status     # what's connected, and the last run
pct exec <ctid> -- calsync sync       # run a sync now
systemctl status calsync.timer        # (inside the CT) the schedule
journalctl -u calsync.service         # (inside the CT) sync logs
```

To re-open the setup page later (e.g. to reconnect Google):

```bash
pct exec <ctid> -- systemctl start calsync-setup.service
```

### Update

Inside the container's console, type:

```bash
update
```

It downloads the latest version, restarts the timer, and **rolls back
automatically** if the new version doesn't start. From the node:
`pct exec <ctid> -- update`.

## Configuration

Non-secret settings live in `/var/lib/calsync/config.toml`:

| Key | Default | Meaning |
|---|---|---|
| `[sync] past_days` | 7 | Days of history to mirror |
| `[sync] future_days` | 60 | Days ahead to mirror |
| `[sync] interval_minutes` | 10 | How often the timer syncs |
| `[sync] title_prefix` | "" | Prefix added to copied titles, e.g. `"[Work] "` |
| `[google] calendar_name` | `Work (Outlook)` | Destination calendar |
| `[microsoft] tenant` | `organizations` | Tenant id/domain, or `organizations` |
| `[microsoft] client_id` | Office client | Override if your tenant blocks it |

Secrets (OAuth refresh tokens, the Google client secret) live separately in
`/var/lib/calsync/tokens.json`, written `0600` and never logged. Restart the
service after editing config: `systemctl restart calsync.timer`.

## Manual install (any Debian/Ubuntu box, Python 3.11+)

```bash
sudo useradd --system --home /var/lib/calsync --shell /usr/sbin/nologin calsync
sudo mkdir -p /opt/calsync /var/lib/calsync
sudo cp -r calsync systemd lxc config.example.toml /opt/calsync/
sudo cp /opt/calsync/config.example.toml /var/lib/calsync/config.toml
sudo cp /opt/calsync/systemd/* /etc/systemd/system/
sudo chown -R calsync:calsync /opt/calsync /var/lib/calsync && sudo chmod 700 /var/lib/calsync
# Connect accounts:
sudo -u calsync CALSYNC_STATE_DIR=/var/lib/calsync python3 -m calsync connect-microsoft
sudo -u calsync CALSYNC_STATE_DIR=/var/lib/calsync python3 -m calsync setup-server \
  --cert /var/lib/calsync/setup-cert.pem --key /var/lib/calsync/setup-key.pem
sudo systemctl enable --now calsync.timer
```

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The tests drive the reconciliation engine and the Graph→Google transform with
in-memory fakes (`tests/fakes.py`) — no network, no credentials.

Layout:

- `calsync/msgraph.py` — Graph client: device-code auth, token refresh, `calendarView`
- `calsync/google.py` — Google client: OAuth web flow, calendar/event CRUD
- `calsync/transform.py` — Graph event → Google event mapping + change hashing
- `calsync/sync.py` — the reconciliation pass (insert / update / delete)
- `calsync/setupserver.py` — the one-time browser setup UI
- `calsync/config.py` — config and the `0600` token store
- `calsync/http.py` — the one small HTTP+JSON helper (urllib, retries)
- `lxc/` — the Proxmox installer and in-container updater

## What this is not

- **Not two-way.** Changes you make in Google are overwritten on the next sync.
- **Not real-time.** It polls on the configured interval (default 10 min).
- **Not a way around company policy.** Copying work calendar data to a personal
  account may break your employer's data-handling rules — that's your call to
  make. CalSync reads only your own mailbox with your own sign-in.
