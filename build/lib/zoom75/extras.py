"""Extras beyond what the vendor apps do: now-playing and slideshows.

These are host-side features -- the screen only ever receives pixels, so
anything renderable is fair game.
"""

from __future__ import annotations

import random
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, ImageDraw, ImageFilter

from .dashboard import BG, DIM, TEXT, _font, _right
from .devices import DEFAULT, Device
from .image import fit, to_rgb565

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}


# --- slideshow -------------------------------------------------------------


def slideshow_paths(folder: str | Path, shuffle: bool = False) -> list[Path]:
    files = sorted(
        p for p in Path(folder).iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if shuffle:
        random.shuffle(files)
    return files


# --- now playing -----------------------------------------------------------


async def now_playing() -> dict | None:
    """Current track from any MPRIS player on the session bus.

    Returns None when no player is present or nothing is loaded. Uses
    dbus-fast, which bleak already depends on, so this adds no new requirement.
    """
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        reply = await bus.call(
            _method("org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "ListNames")
        )
        names = [n for n in reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
        for name in names:
            props = await _props(bus, name)
            if not props:
                continue
            meta = props.get("Metadata")
            if not meta:
                continue
            title = _first(meta.get("xesam:title"))
            if not title:
                continue
            return {
                "player": name.rsplit(".", 1)[-1],
                "status": props.get("PlaybackStatus") or "",
                "title": title,
                "artist": _first(meta.get("xesam:artist")),
                "album": _first(meta.get("xesam:album")),
                "art": _local_art(_first(meta.get("mpris:artUrl"))),
            }
        return None
    finally:
        bus.disconnect()


def _method(dest, path, iface, member, signature=None, body=None):
    from dbus_fast import Message
    return Message(destination=dest, path=path, interface=iface, member=member,
                   signature=signature or "", body=body or [])


async def _props(bus, name):
    try:
        reply = await bus.call(
            _method(name, "/org/mpris/MediaPlayer2",
                    "org.freedesktop.DBus.Properties", "GetAll",
                    "s", ["org.mpris.MediaPlayer2.Player"])
        )
    except Exception:
        return None
    if not reply.body:
        return None
    return {k: v.value for k, v in reply.body[0].items()}


def _first(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


def _local_art(url: str) -> Path | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None            # remote art is not fetched; the panel is offline
    p = Path(unquote(parsed.path))
    return p if p.is_file() else None


def render_now_playing(track: dict, device: Device = DEFAULT) -> Image.Image:
    """Album art bled across the full panel, with the text over it."""
    W, H = device.width, device.height
    img = Image.new("RGB", (W, H), BG)

    if track.get("art"):
        try:
            with Image.open(track["art"]) as art:
                bg = fit(art, "cover", device).filter(ImageFilter.GaussianBlur(14))
                img.paste(Image.blend(Image.new("RGB", (W, H), BG), bg, 0.55))
                cover = art.convert("RGB").resize((H - 40, H - 40), Image.LANCZOS)
                img.paste(cover, (18, 20))
        except OSError:
            pass

    d = ImageDraw.Draw(img)
    left = (H - 40) + 34 if track.get("art") else 16
    avail = W - left - 12

    def shrink(text, px, weight=True):
        for size in range(px, 10, -1):
            f = _font(size, bold=weight)
            if d.textlength(text, font=f) <= avail:
                return f, text
        f = _font(11, bold=weight)
        while text and d.textlength(text + "…", font=f) > avail:
            text = text[:-1]
        return f, (text + "…" if text else "")

    y = 40 if device.height < 300 else H // 3
    f, t = shrink(track["title"], 26)
    d.text((left, y), t, font=f, fill=TEXT)
    if track.get("artist"):
        f, t = shrink(track["artist"], 19, weight=False)
        d.text((left, y + 34), t, font=f, fill=(180, 190, 200))
    if track.get("album"):
        f, t = shrink(track["album"], 15, weight=False)
        d.text((left, y + 60), t, font=f, fill=DIM)

    status = track.get("status", "")
    if status:
        _right(d, (W - 12, H - 26), status.lower(), _font(13, bold=False), DIM)
    return img


def now_playing_frame(track: dict, device: Device = DEFAULT) -> bytes:
    return to_rgb565(render_now_playing(track, device), device)
