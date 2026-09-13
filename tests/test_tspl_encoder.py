"""Tests for the TSPL encoder: PIL image -> raw printer byte stream.

The encoder is the core of this project, so these tests pin the exact byte
layout rather than just checking "it returns bytes". A malformed header or an
inverted bitmap makes the printer emit garbage or a blank label.

Verified wire format (8x1 all-black image):

    SIZE 1 mm, 0 mm\\r\\nGAP 3 mm, 0 mm\\r\\nDIRECTION 0\\r\\nDENSITY 8\\r\\n
    CLS\\r\\nBITMAP 0,0,1,1,1,\\x00\\r\\nPRINT 1\\r\\n

Note the deliberate de-bolding: PIL mode "1" draws black with 0, while TSPL
sets a bit to mean "heat this dot" (i.e. draw). So a black source pixel is a
ZERO byte bit here, and the encoder XORs with 0xFF. These tests exist mainly to
stop someone "fixing" that inversion and blanking every label.
"""

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spool  # noqa: E402

TRAILER = b"\r\nPRINT 1\r\n"


def encode(img: Image.Image) -> bytes:
    return spool._image_to_tspl(img)


def payload(img: Image.Image) -> bytes:
    """Extract just the bitmap bytes, after the final 'BITMAP x,y,w,h,mode,' field."""
    out = encode(img)
    head, _, rest = out.partition(b"BITMAP ")
    assert head, "BITMAP command missing"
    # rest is '0,0,<w>,<h>,<mode>,' followed by raw bytes
    _, _, rest = rest.partition(b",")
    _, _, rest = rest.partition(b",")
    _, _, rest = rest.partition(b",")
    _, _, rest = rest.partition(b",")
    _, _, rest = rest.partition(b",")
    return rest[: -len(TRAILER)]


# ── Header ───────────────────────────────────────────────────────────────────


def test_header_contains_required_tspl_commands():
    header = encode(Image.new("1", (8, 8), 1)).split(b"BITMAP")[0]
    assert b"SIZE " in header
    assert b"GAP 3 mm, 0 mm" in header
    assert b"DIRECTION 0" in header
    assert b"DENSITY 8" in header
    assert b"CLS" in header


def test_terminates_with_print_command():
    assert encode(Image.new("1", (8, 8), 1)).endswith(TRAILER)


def test_size_header_derives_mm_from_dpi():
    """SIZE must be label dimensions in mm, computed from the printer DPI."""
    w, h = 203, 406  # 1in x 2in at 203dpi
    out = encode(Image.new("1", (w, h), 1))
    exp_w = round(w * 25.4 / spool.PRINTER_DPI)
    exp_h = round(h * 25.4 / spool.PRINTER_DPI)
    assert f"SIZE {exp_w} mm, {exp_h} mm".encode() in out


def test_bitmap_origin_and_mode():
    out = encode(Image.new("1", (8, 2), 1))
    assert b"BITMAP 0,0,1,2,1," in out, "origin 0,0 / mode 1 required"


# ── Bitmap width in bytes ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "width,expected_bytes",
    [(8, 1), (1, 1), (7, 1), (9, 2), (16, 2), (17, 3)],
)
def test_bitmap_width_bytes_rounds_up(width, expected_bytes):
    """TSPL BITMAP takes width in BYTES, not pixels. Partial bytes round up."""
    out = encode(Image.new("1", (width, 1), 1))
    after = out.split(b"BITMAP ")[1].split(b",")
    assert int(after[2]) == expected_bytes


def test_payload_length_is_width_bytes_times_height():
    w, h = 12, 3
    assert len(payload(Image.new("1", (w, h), 1))) == ((w + 7) // 8) * h


# ── Polarity (the important one) ─────────────────────────────────────────────


def test_all_black_image_is_all_zero_bytes():
    """PIL black (0) is a CLEARED bit. See module docstring."""
    assert payload(Image.new("1", (8, 1), 0)) == b"\x00"


def test_all_white_image_is_all_one_bytes():
    assert payload(Image.new("1", (8, 1), 1)) == b"\xff"


def test_polarity_differs_between_black_and_white():
    assert payload(Image.new("1", (8, 1), 0)) != payload(Image.new("1", (8, 1), 1))


# ── Bit order within a byte ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "x,expected",
    [(0, 0x7F), (1, 0xBF), (2, 0xDF), (3, 0xEF),
     (4, 0xF7), (5, 0xFB), (6, 0xFD), (7, 0xFE)],
)
def test_black_pixel_clears_its_own_bit_only(x, expected):
    """Pixel x=0 is the MSB. TSPL prints MSB-first within each byte."""
    img = Image.new("1", (8, 1), 1)  # white background
    img.putpixel((x, 0), 0)          # single black dot
    assert payload(img) == bytes([expected])


# ── Row ordering ─────────────────────────────────────────────────────────────


def test_rows_emitted_top_to_bottom():
    """Row 0 of the image is the first row of the payload."""
    img = Image.new("1", (8, 2), 1)
    img.putpixel((0, 0), 0)  # black on row 0 only
    # row0 has bit7 cleared -> 0x7F ; row1 all white -> 0xFF
    assert payload(img) == b"\x7f\xff"


# ── Retry backoff ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("retry,expected", [(0, 10), (1, 30), (2, 90), (3, 270)])
def test_backoff_delay_is_exponential(retry, expected):
    assert spool._backoff_delay(retry) == expected
