"""Render a system dashboard sized for the 320x172 panel."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from .image import _FONT_CANDIDATES
from .protocol import SCREEN_HEIGHT, SCREEN_WIDTH
from .stats import Metric, Sample, human_rate

BG = (13, 17, 23)
HEADER_BG = (22, 27, 34)
TRACK = (33, 38, 45)
TEXT = (230, 237, 243)
DIM = (139, 148, 158)
GOOD = (63, 185, 80)
WARN = (210, 153, 34)
HOT = (248, 81, 73)
RX = (88, 166, 255)
TX = (188, 140, 255)

_BOLD = [c for c in _FONT_CANDIDATES]
_REGULAR = [c.replace("-Bold", "") for c in _FONT_CANDIDATES]


def _font(size: int, bold: bool = True):
    for cand in _BOLD if bold else _REGULAR:
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_colour(pct: float | None) -> tuple[int, int, int]:
    if pct is None:
        return DIM
    if pct >= 85:
        return HOT
    if pct >= 60:
        return WARN
    return GOOD


def _right(draw, xy, text, font, fill):
    x, y = xy
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x - (box[2] - box[0]), y), text, font=font, fill=fill)


def _row(draw, y: int, m: Metric, *, bar_x0=54, bar_x1=196, bar_h=14):
    label_f = _font(15)
    value_f = _font(15)
    detail_f = _font(13, bold=False)

    draw.text((10, y - 1), m.label, font=label_f, fill=TEXT)

    pct = max(0.0, min(100.0, m.percent or 0.0))
    colour = _load_colour(m.percent)
    draw.rounded_rectangle([bar_x0, y, bar_x1, y + bar_h], radius=3, fill=TRACK)
    filled = int((bar_x1 - bar_x0) * pct / 100)
    if filled > 2:
        draw.rounded_rectangle([bar_x0, y, bar_x0 + filled, y + bar_h], radius=3, fill=colour)

    _right(draw, (248, y - 1), f"{pct:.0f}%", value_f, TEXT)
    if m.detail:
        _right(draw, (SCREEN_WIDTH - 8, y + 1), m.detail, detail_f, DIM)


def _arrow(draw, x: int, y: int, *, down: bool, colour, w: int = 9, h: int = 15):
    """Draw a traffic arrow. The bundled fonts lack the arrow glyphs, so these
    are drawn as geometry rather than text."""
    mid = x + w // 2
    head = h // 2
    if down:
        draw.rectangle([mid - 1, y, mid + 1, y + h - head], fill=colour)
        draw.polygon([(x, y + h - head - 1), (x + w, y + h - head - 1), (mid, y + h)], fill=colour)
    else:
        draw.rectangle([mid - 1, y + head, mid + 1, y + h], fill=colour)
        draw.polygon([(x, y + head + 1), (x + w, y + head + 1), (mid, y)], fill=colour)


def render(sample: Sample) -> Image.Image:
    img = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), BG)
    d = ImageDraw.Draw(img)

    # Header carries only values that do not go stale between refreshes. A
    # clock lived here once, but a frame takes ~35s to upload and the panel is
    # only redrawn once a minute, so it was always showing a time that had
    # already passed. The module keeps its own real-time clock for that --
    # see `z75 time` and `z75 mode 1`.
    d.rectangle([0, 0, SCREEN_WIDTH - 1, 25], fill=HEADER_BG)
    d.text((10, 4), sample.host[:26], font=_font(15), fill=TEXT)
    if sample.net_iface:
        _right(d, (SCREEN_WIDTH - 8, 6), sample.net_iface, _font(13, bold=False), DIM)

    rows = [sample.cpu, sample.mem]
    if sample.gpu is not None:
        rows.insert(1, sample.gpu)

    # Space the rows evenly in the band between the header and the network strip.
    top, bottom = 36, 128
    step = (bottom - top) // max(1, len(rows))
    for i, m in enumerate(rows):
        _row(d, top + i * step, m)

    # network strip
    d.line([(10, 132), (SCREEN_WIDTH - 10, 132)], fill=TRACK, width=1)
    rate_f = _font(17)

    _arrow(d, 14, 142, down=True, colour=RX)
    d.text((32, 141), human_rate(sample.rx_bps), font=rate_f, fill=TEXT)
    _arrow(d, 170, 142, down=False, colour=TX)
    d.text((188, 141), human_rate(sample.tx_bps), font=rate_f, fill=TEXT)

    return img
