"""Raw-HID feed for the Zoom75's built-in CPU / GPU / fan / network screens.

Those screens are fed over **USB**, not BLE: the vendor's Windows app
(MeletrixID, which uses LibreHardwareMonitor) pushes 32-byte reports to the
keyboard's vendor HID interface. The phone app has no such command, which is
why the screens read 0 with only the BLE tooling running.

Ported from MeletrixID 3.3.5.2 (`wuque.MainWindow`), the `hidZoomDevices`
branches specifically -- the Tiga/Dyna/Hetix models use a different, bundled
report.

Report layout (32 bytes, all offsets fixed)::

    [0]      0x1C
    [1]      outer type: 0 for a sensor value, 3 for the clock
    [2..4]   0
    [5]      outer length: 8 for sensors, 15 for the clock
    [6..7]   CRC-16/CCITT-FALSE over the whole report, low byte first
    [8]      0xA5 marker
    [9]      field id
    [10..11] data length, big-endian
    [12..]   data, always starting with a zero byte
    [12+len] checksum: (sum(report) & 0xFF ^ 0xFF) % 255

Both checksums are computed with the fields they occupy still zeroed, and the
CRC is computed after [0], [1] and [5] are filled in but before [6..7] are.
"""

from __future__ import annotations

import glob
import os
import struct
from dataclasses import dataclass

from .devices import DEFAULT, Device, by_usb

VENDOR_ID = 0x1EA7
PRODUCT_IDS = (0xCD68, 0xCED3)
# Every vendor/product pair across the model range, for discovery.
ALL_USB_IDS = ((0x1EA7, 0xCD68), (0x1EA7, 0xCED3), (0x1EA7, 0xCEDD),
               (0x5542, 0xC987), (0x1EA7, 0xD587), (0x36B5, 0x287F))
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61
REPORT_SIZE = 32

FIELD_CPU_TEMP = 0x37
FIELD_GPU_TEMP = 0x38
FIELD_FAN_RPM = 0x39
FIELD_NET_SPEED = 0x3D
FIELD_WEATHER = 0x3B

# The Tiga / Dyna / Hetix firmware takes every sensor in one report instead of
# one report per value, under two different field ids.
FIELD_BUNDLE = 0xFF
FIELD_BUNDLE_WEATHER = 0xFE
OUTER_BUNDLE = (2, 16)
OUTER_BUNDLE_WEATHER = (2, 13)
FIELD_TIME = 0x38  # same id as GPU; told apart by outer type and length

OUTER_SENSOR = (0, 8)
OUTER_TIME = (3, 15)
OUTER_WEATHER = (0, 9)

# Icon indices the firmware understands, from MeletrixID's condition mapping.
WEATHER_CLEAR_DAY = 0
WEATHER_PARTLY_DAY = 1
WEATHER_PARTLY_NIGHT = 3
WEATHER_CLEAR_NIGHT = 4
WEATHER_CLOUDY = 5
WEATHER_RAIN = 6
WEATHER_SNOW = 7
WEATHER_THUNDER = 8


class HidError(RuntimeError):
    pass


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: init 0xFFFF, poly 0x1021, MSB first, no reflection."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_report(field: int, data: bytes, outer=OUTER_SENSOR) -> bytes:
    if len(data) > REPORT_SIZE - 13:
        raise ValueError("data too long for a 32-byte report")
    buf = bytearray(REPORT_SIZE)
    buf[8] = 0xA5
    buf[9] = field
    buf[10], buf[11] = (len(data) >> 8) & 0xFF, len(data) & 0xFF
    buf[12 : 12 + len(data)] = data
    buf[12 + len(data)] = ((sum(buf) & 0xFF) ^ 0xFF) % 255
    buf[0] = 0x1C
    buf[1] = outer[0]
    buf[5] = outer[1]
    crc = crc16_ccitt(bytes(buf))
    buf[7] = (crc >> 8) & 0xFF
    buf[6] = crc & 0xFF
    return bytes(buf)


def report_cpu_temp(celsius: int) -> bytes:
    return build_report(FIELD_CPU_TEMP, bytes([0, 0, celsius & 0xFF]))


def report_gpu_temp(celsius: int) -> bytes:
    return build_report(FIELD_GPU_TEMP, bytes([0, 0, celsius & 0xFF]))


def report_fan_rpm(rpm: int) -> bytes:
    return build_report(FIELD_FAN_RPM, bytes([0]) + struct.pack(">H", rpm & 0xFFFF))


def report_net_speed(bytes_per_sec: int) -> bytes:
    return build_report(FIELD_NET_SPEED, bytes([0]) + struct.pack(">I", bytes_per_sec & 0xFFFFFFFF))


def encode_temp(celsius: int) -> int:
    """Temperature as the firmware wants it: magnitude with bit 15 as the sign."""
    return int(celsius) if celsius >= 0 else (int(-celsius) | 0x8000)


def report_weather(code: int, celsius: int) -> bytes:
    """Weather report. The vendor writes a literal 0xFF where the checksum
    would go rather than computing one, so that is reproduced here."""
    te = encode_temp(celsius)
    buf = bytearray(REPORT_SIZE)
    buf[8] = 0xA5
    buf[9] = FIELD_WEATHER
    buf[10], buf[11] = 0, 4
    buf[12] = 0
    buf[13] = code & 0xFF
    buf[14] = (te >> 8) & 0xFF
    buf[15] = te & 0xFF
    buf[16] = 0xFF
    buf[0] = 0x1C
    buf[1] = OUTER_WEATHER[0]
    buf[5] = OUTER_WEATHER[1]
    crc = crc16_ccitt(bytes(buf))
    buf[7] = (crc >> 8) & 0xFF
    buf[6] = crc & 0xFF
    return bytes(buf)


def wmo_to_icon(wmo: int, is_night: bool = False) -> int:
    """Map an Open-Meteo WMO weather code onto the firmware's icon set."""
    if wmo == 0:
        return WEATHER_CLEAR_NIGHT if is_night else WEATHER_CLEAR_DAY
    if wmo in (1, 2):
        return WEATHER_PARTLY_NIGHT if is_night else WEATHER_PARTLY_DAY
    if wmo in (3, 45, 48):
        return WEATHER_CLOUDY
    if 51 <= wmo <= 67 or 80 <= wmo <= 82:
        return WEATHER_RAIN
    if 71 <= wmo <= 77 or wmo in (85, 86):
        return WEATHER_SNOW
    if wmo >= 95:
        return WEATHER_THUNDER
    return WEATHER_CLEAR_DAY


def report_bundle(cpu_c: int, gpu_c: int, ssd_c: int, fan_rpm: int, net_bps: int) -> bytes:
    """Single combined report for the Tiga / Dyna / Hetix firmware.

    Note the vendor writes a literal 0xFF in the checksum slot for this style
    rather than computing one, and the fan field is a single byte, so speeds
    above 255 rpm wrap. Both quirks are reproduced -- the firmware is matched
    against the vendor's output, not against what would be sensible.
    """
    buf = bytearray(REPORT_SIZE)
    buf[8] = 0xA5
    buf[9] = FIELD_BUNDLE
    buf[10], buf[11] = 0x00, 0x0B
    buf[12] = 0
    buf[13] = 0
    buf[14] = cpu_c & 0xFF
    buf[16] = gpu_c & 0xFF
    buf[18] = ssd_c & 0xFF
    buf[20] = fan_rpm & 0xFF
    buf[21] = (net_bps >> 8) & 0xFF
    buf[22] = net_bps & 0xFF
    buf[23] = 0xFF
    buf[0] = 0x1C
    buf[1], buf[5] = OUTER_BUNDLE
    crc = crc16_ccitt(bytes(buf))
    buf[7] = (crc >> 8) & 0xFF
    buf[6] = crc & 0xFF
    return bytes(buf)


def report_bundle_weather(code: int, temp_c: int, temp_max: int, temp_min: int) -> bytes:
    """Weather for the bundled-style firmware: current, high and low."""
    buf = bytearray(REPORT_SIZE)
    buf[8] = 0xA5
    buf[9] = FIELD_BUNDLE_WEATHER
    buf[10], buf[11] = 0x00, 0x08
    buf[12] = 0
    buf[13] = code & 0xFF
    buf[14:16] = struct.pack(">H", encode_temp(temp_c))
    buf[16:18] = struct.pack(">H", encode_temp(temp_max))
    buf[18:20] = struct.pack(">H", encode_temp(temp_min))
    buf[20] = 0xFF
    buf[0] = 0x1C
    buf[1], buf[5] = OUTER_BUNDLE_WEATHER
    crc = crc16_ccitt(bytes(buf))
    buf[7] = (crc >> 8) & 0xFF
    buf[6] = crc & 0xFF
    return bytes(buf)


def report_time(when) -> bytes:
    """Clock report. Weekday is 0=Sunday, matching .NET's DayOfWeek."""
    dow = (when.weekday() + 1) % 7
    data = (
        bytes([0, 1])
        + struct.pack(">H", when.year)
        + bytes([when.month, when.day, when.hour, when.minute, when.second, dow])
    )
    return build_report(FIELD_TIME, data, outer=OUTER_TIME)


# --- device discovery ------------------------------------------------------


@dataclass(frozen=True)
class RawHidDevice:
    path: str
    vendor_id: int
    product_id: int
    device: Device = DEFAULT

    def write(self, report: bytes):
        """Write one report. Linux hidraw always wants a leading report-id
        byte; this interface has no numbered reports, so it is zero."""
        try:
            fd = os.open(self.path, os.O_WRONLY)
        except PermissionError as e:
            raise HidError(
                f"cannot open {self.path}: {e}. The hidraw nodes are root-only "
                "by design so that nothing unprivileged can read the keyboard's "
                "input interfaces. Run the zoom75-screen service (see "
                "install-service.sh), or use sudo for a one-off."
            ) from e
        try:
            os.write(fd, b"\\x00" + report)
        finally:
            os.close(fd)


def _sysfs_ids(hidraw: str):
    """Read (bus, vid, pid) and the report descriptor for a hidraw node."""
    dev = f"/sys/class/hidraw/{hidraw}/device"
    try:
        with open(f"{dev}/uevent") as fh:
            fields = dict(
                line.split("=", 1) for line in fh.read().splitlines() if "=" in line
            )
        bus, vid, pid = (int(x, 16) for x in fields["HID_ID"].split(":"))
        with open(f"{dev}/report_descriptor", "rb") as fh:
            desc = fh.read()
        return vid, pid, desc
    except (OSError, KeyError, ValueError):
        return None, None, b""


def _is_raw_hid(desc: bytes) -> bool:
    """Usage page 0xFF60 with usage 0x61 -- the vendor raw-HID interface."""
    return desc.startswith(bytes([0x06, 0x60, 0xFF, 0x09, 0x61]))


def find_devices() -> list[RawHidDevice]:
    """Every vendor raw-HID interface present, across the whole model range."""
    out = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        name = os.path.basename(node)
        vid, pid, desc = _sysfs_ids(name)
        if (vid, pid) in ALL_USB_IDS and _is_raw_hid(desc):
            out.append(RawHidDevice(f"/dev/{name}", vid, pid, by_usb(vid, pid) or DEFAULT))
    return out


def find_device() -> RawHidDevice:
    devices = find_devices()
    if not devices:
        raise HidError(
            "no Zoom75 raw-HID interface found. The keyboard must be connected "
            "by USB (or its 2.4GHz dongle) -- these screens are not fed over BLE."
        )
    return devices[0]


UDEV_RULE = (
    'KERNEL=="hidraw*", ATTRS{idVendor}=="1ea7", ATTRS{idProduct}=="ced3", '
    'ATTRS{bInterfaceNumber}=="01", MODE="0660", TAG+="uaccess"\n'
    'KERNEL=="hidraw*", ATTRS{idVendor}=="1ea7", ATTRS{idProduct}=="cd68", '
    'ATTRS{bInterfaceNumber}=="01", MODE="0660", TAG+="uaccess"\n'
)
"""The interface-number match matters: interface 00 is the boot keyboard and
carries keystrokes. A rule matching only vendor/product would expose that node
to any process running as the user, i.e. unprivileged keylogging."""
