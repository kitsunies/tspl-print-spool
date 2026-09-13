#!/usr/bin/env python3
"""
TSPL Print Spool — a lightweight HTTP service that sits between callers and CUPS.

Converts PDFs / images to TSPL (TSC Printer Language) for thermal label printers.
Provides a SQLite-backed job queue with retry.
Runs on the host (closer to CUPS), not in Docker.

TSPL is understood by most TSC-compatible and many generic thermal label
printers (Munbyn, Rollo, iDPRT, Xprinter, ...). Raw TSPL is sent to CUPS with
`lp -o raw`, so no vendor driver is required.

Endpoints:
  POST /print/job          Submit a print job (label, image, raw, pdf)
  GET  /print/status/<id>  Check job status
  GET  /print/queue        List recent jobs
  GET  /print/printers     List CUPS printers
"""

import os
import sys
import io
import json
import uuid
import base64
import logging
import sqlite3
import subprocess
import tempfile
import threading
import time
import glob
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from PIL import Image
from flask import Flask, request, jsonify

# ── Config ──────────────────────────────────────────────────────────────────────

PRINTER_NAME = os.environ.get("PRINTER_NAME", "THERMAL_LABEL")
PRINTER_DPI = int(os.environ.get("PRINTER_DPI", "203"))
LABEL_WIDTH_MM = int(os.environ.get("LABEL_WIDTH_MM", "100"))
LABEL_HEIGHT_MM = int(os.environ.get("LABEL_HEIGHT_MM", "150"))
DB_PATH = os.environ.get("SPOOL_DB_PATH", "/var/lib/print-spool/jobs.db")
HOST = os.environ.get("SPOOL_HOST", "0.0.0.0")
PORT = int(os.environ.get("SPOOL_PORT", "5200"))
MAX_RETRIES = 3
POLL_INTERVAL = 1  # seconds between worker queue polls

_CUPS_ENV = {**os.environ}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] print-spool: %(message)s",
)
logger = logging.getLogger("print-spool")

app = Flask(__name__)

# ── Database ─────────────────────────────────────────────────────────────────────


def _recover_db():
    """Clean up stale WAL/SHM files from unclean shutdowns (power cuts)."""
    for suffix in ("-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            try:
                os.unlink(path)
                logger.warning("Cleaned stale journal file: %s", path)
            except OSError:
                pass


def get_db() -> sqlite3.Connection:
    """Get a thread-safe SQLite connection."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # TRUNCATE instead of WAL: no separate WAL files to corrupt on power loss
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    _recover_db()
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id      TEXT PRIMARY KEY,
            type        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'queued',
            url         TEXT,
            data        BLOB,
            options     TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            error       TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            scheduled_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_scheduled ON jobs(scheduled_at)
    """)
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── TSPL Conversion (ported from printer.py) ─────────────────────────────────────


def _pdf_to_image(pdf_path: str) -> Image.Image:
    """Render PDF to a 1-bit monochrome PIL image at the printer's native DPI."""
    prefix = pdf_path.replace(".pdf", "")
    subprocess.run(
        ["pdftoppm", "-r", str(PRINTER_DPI), "-png", "-singlefile", pdf_path, prefix],
        check=True, capture_output=True, timeout=30,
    )
    candidates = glob.glob(f"{prefix}*.png")
    if not candidates:
        raise FileNotFoundError(f"pdftoppm produced no PNG from {pdf_path}")
    png_path = candidates[0]
    img = Image.open(png_path).convert("L")
    os.unlink(png_path)

    label_w = round(LABEL_WIDTH_MM * PRINTER_DPI / 25.4)
    label_h = round(LABEL_HEIGHT_MM * PRINTER_DPI / 25.4)

    # Find content bounding box — detect the actual label area within any envelope
    dark = img.point(lambda p: 255 if p < 240 else 0)
    bbox = dark.getbbox()

    if bbox:
        left, top, right, bottom = bbox
        content = img.crop(bbox)
        content.thumbnail((label_w, label_h), Image.LANCZOS)
        canvas = Image.new("L", (label_w, label_h), 255)
        canvas.paste(content, (0, 0))
        img = canvas
    else:
        img = img.resize((label_w, label_h), Image.LANCZOS)

    img = img.convert("1")
    logger.info("Rendered PDF -> %dx%d mono image", img.width, img.height)
    return img


def _image_to_tspl(img: Image.Image) -> bytes:
    """Build a raw TSPL command stream from a 1-bit PIL image.

    PIL mode "1": 0=black; TSPL BITMAP mode 1: bit=1 means print dot.
    """
    w, h = img.size
    width_mm = round(w * 25.4 / PRINTER_DPI)
    height_mm = round(h * 25.4 / PRINTER_DPI)
    width_bytes = (w + 7) // 8

    header = (
        f"SIZE {width_mm} mm, {height_mm} mm\r\n"
        f"GAP 3 mm, 0 mm\r\n"
        f"DIRECTION 0\r\n"
        f"DENSITY 8\r\n"
        f"CLS\r\n"
        f"BITMAP 0,0,{width_bytes},{h},1,"
    ).encode("ascii")

    pixels = img.load()
    data = bytearray()
    for y in range(h):
        for x_byte in range(width_bytes):
            byte_val = 0
            for bit in range(8):
                x = x_byte * 8 + bit
                if x < w and pixels[x, y] == 0:
                    byte_val |= 0x80 >> bit
            data.append(byte_val ^ 0xFF)

    return header + bytes(data) + b"\r\nPRINT 1\r\n"


def download_pdf(url: str) -> str:
    """Download a PDF URL to a temp file and return the path."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content = resp.content
    if not content.startswith(b"%PDF"):
        logger.warning("Downloaded content does not look like a PDF (first bytes: %s)", content[:20])
    logger.info("Downloaded label: %d bytes", len(content))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(content)
    tmp.close()
    return tmp.name


# ── CUPS send ────────────────────────────────────────────────────────────────────


def _send_to_printer(tspl_data: bytes) -> dict:
    """Send TSPL command stream to the printer via CUPS lp command."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(tspl_data)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["lp", "-d", PRINTER_NAME, "-o", "raw", tmp_path],
            capture_output=True, text=True, timeout=30, env=_CUPS_ENV,
        )
        if result.returncode != 0:
            logger.error("lp error: %s", result.stderr)
            return {"success": False, "error": result.stderr.strip()}
        job_id = result.stdout.strip()
        logger.info("Print job submitted: %s", job_id)
        return {"success": True, "lp_job_id": job_id}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Job processing ───────────────────────────────────────────────────────────────


def _backoff_delay(retry_count: int) -> int:
    """Exponential backoff: 10s, 30s, 90s."""
    return 10 * (3 ** retry_count)


def process_job(job: dict) -> str:
    """
    Process a single print job. Returns the new status ('done' or raises for retry).
    Raises an exception on failure so the caller can handle retry logic.
    """
    job_id = job["job_id"]
    job_type = job["type"]
    url = job["url"]
    raw_data = job["data"]

    logger.info("Processing job %s (type=%s)", job_id, job_type)

    pdf_path = None

    try:
        if job_type == "label":
            if not url:
                raise ValueError("label type requires a 'url' field")
            pdf_path = download_pdf(url)
            img = _pdf_to_image(pdf_path)
            tspl = _image_to_tspl(img)

        elif job_type == "pdf":
            if not raw_data:
                raise ValueError("pdf type requires a 'data' field")
            pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
            with open(pdf_path, "wb") as f:
                f.write(base64.b64decode(raw_data))
            img = _pdf_to_image(pdf_path)
            tspl = _image_to_tspl(img)

        elif job_type == "image":
            if not raw_data:
                raise ValueError("image type requires a 'data' field")
            img_data = base64.b64decode(raw_data)
            img = Image.open(io.BytesIO(img_data)).convert("L")

            # Apply options if provided
            opts = json.loads(job["options"]) if job.get("options") else {}
            w_mm = int(opts.get("width_mm", LABEL_WIDTH_MM))
            h_mm = int(opts.get("height_mm", LABEL_HEIGHT_MM))
            target_w = round(w_mm * PRINTER_DPI / 25.4)
            target_h = round(h_mm * PRINTER_DPI / 25.4)

            # Detect content and fit to label
            dark = img.point(lambda p: 255 if p < 240 else 0)
            bbox = dark.getbbox()
            if bbox:
                content = img.crop(bbox)
                content.thumbnail((target_w, target_h), Image.LANCZOS)
                canvas = Image.new("L", (target_w, target_h), 255)
                canvas.paste(content, (0, 0))
                img = canvas
            else:
                img = img.resize((target_w, target_h), Image.LANCZOS)

            img = img.convert("1")
            logger.info("Image -> %dx%d mono image", img.width, img.height)
            tspl = _image_to_tspl(img)

        elif job_type == "raw":
            if not raw_data:
                raise ValueError("raw type requires a 'data' field")
            tspl = base64.b64decode(raw_data)

        else:
            raise ValueError(f"Unknown job type: {job_type}")

        result = _send_to_printer(tspl)
        if not result["success"]:
            raise RuntimeError(result.get("error", "CUPS print failed"))

        logger.info("Job %s completed successfully", job_id)
        return "done"

    finally:
        if pdf_path and os.path.exists(pdf_path):
            os.unlink(pdf_path)


def worker_loop():
    """Background thread: polls DB for queued jobs and processes them."""
    logger.info("Worker started (poll interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            conn = get_db()
            now = _now()
            # Find jobs ready to process: queued, or failed-but-due-for-retry
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE status IN ('queued', 'failed')
                   AND (scheduled_at IS NULL OR scheduled_at <= ?)
                   ORDER BY created_at ASC
                   LIMIT 1""",
                (now,),
            ).fetchall()

            for row in rows:
                job = dict(row)
                job_id = job["job_id"]

                conn.execute(
                    "UPDATE jobs SET status = 'printing', updated_at = ? WHERE job_id = ?",
                    (_now(), job_id),
                )
                conn.commit()

                try:
                    new_status = process_job(job)
                    conn.execute(
                        "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                        (new_status, _now(), job_id),
                    )
                except Exception as exc:
                    retry_count = job["retry_count"] + 1
                    max_retries = job.get("max_retries", MAX_RETRIES)
                    error_str = str(exc)
                    logger.warning("Job %s failed (attempt %d/%d): %s",
                                   job_id, retry_count, max_retries, error_str)

                    if retry_count >= max_retries:
                        conn.execute(
                            """UPDATE jobs SET status = 'failed', retry_count = ?,
                               error = ?, updated_at = ? WHERE job_id = ?""",
                            (retry_count, error_str, _now(), job_id),
                        )
                    else:
                        delay = _backoff_delay(retry_count)
                        scheduled = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                        conn.execute(
                            """UPDATE jobs SET status = 'failed', retry_count = ?,
                               error = ?, updated_at = ?, scheduled_at = ? WHERE job_id = ?""",
                            (retry_count, error_str, _now(), scheduled, job_id),
                        )
                        logger.info("Job %s retry scheduled in %ds", job_id, delay)

                conn.commit()
                conn.close()
                conn = get_db()

        except Exception as exc:
            logger.error("Worker error: %s", exc)

        time.sleep(POLL_INTERVAL)


# ── API Endpoints ────────────────────────────────────────────────────────────────


def _serialize_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Don't expose binary data in API responses
    d.pop("data", None)
    return d


@app.route("/print/job", methods=["POST"])
def submit_job():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "request body must be JSON"}), 400

    job_type = body.get("type")
    if job_type not in ("label", "pdf", "image", "raw"):
        return jsonify({"error": f"invalid type '{job_type}'; choose from: label, pdf, image, raw"}), 400

    if job_type == "label" and not body.get("url"):
        return jsonify({"error": "label type requires 'url' field"}), 400

    if job_type in ("pdf", "image", "raw") and not body.get("data"):
        return jsonify({"error": f"{job_type} type requires 'data' field (base64)"}), 400

    job_id = str(uuid.uuid4())
    now = _now()

    options = json.dumps(body.get("options", {})) if body.get("options") else None

    data_raw = None
    if body.get("data"):
        data_raw = body["data"]

    conn = get_db()
    conn.execute(
        """INSERT INTO jobs (job_id, type, status, url, data, options, retry_count,
           max_retries, error, created_at, updated_at, scheduled_at)
           VALUES (?, ?, 'queued', ?, ?, ?, 0, ?, NULL, ?, ?, NULL)""",
        (job_id, job_type, body.get("url"), data_raw, options,
         body.get("max_retries", MAX_RETRIES), now, now),
    )
    conn.commit()
    conn.close()

    logger.info("Job %s submitted (type=%s)", job_id, job_type)
    return jsonify({"job_id": job_id, "status": "queued"}), 201


@app.route("/print/status/<job_id>")
def get_status(job_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "job not found"}), 404
    return jsonify(_serialize_job(row))


@app.route("/print/queue")
def get_queue():
    limit = request.args.get("limit", 50, type=int)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
        (min(limit, 200),),
    ).fetchall()
    conn.close()
    return jsonify([_serialize_job(r) for r in rows])


@app.route("/print/printers")
def api_printers():
    """List CUPS printers on this host."""
    try:
        result = subprocess.run(
            ["lpstat", "-p"],
            capture_output=True, text=True, timeout=10,
            env=_CUPS_ENV,
        )
        printers = []
        for line in result.stdout.strip().splitlines():
            if line.startswith("printer "):
                parts = line.split()
                if len(parts) >= 2:
                    printers.append(parts[1])
        return jsonify({"printers": printers})
    except Exception as exc:
        logger.warning("Could not list printers: %s", exc)
        return jsonify({"printers": [], "error": str(exc)})


@app.route("/print/health")
def health():
    return jsonify({"ok": True, "printer": PRINTER_NAME, "queue_size": _queue_size()})


def _queue_size() -> int:
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE status = 'queued'").fetchone()["c"]
    conn.close()
    return count


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    init_db()

    # Start background worker
    worker = threading.Thread(target=worker_loop, name="spool-worker", daemon=True)
    worker.start()
    logger.info("Print Spool Service starting on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
