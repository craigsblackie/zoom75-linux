"""Per-model differences, in one place.

The vendor spreads its models across two apps and several code paths. This
collects what actually differs: panel geometry, USB ids, and which of the two
raw-HID report styles a model wants.

Only the Zoom75 has been verified against hardware. Everything else is
transcribed from the vendor clients and marked accordingly, so a user on
another board knows what they are trusting.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Device:
    name: str
    width: int
    height: int
    usb_vid: int | None = None
    usb_pids: tuple[int, ...] = ()
    #: "single" = one report per sensor (Zoom family); "bundled" = every sensor
    #: in one report (Tiga / Dyna / Hetix).
    hid_style: str = "single"
    #: Frames the vendor app thins an animation down to before uploading.
    max_frames: int = 9
    verified: bool = False
    notes: str = ""

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 2


ZOOM75 = Device(
    name="zoom75",
    width=320, height=172,
    usb_vid=0x1EA7, usb_pids=(0xCD68, 0xCED3),
    hid_style="single", max_frames=9, verified=True,
    notes="Verified end to end: BLE image/animation upload, clock, notes.",
)

# The 390x390 module the vendor app calls DEVICE_SECOND. Geometry comes from
# the animation descriptor the app builds for it (0x0186 = 390 in both axes).
SECOND_GEN = Device(
    name="second-gen",
    width=390, height=390,
    usb_vid=0x1EA7, usb_pids=(),
    hid_style="single", max_frames=9,
    notes="390x390 round module. Geometry from the vendor animation descriptor; untested.",
)

TIGA = Device(
    name="tiga", width=320, height=172,
    usb_vid=0x1EA7, usb_pids=(0xCEDD,), hid_style="bundled",
    notes="Bundled HID report. Untested.",
)
DYNA = Device(
    name="dyna", width=320, height=172,
    usb_vid=0x5542, usb_pids=(0xC987,), hid_style="bundled",
    notes="Bundled HID report. Untested.",
)
HETIX = Device(
    name="hetix", width=320, height=172,
    usb_vid=0x1EA7, usb_pids=(0xD587,), hid_style="bundled",
    notes="Bundled HID report. Untested.",
)

ALL = (ZOOM75, SECOND_GEN, TIGA, DYNA, HETIX)
DEFAULT = ZOOM75
BY_NAME = {d.name: d for d in ALL}


def by_name(name: str) -> Device:
    try:
        return BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown device {name!r}; choose from {', '.join(BY_NAME)}"
        ) from None


def by_usb(vid: int, pid: int) -> Device | None:
    for d in ALL:
        if d.usb_vid == vid and pid in d.usb_pids:
            return d
    return None
