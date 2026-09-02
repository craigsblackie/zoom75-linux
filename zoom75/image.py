"""Pixel conversion and the animated "DLX" container the screen expects."""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence

from .protocol import SCREEN_HEIGHT, SCREEN_WIDTH

GIF_SPEED_DEFAULT = 5  # vendor default; 1 (slowest) .. 10 (fastest)


def to_rgb565(img: Image.Image) -> bytes:
    """Pack a 320x172 RGB image into big-endian RGB565, row-major."""
    if img.size != (SCREEN_WIDTH, SCREEN_HEIGHT):
        raise ValueError(f"expected {SCREEN_WIDTH}x{SCREEN_HEIGHT}, got {img.size}")
    out = bytearray()
    for r, g, b in img.convert("RGB").getdata():
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out += struct.pack(">H", v)
    return bytes(out)


def fit(img: Image.Image, mode: str = "cover") -> Image.Image:
    """Resize to the panel. 'cover' crops to fill, 'contain' letterboxes,
    'stretch' ignores the aspect ratio."""
    img = img.convert("RGB")
    target = (SCREEN_WIDTH, SCREEN_HEIGHT)
    if mode == "stretch":
        return img.resize(target, Image.LANCZOS)
    if mode == "contain":
        out = Image.new("RGB", target, (0, 0, 0))
        copy = img.copy()
        copy.thumbnail(target, Image.LANCZOS)
        out.paste(copy, ((SCREEN_WIDTH - copy.width) // 2, (SCREEN_HEIGHT - copy.height) // 2))
        return out
    scale = max(SCREEN_WIDTH / img.width, SCREEN_HEIGHT / img.height)
    scaled = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    left = (scaled.width - SCREEN_WIDTH) // 2
    top = (scaled.height - SCREEN_HEIGHT) // 2
    return scaled.crop((left, top, left + SCREEN_WIDTH, top + SCREEN_HEIGHT))


def load_still(path: str | Path, mode: str = "cover") -> bytes:
    with Image.open(path) as img:
        return to_rgb565(fit(img, mode))


# The vendor app subsamples animations down to this many frames for the Zoom75
# before uploading; longer ones are thinned rather than truncated.
MAX_FRAMES = 9


def load_frames(path: str | Path, mode: str = "cover", max_frames: int | None = MAX_FRAMES) -> list[bytes]:
    """Decode an animation, thinning it evenly to ``max_frames``.

    Every frame costs 110,080 bytes and roughly 30s of transfer, so keeping the
    count down matters more here than it would over USB.
    """
    with Image.open(path) as img:
        raw = [fit(frame, mode) for frame in ImageSequence.Iterator(img)]
    if max_frames and len(raw) > max_frames:
        step = len(raw) / max_frames
        raw = [raw[int(i * step)] for i in range(max_frames)]
    return [to_rgb565(f) for f in raw]


def render_text(
    text: str,
    *,
    font_path: str | None = None,
    size: int | None = None,
    fg: tuple[int, int, int] = (255, 255, 255),
    bg: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """Lay a short string out over the whole panel, auto-sizing to fit."""
    img = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    lines = text.split("\n")

    def load(px: int):
        for cand in filter(None, [font_path, *_FONT_CANDIDATES]):
            try:
                return ImageFont.truetype(cand, px)
            except OSError:
                continue
        return ImageFont.load_default()

    def measure(font):
        w = h = 0
        for line in lines:
            box = draw.textbbox((0, 0), line or " ", font=font)
            w = max(w, box[2] - box[0])
            h += box[3] - box[1] + 6
        return w, h

    if size:
        font = load(size)
    else:
        font = load(16)
        for px in range(120, 11, -2):
            cand = load(px)
            w, h = measure(cand)
            if w <= SCREEN_WIDTH - 16 and h <= SCREEN_HEIGHT - 16:
                font = cand
                break

    _, total_h = measure(font)
    y = (SCREEN_HEIGHT - total_h) // 2
    for line in lines:
        box = draw.textbbox((0, 0), line or " ", font=font)
        draw.text(((SCREEN_WIDTH - (box[2] - box[0])) // 2 - box[0], y - box[1]), line, font=font, fill=fg)
        y += box[3] - box[1] + 6
    return to_rgb565(img)


_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]


# --- animated container ----------------------------------------------------


def _descriptor(kind: int, sub: int, *, frames: int = 0, speed: int = 0) -> bytes:
    """One 28-byte descriptor record. Bytes 10..13 carry the panel geometry as
    two big-endian 16-bit values (320 x 172)."""
    rec = bytearray(28)
    rec[0] = kind
    rec[1] = sub
    if kind == 21:  # image header record
        rec[7] = 1
    else:
        if sub == 3:
            rec[3] = 1
            rec[4] = speed
            rec[7] = frames
    rec[10:14] = struct.pack(">HH", SCREEN_WIDTH, SCREEN_HEIGHT)
    return bytes(rec)


def build_animation(frames: list[bytes], speed: int = GIF_SPEED_DEFAULT) -> bytes:
    """Assemble the multi-frame blob ("DLX" container) the screen flashes.

    Layout: a 28-byte magic header, 324 filler bytes, four little-endian
    section offsets, then the descriptor table (B), the per-frame offset table
    (C), the frame pixels (D) and a 4-byte trailer.
    """
    if not frames:
        raise ValueError("no frames")
    n = len(frames)
    delay = 11 - max(1, min(10, speed))

    magic = bytearray(28)
    magic[0:4] = b"\x00DLX"
    magic[4:8] = bytes([0xFC, 0xFF, 0x00, 0x00])
    magic[8:12] = bytes([0x60, 0x01, 0x03, 0x00])
    magic[12] = n & 0xFF
    magic[15] = 1

    section_b = (
        _descriptor(21, 1)
        + _descriptor(20, 3, frames=n, speed=delay)
        + _descriptor(20, 4)
    )

    offsets = bytearray()
    pos = 0
    for f in frames:
        offsets += struct.pack("<I", pos)
        pos += len(f)
    section_c = bytes(offsets)
    section_d = b"".join(frames)

    a_end = 368  # 28 + 324 + 4 offsets * 4 bytes
    table = (
        struct.pack("<I", a_end)
        + struct.pack("<I", a_end + len(section_b))
        + struct.pack("<I", a_end + len(section_b) + len(section_c))
        + struct.pack("<I", a_end + len(section_b) + len(section_c) + len(section_d))
    )

    return (
        bytes(magic)
        + b"\xff" * 324
        + table
        + section_b
        + section_c
        + section_d
        + bytes([0xFC, 0xFF, 0x00, 0x00])
    )
