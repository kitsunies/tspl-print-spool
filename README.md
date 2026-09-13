# tspl-print-spool

A small HTTP service that turns PDFs and images into **TSPL** for thermal label
printers, and queues them reliably.

Point it at a PDF URL, an image, or raw TSPL, and it renders, converts, and
prints via CUPS. No vendor driver, no cloud service, no proprietary SDK.

```
Caller ──→ POST /print/job ──→ Print Spool (:5200) ──→ lp -o raw ──→ CUPS ──→ Printer
             {type: "label", url: "..."}   │
                                           └── SQLite queue: queued → printing → done/failed
```

## Why this exists

Most thermal label printers speak **TSPL**, not PDF. Getting a PDF onto one
usually means either a vendor driver (Windows-only, or a black box) or
hand-rolling the conversion. This service does the conversion and gives you a
retrying queue in front of it, so a printer that's offline or out of paper does
not lose the job.

It sits on the host rather than in a container so it can reach the CUPS socket
directly.

## What it handles

- **PDF → TSPL**: rasterises at the printer's native DPI with `pdftoppm`, finds
  the content bounding box, and fits it to the label.
- **Image → TSPL**: same fit-to-label logic for PNG/JPEG input.
- **Raw TSPL passthrough**: if you already generate TSPL, send it straight through.
- **Retries**: failed jobs back off exponentially (10s, 30s, 90s) and stay in the
  DB for inspection.
- **Power-loss tolerance**: SQLite runs in TRUNCATE journal mode and stale
  journals are cleaned on boot, so an unclean shutdown does not corrupt the queue.

## Compatibility

TSPL is a de-facto standard across TSC-compatible thermal printers. Tested on a
**Munbyn B130** (203 DPI, 100x150mm); expected to work with Rollo, iDPRT,
Xprinter and similar. DPI and label size are configurable (see below).

Your printer needs a CUPS queue. Any driver that accepts raw passthrough works.

## Install

Requires Python 3.9+, CUPS, `poppler-utils`, and a configured CUPS printer queue.

```bash
git clone https://github.com/kitsunies/tspl-print-spool
cd tspl-print-spool

sudo apt-get install -y python3-pip poppler-utils cups
sudo mkdir -p /opt/print-spool /var/lib/print-spool
sudo cp spool.py requirements.txt /opt/print-spool/
cd /opt/print-spool
sudo pip3 install -r requirements.txt

sudo cp print-spool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now print-spool
```

Check it came up:

```bash
curl localhost:5200/print/health
curl localhost:5200/print/printers   # lists CUPS queues it can see
```

Set `PRINTER_NAME` in `print-spool.service` to your CUPS queue name (`lpstat -p`
lists them).

## API

### `POST /print/job`

Returns `{"job_id": "<uuid>", "status": "queued"}`.

| Type | Body | Notes |
|------|------|-------|
| `label` | `{"type": "label", "url": "https://..."}` | Downloads a PDF from a URL |
| `pdf` | `{"type": "pdf", "data": "<base64>"}` | PDF bytes inline |
| `image` | `{"type": "image", "data": "<base64>"}` | PNG/JPEG bytes inline |
| `raw` | `{"type": "raw", "data": "<base64>"}` | Pre-built TSPL bytes |

Optional: `options` (`{"width_mm": 100, "height_mm": 150}`),
`max_retries` (default 3).

### Other endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /print/status/<job_id>` | Job status: `queued`, `printing`, `done`, `failed` |
| `GET /print/queue` | Recent jobs (default 50, max 200) |
| `GET /print/printers` | CUPS printers on the host |
| `GET /print/health` | Health check + queue size |

### Examples

```bash
# Print a label from a URL
curl -X POST http://localhost:5200/print/job \
  -H "Content-Type: application/json" \
  -d '{"type": "label", "url": "https://example.com/label.pdf"}'

# Print an image
curl -X POST http://localhost:5200/print/job \
  -H "Content-Type: application/json" \
  -d '{"type": "image", "data": "'$(base64 -w0 label.png)'"}'

# Send raw TSPL
curl -X POST http://localhost:5200/print/job \
  -H "Content-Type: application/json" \
  -d '{"type": "raw", "data": "'$(base64 -w0 label.tspl)'"}'
```

## Configuration

All via environment variables (set in `print-spool.service`):

| Variable | Default | Description |
|----------|---------|-------------|
| `SPOOL_HOST` | `0.0.0.0` | Bind address |
| `SPOOL_PORT` | `5200` | Bind port |
| `SPOOL_DB_PATH` | `/var/lib/print-spool/jobs.db` | SQLite queue path |
| `PRINTER_NAME` | `THERMAL_LABEL` | CUPS queue name |
| `PRINTER_DPI` | `203` | Printer DPI |
| `LABEL_WIDTH_MM` | `100` | Label width |
| `LABEL_HEIGHT_MM` | `150` | Label height |

## ⚠️ No authentication

This service has **no auth and no TLS**. Anything that can reach the port can
print, and `label`/`pdf` types will fetch arbitrary URLs on your behalf.

Run it on a trusted network, or bind to loopback and put an authenticating
reverse proxy in front of it. Do not expose it to the internet.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -v
```

The encoder tests pin the exact TSPL byte layout, including the bit ordering and
the black/white polarity. Both are easy to get subtly wrong, and getting either
backwards produces a blank or inverted label rather than an error.

## A note on the TSPL encoder

The interesting part is `_image_to_tspl()`, which builds the printer's bitmap
command by hand: it packs pixels MSB-first into bytes and inverts them. The
inversion is not a bug — PIL mode `"1"` represents black as `0`, while TSPL
sets a bit to mean "heat this dot". So a black source pixel becomes a cleared
bit. That is the one line in this codebase most likely to be "fixed" by someone
skimming it, which is why the tests assert the exact bytes.

## License

MIT — see [LICENSE](LICENSE).
