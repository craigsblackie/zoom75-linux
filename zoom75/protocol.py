"""Wire protocol for the Zoom75 (Meletrix/Wuque) BLE screen module.

Derived from the vendor PocketWuque Android app (com.app.smartkeyboard v1.1.3),
classes com.blala.blalable.{Utils,BleConstant,BleOperateManager} and
com.blala.blalable.keyboard.KeyBoardConstant.

Frame layout (both directions)::

    88 00 00 LL LL LL LL XX  <payload...>
    |  |     |            |
    |  |     |            +-- XOR of every payload byte
    |  |     +--------------- payload length, 4 bytes big-endian
    |  +--------------------- two reserved zero bytes
    +------------------------ product id

The payload starts with a 2-byte opcode. The screen answers a request opcode
with opcode+1, so 0x0013 -> 0x0014, 0x0903 -> 0x0904, 0x0805 -> 0x0806.
"""

from __future__ import annotations

import datetime as _dtmod
import struct
from dataclasses import dataclass

# --- GATT ------------------------------------------------------------------

SERVICE_UUID = "1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa"
WRITE_UUID = "1f400001-aab4-14a3-f1ba-f61f35cddbaa"  # commands
NOTIFY_UUID = "1f400002-aab4-14a3-f1ba-f61f35cddbaa"  # replies
FLASH_WRITE_UUID = "1f400003-aab4-14a3-f1ba-f61f35cddbaa"  # bulk image data
FLASH_NOTIFY_UUID = "1f400004-aab4-14a3-f1ba-f61f35cddbaa"

BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

ADVERTISED_NAME = "Zoom75 Screen"

# --- Panel geometry --------------------------------------------------------

SCREEN_WIDTH = 320
SCREEN_HEIGHT = 172
FRAME_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT * 2  # RGB565

PRODUCT_ID = 0x88
HEADER_LEN = 8

# Bulk-transfer geometry. The image is cut into 4096-byte blocks, each block
# into 243-byte chunks; every chunk carries a trailing XOR byte.
BLOCK_SIZE = 4096
CHUNK_SIZE = 243
FLASH_HEADER_LEN = 25  # prefix on the very first chunk, eats into its payload

# --- Opcodes ---------------------------------------------------------------

OP_DEVICE_VERSION = b"\x00\x01"
OP_SYNC_TIME = b"\x04\x01"
OP_DEVICE_INFO = b"\x04\x02"
OP_SCREEN_MODE = b"\x01\x1c"
OP_STATUS = b"\x00\x13"
OP_NOTEBOOK = b"\x04\x0a"
OP_NOTEBOOK_INFO = b"\x00\x15"
OP_NOTEBOOK_DELETE = b"\x04\x0b"
OP_SYSTEM_DATA = b"\x00\x17"
OP_USE_TIME = b"\x00\x23"
OP_ALARM_READ = b"\x00\x25"
OP_STYLE = b"\x04\x0d"
OP_DIAL_INFO = b"\x09\x03"
OP_DIAL_BEGIN = b"\x08\x03"
OP_DIAL_DATA = b"\x08\x05"
OP_LOCAL_DIAL = b"\x09\x07"
OP_NOTIFY_MSG = b"\x05\x01"
OP_WEATHER = b"\x04\x07"
OP_ALARM_SET = b"\x04\x0c"
OP_UNBIND = b"\x01\x01"

# Present in the vendor SDK but never called from anywhere in the app. The
# three privileged ones share a magic tail (a1 fe 74 69) that reads as a guard
# against issuing them by accident.
OP_FIND_DEVICE = b"\x00\x19"
OP_BACKLIGHT = b"\x02\x36"
OP_SET_LOCAL_DIAL = b"\x01\x1b"
OP_CURRENT_FACE = b"\x01\x61"   # privileged
OP_POWER_OFF = b"\x05\x60"      # privileged
OP_TEST_MODE = b"\x05\xfe"      # privileged
PRIVILEGED_MAGIC = bytes([0xA1, 0xFE, 0x74, 0x69])

# Device timestamps (notes, usage stats) count seconds from 2000-01-01 against
# the wall clock sync_time set. The module has no timezone concept at all, so a
# stamp decodes straight to a naive *local* datetime.
#
# The vendor app adds 946656000, i.e. 2000-01-01T00:00:00+08:00, which bakes in
# its developers' timezone -- outside UTC+8 its own note timestamps are wrong.
# Verified here: a note written at 08:28:38 BST came back as 841652918, which
# is exactly 2000-01-01 + 841652918s, not that minus eight hours.
DEVICE_EPOCH = 946684800          # 2000-01-01T00:00:00Z
VENDOR_EPOCH = 946656000          # what the app uses; kept for reference
DEVICE_ZERO = _dtmod.datetime(2000, 1, 1)

# Generic acknowledgement; payload is 01 followed by the request opcode.
OP_ACK = b"\x07\x02"

# Reply status byte of OP_DIAL_INFO (0x0904).
DIAL_INFO_INVALID = 1
DIAL_INFO_OK = 4
DIAL_INFO_BUSY = 5

# Reply status byte of OP_DIAL_DATA (0x0806) and the sync result codes.
FLASH_FAILED = 1
FLASH_SUCCESS = 2
FLASH_READY = 3
FLASH_ERROR_EXIT = 6

# Device status (0x0014 reply, last payload byte). 3 == armed for a flash write.
STATUS_READY_FOR_FLASH = 3

# The vendor app hard-codes these as the flash start/end keys.
DIAL_KEY = b"\x00\xff\xff\xff"
UI_FEATURE = 65533  # Utf8.REPLACEMENT_CODE_POINT in the decompiled source


def xor(data: bytes) -> int:
    """XOR checksum used in every frame and every bulk chunk."""
    acc = 0
    for b in data:
        acc ^= b
    return acc


def frame(payload: bytes) -> bytes:
    """Wrap a payload in the 8-byte transport header."""
    return (
        bytes([PRODUCT_ID, 0, 0])
        + struct.pack(">I", len(payload))
        + bytes([xor(payload)])
        + payload
    )


@dataclass(frozen=True)
class Reply:
    opcode: bytes
    payload: bytes  # everything after the 2-byte opcode
    raw: bytes

    @property
    def status(self) -> int:
        return self.payload[0] if self.payload else -1

    def __repr__(self) -> str:
        return f"<Reply {self.opcode.hex()} {self.payload.hex()}>"


def parse(raw: bytes) -> Reply | None:
    """Decode a notification. Returns None if it is not a well-formed frame."""
    if len(raw) < HEADER_LEN + 2 or raw[0] != PRODUCT_ID:
        return None
    length = struct.unpack(">I", raw[3:7])[0]
    payload = raw[HEADER_LEN : HEADER_LEN + length]
    if len(payload) < 2:
        return None
    return Reply(opcode=payload[:2], payload=payload[2:], raw=raw)


# --- Command builders ------------------------------------------------------


def cmd_status() -> bytes:
    return frame(OP_STATUS + b"\x00")


def cmd_device_version() -> bytes:
    return frame(OP_DEVICE_VERSION)


def cmd_system_data() -> bytes:
    return frame(OP_SYSTEM_DATA)


def cmd_use_time() -> bytes:
    return frame(OP_USE_TIME + b"\x00")


def cmd_alarm_read() -> bytes:
    return frame(OP_ALARM_READ + b"\x00")


def cmd_sync_time(year, month, day, hour, minute, second) -> bytes:
    return frame(
        OP_SYNC_TIME
        + bytes(
            [
                (year >> 8) & 0xFF,
                year & 0xFF,
                month & 0xFF,
                day & 0xFF,
                hour & 0xFF,
                minute & 0xFF,
                second & 0xFF,
            ]
        )
    )


def cmd_device_info(english: bool = True) -> bytes:
    """KeyBoardConstant.deviceInfoData.

    The app always sends this immediately after a time sync, on every connect.
    The payload is fixed apart from the language byte (1 = non-Chinese).
    """
    return frame(
        OP_DEVICE_INFO
        + bytes([0x02, 0x12, 0xA0, 0x37, 1 if english else 0, 0, 0, 1, 0, 0])
        + bytes([0x00, 0x00, 0x1F, 0x40])
    )


def cmd_screen_mode(mode: int) -> bytes:
    """mode 1 = normal/clock, 2 = image."""
    return frame(OP_SCREEN_MODE + bytes([1, 0, 1, mode & 0xFF]))


def cmd_style(is_screen_style: bool, index: int) -> bytes:
    """Select a built-in style.

    The vendor app only sends this from its `second` package, i.e. to the
    2nd-generation 390x390 module. A generation-1 Zoom75 acks it but the
    built-in dial is a decorative animation, not a live clock -- there is no
    clock readout on this hardware to point it at.
    """
    a = index + 1 if is_screen_style else 0
    b = 0 if is_screen_style else index + 1
    return frame(OP_STYLE + bytes([a & 0xFF, b & 0xFF]))


def cmd_local_dial() -> bytes:
    """Switch back to the keyboard's own built-in dial."""
    return bytes.fromhex("880000000000060009070000FFFE")


def cmd_dial_info(bin_size: int, animated: bool) -> bytes:
    """Announce an upcoming upload: its size and whether it is a GIF."""
    size = struct.pack(">I", bin_size)
    if animated:
        body = (
            bytes([4, 0, 8, 0, 0, 0xFF, 0xFC])
            + size
            + bytes([5, 0, 20])
            + b"\xff" * 20
        )
    else:
        body = bytes([1, 0, 11]) + struct.pack(">I", UI_FEATURE) + size + b"\xff\xff\xff"
    return frame(OP_DIAL_INFO + body)


def cmd_dial_begin() -> bytes:
    return frame(OP_DIAL_BEGIN + DIAL_KEY + DIAL_KEY)


def flash_header(total_len: int) -> bytes:
    """25-byte prefix on the first bulk chunk.

    Note the length field counts the payload plus 17, and the checksum byte is
    hard-coded to zero here -- both quirks are reproduced from the vendor app.
    """
    return (
        bytes([PRODUCT_ID, 0, 0])
        + struct.pack(">I", total_len + 17)
        + bytes([0x00])
        + bytes([0x08, 0x05, 0x01, 0x00, 0x09])
        + DIAL_KEY
        + DIAL_KEY
        + bytes([0x02, 0x02, 0xFF, 0xFF])
    )


def build_flash_blocks(data: bytes) -> list[list[bytes]]:
    """Split image bytes into [block][chunk] packets ready for the flash char.

    Every chunk is <=243 payload bytes plus one XOR byte. The first chunk of
    the first block is prefixed with :func:`flash_header`, which displaces 25
    bytes of payload -- every later chunk offset in that first block is shifted
    back by 25 to compensate.
    """
    blocks = [data[i : i + BLOCK_SIZE] for i in range(0, len(data), BLOCK_SIZE)]
    out: list[list[bytes]] = []
    for block_i, block in enumerate(blocks):
        # Block 0 must carry 25 extra header bytes, so it may need one chunk
        # more than a naive length/243 would suggest. (The vendor app omits
        # this and truncates payloads under ~4KB; real frames are larger.)
        span = len(block) + (FLASH_HEADER_LEN if block_i == 0 else 0)
        n_chunks = -(-span // CHUNK_SIZE)
        chunks: list[bytes] = []
        for i in range(n_chunks):
            if i == 0 and block_i == 0:
                body = flash_header(len(data)) + block[: CHUNK_SIZE - FLASH_HEADER_LEN]
            else:
                start = i * CHUNK_SIZE - (FLASH_HEADER_LEN if block_i == 0 else 0)
                body = block[start : start + CHUNK_SIZE]
            chunks.append(body + bytes([xor(body)]))
        out.append(chunks)
    return out


# --- text ------------------------------------------------------------------


def utf16(text: str) -> bytes:
    """The app encodes strings as UTF-16BE, via a \\uXXXX round-trip."""
    return text.encode("utf-16-be")


def _tlv(tag: int, body: bytes, size: int = 2) -> bytes:
    """Tag-length-value. Length width differs by command: the notification
    builder widens it to 4 bytes (Java `toByteArray(int)`), while the notebook,
    alarm and weather builders use 2."""
    length = struct.pack(">I", len(body)) if size == 4 else struct.pack(">H", len(body))
    return bytes([tag]) + length + body


# --- notifications ---------------------------------------------------------

# App ids the vendor app wires up. The firmware carries iOS bundle ids for
# these too; other values are accepted but the icon shown is unverified.
APP_WECHAT = 5
APP_QQ = 9
APP_DISCORD = 13


def cmd_notify(app_id: int, title: str, body: str) -> bytes:
    """KeyBoardConstant.getMsgNotifyData -- push a notification to the panel."""
    return frame(
        OP_NOTIFY_MSG
        + bytes([0x01, 0x00, 0x01, app_id & 0xFF])
        + _tlv(0x02, utf16(title), size=4)
        + _tlv(0x03, utf16(body), size=4)
    )


# --- notebook --------------------------------------------------------------


def cmd_note(title: str, content: str, when) -> tuple[bytes, bytes]:
    """BleOperateManager.sendKeyBoardNoteBook.

    Two frames: the first carries the timestamp and title, the second the body,
    and the app waits for the first to be acked before sending the second.
    Weekday is 0=Sunday, matching Java's Calendar.DAY_OF_WEEK - 1.
    """
    weekday = (when.weekday() + 1) % 7
    stamp = _tlv(
        0x01,
        struct.pack(">H", when.year)
        + bytes([when.month, when.day, when.hour, when.minute, when.second, weekday]),
    )
    first = frame(OP_NOTEBOOK + stamp + _tlv(0x02, utf16(title)))
    second = frame(OP_NOTEBOOK + _tlv(0x03, utf16(content[:100])))
    return first, second


def cmd_note_info() -> bytes:
    return frame(OP_NOTEBOOK_INFO + b"\x00\x00")


def cmd_note_delete(device_time: int) -> bytes:
    """Delete the note with this device timestamp (seconds since DEVICE_EPOCH)."""
    # The app builds this little-endian then re-indexes it [3][2][1][0],
    # i.e. big-endian on the wire.
    return frame(OP_NOTEBOOK_DELETE + struct.pack(">I", device_time & 0xFFFFFFFF))


# --- alarms ----------------------------------------------------------------


def cmd_alarm_set(alarms) -> bytes:
    """Replace the alarm list. Each entry is (enabled, repeat_mask, hour, minute).

    The index is written with "%02d" into a hex string, so indices above 9 are
    misencoded by the vendor app; this keeps the same encoding for indices 0-9
    and refuses beyond that rather than silently differing.
    """
    if len(alarms) > 10:
        raise ValueError("the vendor index encoding only holds 10 alarms")
    body = bytearray(b"\x05\x05")
    for i, (enabled, repeat, hour, minute) in enumerate(alarms):
        body += bytes.fromhex(f"{i:02d}")
        body += bytes([2 if enabled else 1, repeat & 0xFF, hour & 0xFF, minute & 0xFF])
    return frame(OP_ALARM_SET + b"\x01" + struct.pack(">H", len(body)) + bytes(body))


def parse_alarms(payload: bytes):
    """Decode a 0x0026 reply body (payload is everything after the opcode)."""
    if len(payload) < 5:
        return []
    length = struct.unpack(">H", payload[3:5])[0]
    entries = payload[7 : 7 + max(0, length - 2)]
    out = []
    for i in range(0, len(entries) - 4, 5):
        idx, state, repeat, hour, minute = entries[i : i + 5]
        out.append(
            {"index": idx, "enabled": state == 2, "repeat": repeat, "hour": hour, "minute": minute}
        )
    return out


def device_time(stamp: int) -> _dtmod.datetime:
    """Convert a device timestamp to a naive local datetime."""
    return DEVICE_ZERO + _dtmod.timedelta(seconds=stamp)


def to_device_time(when: _dtmod.datetime) -> int:
    return int((when - DEVICE_ZERO).total_seconds())


def parse_use_time(payload: bytes):
    """Decode a 0x0024 reply body into [(datetime, minutes_used)]."""
    if len(payload) < 5:
        return []
    length = struct.unpack(">H", payload[3:5])[0]
    entries = payload[7 : 7 + max(0, length - 2)]
    out = []
    for i in range(0, len(entries) - 5, 6):
        day = struct.unpack("<I", entries[i : i + 4])[0]
        minutes = struct.unpack(">H", entries[i + 4 : i + 6])[0]
        out.append((device_time(day), minutes))
    return out


def parse_note_info(payload: bytes) -> _dtmod.datetime | None:
    """Decode a 0x0016 reply into the stored note's timestamp, if any."""
    if len(payload) < 4:
        return None
    return device_time(struct.unpack(">I", payload[-4:])[0])


# --- weather ---------------------------------------------------------------


def cmd_weather(
    *, timestamp: int, status: int, temp: float, temp_max: float, temp_min: float,
    humidity: int, uv: int, sunrise: tuple[int, int], sunset: tuple[int, int],
    wind_kph: float | None = None, place: str | None = None,
) -> bytes:
    """SecondHomeViewModel.sendTodayWeather.

    The app only sends this to the 2nd-generation module, but the fixed 22-byte
    block plus an optional 0x31 place tag is the whole format.
    """
    body = bytearray()
    body += struct.pack(">I", (timestamp - DEVICE_EPOCH) & 0xFFFFFFFF)
    body += bytes.fromhex(f"{status:02d}")  # the app formats this as decimal
    body += struct.pack(">h", int(temp * 10))
    body += struct.pack(">h", int(temp_max * 10))
    body += struct.pack(">h", int(temp_min * 10))
    body += struct.pack(">H", 50)  # fixed in the vendor app
    body += struct.pack(">H", humidity * 10)
    body += bytes([uv & 0xFF, sunrise[0] & 0xFF, sunrise[1] & 0xFF, sunset[0] & 0xFF, sunset[1] & 0xFF])
    body += b"\xff" if wind_kph is None else struct.pack(">H", int(wind_kph * 10))
    out = OP_WEATHER + b"\x01" + struct.pack(">H", len(body)) + bytes(body)
    if place:
        out += _tlv(0x31, utf16(place))
    else:
        out += bytes([0x31, 0x00, 0x00])
    return frame(out)


# --- SDK commands the app never sends --------------------------------------


def cmd_find_device() -> bytes:
    return frame(OP_FIND_DEVICE)


def cmd_backlight(level: int, timeout: int) -> bytes:
    return frame(OP_BACKLIGHT + bytes([level & 0xFF, timeout & 0xFF]))


def cmd_set_local_dial(index: int) -> bytes:
    return frame(OP_SET_LOCAL_DIAL + bytes([index & 0xFF]))


def cmd_unbind() -> bytes:
    """Vendor "recycle device". The app sends this from its unbind screens."""
    return frame(OP_UNBIND)


def cmd_current_face(index: int) -> bytes:
    return frame(OP_CURRENT_FACE + bytes([index & 0xFF]) + PRIVILEGED_MAGIC + b"\x02")


def cmd_power_off(mode: int = 1) -> bytes:
    return frame(OP_POWER_OFF + bytes([mode & 0xFF]) + PRIVILEGED_MAGIC)


def cmd_test_mode() -> bytes:
    return frame(OP_TEST_MODE + b"\x54" + PRIVILEGED_MAGIC)
