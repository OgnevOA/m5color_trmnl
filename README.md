# m5color-trmnl

A TRMNL-like color e-ink display system for a 400x600 portrait color e-paper
panel (Spectra 6 style palette). It consists of:

- A **low-power reference client** (`client.py`) that wakes, reports status,
  fetches a pre-rendered image, displays it, and goes back to deep sleep.
- A **FastAPI backend** that owns all state, scheduling, queueing and
  server-side image rendering.
- A **Telegram bot** for controlling the system and pushing content.

The backend, the background pre-render worker, and the Telegram bot all run in
**one combined asyncio process** (so they share a single SQLite connection and
service layer with no multi-process write contention).

That process can drive **multiple devices at once** -- e.g. an M5 Paper Color
and a Seeed reTerminal E1004 -- each as a fully independent stack with its own
Telegram bot, SQLite database, render queue, settings and data directory.
Requests are routed to the right stack by the `{device_id}` in the URL, and the
only shared resources are the HTTP port and the (locked) headless Chromium
renderer. See [Multiple devices](#multiple-devices).

## Architecture

```
Telegram user sends command / content
  -> bot validates the user
  -> backend updates mode / settings / queue
  -> background worker pre-renders a 400x600 image (Chromium + Pillow)
  -> image stored in /data/rendered
  -> device wakes
  -> device POSTs status + battery
  -> backend returns an action + next_wake_seconds
  -> device downloads the image if needed
  -> device draws the image
  -> device sets its wake timer
  -> device enters deep sleep
```

Key principles:

- **The backend is the scheduling authority.** The device just obeys
  `next_wake_seconds`.
- **The device stays dumb and power-efficient.** It does no scheduling logic
  beyond simple offline fallbacks.
- **The headless browser never runs during a device request.** All rendering
  is done ahead of time by the background worker.
- **The server sends smooth RGB; the panel does the color conversion.** Images
  are rendered/sent as full-color PNGs and the device maps them to its Spectra-6
  palette once (dithering photos, nearest-color for flat text/cards), avoiding
  the muddy double-dithering of quantizing on both sides.
- **Night mode is handled server-side** (default 23:00-06:30 Asia/Jerusalem):
  the device is told to sleep through the night.
- **Images are pre-rendered and cached** on the `/data` volume.

### Component layout

```
app/
  config.py            Settings loaded from environment variables
  db.py                SQLite schema + async access (aiosqlite)
  models.py            Pydantic API contract + internal models
  auth.py              Device token + Telegram user validation
  scheduler.py         next_wake_seconds + night-mode logic
  queue_service.py     Queue + rendered-image bookkeeping
  services.py          Shared service layer (API + bot + worker)
  render/
    image_ops.py       Fit to 400x600 + smooth RGB output (device dithers)
    browser.py         Playwright/Chromium HTML -> PNG
    templates.py       Jinja2 HTML rendering
    templates/         HTML/CSS templates
    worker.py          Background pre-render worker
  modes/               Content modes (plain_text, image, xkcd, quotes, ...)
  api/routes.py        Device + health HTTP endpoints
  telegram/handlers.py aiogram bot commands and content input
  runtime.py           Wires API + per-device worker/bot stacks into one process
server.py              Combined process entrypoint
bot.py                 Standalone bot entrypoint (shares app/ services)
client.py              Mock/reference device client
```

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium               # one-time browser download

cp .env.example .env                                # then edit values
# For local testing you can point storage at a local folder:
#   DATA_DIR=./data DATABASE_PATH=./data/trmnl.db RENDERED_IMAGES_DIR=./data/rendered

python server.py
```

The API is then available at `http://localhost:8000` (`/health` to check).

Run the mock client against it:

```bash
export BACKEND_URL=http://localhost:8000
export DEVICE_ID=m5paper-color-01
export DEVICE_TOKEN=please-change-this-device-token   # must match .env

python client.py                       # one wake cycle (timer)
python client.py --wake-reason button  # simulate a button press
python client.py --loop                # repeat; press Enter to wake
```

The mock display image is written to `mock_display/current.png`.

## Docker deployment (TrueNAS Scale)

The published image already contains Chromium and all browser dependencies
(it is based on the official Playwright Python image).

1. Copy `.env.example` to `.env` and fill in `DEVICE_TOKEN`,
   `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ALLOWED_USER_IDS`. Set
   `PUBLIC_BASE_URL` to the host address including the published port
   (e.g. `http://<host-ip>:17555`).
2. The image is public, so no registry login is needed:
   `ghcr.io/ognevoa/m5color-trmnl:latest`.
3. Deploy:

```bash
docker compose pull
docker compose up -d
```

The app listens on port `8000` inside the container and is published on host
port **17555** (see `docker-compose.yml`). Reach the API at
`http://<host-ip>:17555/health`.

Persistent data (SQLite DB, rendered images, uploads) is stored in `./data`
mounted at `/data` in the container. On TrueNAS Scale, point this at a
dataset path.

To build locally instead of pulling, comment out `image:` and uncomment
`build: .` in `docker-compose.yml`.

## CI/CD (GitHub Actions)

`.github/workflows/ci.yml` runs on pushes, tags (`v*`), and pull requests:

- **lint** - byte-compiles all Python sources as a fast sanity check.
- **build-and-push** - builds the Docker image (with Playwright/Chromium) and,
  on `main` / tags, pushes it to **GitHub Container Registry**:
  - `ghcr.io/<owner>/m5color-trmnl:latest`
  - `ghcr.io/<owner>/m5color-trmnl:<git-sha>` and `:<tag>` for version tags

Authentication uses the built-in `GITHUB_TOKEN` (no extra secrets needed).
Pull requests build the image but do not push.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `APP_ENV` | `production` | Environment label. |
| `DATA_DIR` | `/data` | Base data directory. |
| `DATABASE_PATH` | `/data/trmnl.db` | SQLite database file. |
| `RENDERED_IMAGES_DIR` | `/data/rendered` | Rendered PNG storage. |
| `DEVICE_ID` | `m5paper-color-01` | Device identifier. |
| `DEVICE_TOKEN` | `change-me-...` | Shared bearer token for the device. |
| `DEVICE_TYPE` | `m5` | Panel family: `m5` (400x600 PNG) or `e1004` (1200x1600 packed frame). |
| `DEVICE_<n>_<FIELD>` | _(unset)_ | Optional per-device override for multi-device mode; one stack per index `n` (see [Multiple devices](#multiple-devices)). |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of the backend. |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Bot token; bot disabled if empty. |
| `TELEGRAM_ALLOWED_USER_IDS` | _(empty)_ | Allowed user IDs (comma/space separated). |
| `DEFAULT_INTERVAL_MINUTES` | `60` | Default polling interval. |
| `TIMEZONE` | `Asia/Jerusalem` | Scheduling timezone. |
| `NIGHT_MODE_START` | `23:00` | Night mode start. |
| `NIGHT_MODE_END` | `06:30` | Night mode end. |
| `HOST` | `0.0.0.0` | Bind host. |
| `PORT` | `8000` | Bind port. |

## Multiple devices

By default the process runs one device from the `DEVICE_*` / `TELEGRAM_*` env
vars. To drive several devices at once (e.g. an M5 Paper Color and a reTerminal
E1004), define `DEVICE_<n>_<FIELD>` vars in the same `.env` -- one index `n` per
device, where `<FIELD>` is any setting name:

```dotenv
DEVICE_1_DEVICE_ID=m5paper-color-01
DEVICE_1_DEVICE_TYPE=m5
DEVICE_1_DEVICE_TOKEN=...
DEVICE_1_TELEGRAM_BOT_TOKEN=111:AAA
DEVICE_1_TELEGRAM_ALLOWED_USER_IDS=12345
DEVICE_1_DATA_DIR=/data/m5

DEVICE_2_DEVICE_ID=reterminal-e1004-01
DEVICE_2_DEVICE_TYPE=e1004
DEVICE_2_DEVICE_TOKEN=...
DEVICE_2_TELEGRAM_BOT_TOKEN=222:BBB
DEVICE_2_TELEGRAM_ALLOWED_USER_IDS=12345
DEVICE_2_DATA_DIR=/data/e1004
```

Each index becomes a fully independent stack (own bot, DB, queue, settings, data
dir); global config (timezone, mode API keys, Home Assistant, `PUBLIC_BASE_URL`,
...) is shared from `.env`. `DEVICE_<n>_DEVICE_ID` is required per device;
missing `DEVICE_<n>_DATABASE_PATH` / `DEVICE_<n>_RENDERED_IMAGES_DIR` default to
`<data_dir>/trmnl.db` and `<data_dir>/rendered`. Both device firmwares point at
the same `host:17555`, each with its own `DEVICE_ID` + token. When **any**
`DEVICE_<n>_*` var is present the single `DEVICE_*` block is ignored; with none,
single-device behaviour is unchanged.

## API summary

All device endpoints require `Authorization: Bearer <DEVICE_TOKEN>` (validated
against that device's stack).

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/device/{device_id}/status` | Report status/battery; returns an action + `next_wake_seconds`. |
| `GET` | `/api/device/{device_id}/image/{image_id}` | Download the rendered frame: a 400x600 PNG (M5) or a packed 1200x1600 `.bin` (E1004). |
| `GET` | `/stats` | LAN telemetry view; add `?device=<id>` to pick a device (defaults to the first). |
| `GET` | `/health` | Liveness check (`{"status":"ok"}`). |

### Status response actions

- `draw` - download `image_url` (E1004 prefers `frame_url`), display it, then sleep.
- `sleep` - do not change the display; set the wake timer and sleep.
- `noop` - no new content; keep the current display and sleep.
- `blank` - display a blank frame, then sleep.

## Telegram bot commands

| Command | Description |
| --- | --- |
| `/start` | Help + current status. |
| `/status` | Mode, last update, wake reason, last image, battery, interval, night mode, overlay, queue size. |
| `/interval N` | Set polling interval to N minutes. |
| `/mode NAME` | Set the active mode. |
| `/queue` | Show queue status. |
| `/clear` | Clear pending queue entries. |
| `/next` | Skip to / generate the next item for the active mode. |
| `/night on\|off\|status` | Control night mode (window stays 23:00-06:30). |
| `/overlay on\|off\|status` | Toggle the info overlay on artwork/photo frames (default off). |
| `/help` | List commands. |

Sending a plain text message displays that text. Sending a photo displays the
image (cropped/resized to 400x600).

Built-in modes: `plain_text`, `image`, `random_friends_quote`, `random_xkcd`,
and the artist modes `van_gogh`, `monet`, `caravaggio`, `klimt` (each a random,
portrait-first public-domain painting by that artist via Wikidata/Commons)
(plus a placeholder for unknown modes). The mode interface in
`app/modes/base.py` makes adding new modes (weather, calendar, now_playing,
...) straightforward.

### Artwork info overlay

Toggle a content-aware info overlay on image/photo frames with `/overlay on`
(off by default, per device). When on, the pre-render worker draws the artwork
full-bleed and lays a compact band across the bottom quarter of the display: a
mini month calendar (today highlighted) on one side, and the date, an artwork
caption (title / artist / year, for the artist modes) and current weather on
the other. Each block samples the luminance of the artwork behind it and flips
between light and dark text so it stays legible over any picture. Weather reuses
the OpenWeather config (shown only when `OPENWEATHER_API_KEY` is set). Since
e-ink only repaints on wake, the date/weather reflect the last refresh, not live
time. Text cards (quotes, QR, weather, plain text) are unaffected.
