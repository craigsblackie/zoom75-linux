"""FreqChip OTA (firmware update) for the Zoom75 screen module.

DANGER: unlike the display protocol, this erases and rewrites flash. Read the
safety notes in PROTOCOL.md before using it.

Ported from com.app.smartkeyboard.ble.ota.{OtaDialogView,WriterOperation,
OtaUtils} in PocketWuque v1.1.3. The module is a FreqChip part -- the vendor
code writes its NVDS dump to a "Freqchip/" directory, and the command set
matches that SDK.

The design is dual-bank: the device reports the base address of the image it is
running, the host writes the new image to the *other* bank, and only the final
REBOOT command -- which carries the length and CRC -- makes the bootloader
consider switching. An aborted transfer therefore damages only the spare bank.
Every guard in this module exists to keep that property true.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient

# --- GATT ------------------------------------------------------------------

OTA_SERVICE_UUID = "02f00000-0000-0000-0000-00000000fe00"
OTA_WRITE_UUID = "02f00000-0000-0000-0000-00000000ff01"
OTA_NOTIFY_UUID = "02f00000-0000-0000-0000-00000000ff02"

# --- Commands --------------------------------------------------------------

CMD_NVDS_TYPE = 0
CMD_GET_STR_BASE = 1
CMD_PAGE_ERASE = 3
CMD_CHIP_ERASE = 4  # never issued by this module -- it would destroy both banks
CMD_WRITE_DATA = 5
CMD_READ_DATA = 6
CMD_WRITE_MEM = 7
CMD_READ_MEM = 8
CMD_REBOOT = 9

PAGE_SIZE = 4096
WRITE_HEADER_LEN = 9  # cmd + len + addr + datalen, prepended to every data chunk

# Bank bases for the older 8010 part, hard-coded in the vendor app. The 8010H
# reports its own target address instead, and must, because published images
# are larger than the gap between these two.
ADDR_BANK_A = 0x00000
ADDR_BANK_B = 0x14000

# Upper bound for a sanity check; these parts are 512 KB.
MAX_FLASH = 0x80000

# The CRC in the image footer covers everything past this offset.
CRC_SKIP_BYTES = 256

# Firmware header: little-endian u32 version code, matching the value the
# device reports over the display protocol and the vendor's update API.
VERSION_CODE_OFFSET = 0x18

UPDATE_API = "https://wuquedistribution.com:12349/checkUpdate"


class OtaError(RuntimeError):
    pass


class OtaAborted(OtaError):
    """Raised before anything destructive happened."""


# --- CRC -------------------------------------------------------------------


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (0xEDB88320 if c & 1 else 0)
        table.append(c)
    return table


_CRC_TABLE = _crc_table()


def firmware_crc(data: bytes) -> int:
    """The vendor's CRC over the image, skipping the first 256 bytes.

    This is *not* stock CRC-32, and two details are easy to get wrong:

    * `OtaUtils.Crc32CalByByte` shifts *left* while indexing a *reflected*
      table, and indexes it with `crc / 256` -- Java integer division, which
      truncates toward zero. On a negative (high-bit-set) crc that differs from
      `crc >>> 8`, and the two produce completely different results
      (0xf7db5f5c vs 0xa0ff8a33 on the shipped Zoom75 image). The signed
      division is what the vendor app actually computes, so it is what the
      bootloader is comparing against.
    * `getCRC32new` reads the file in 256-byte blocks but skips block 0, so the
      first 256 bytes are excluded even though they are still written to flash.
    """
    crc = 0  # held signed, exactly like the Java int
    for b in data[CRC_SKIP_BYTES:]:
        quotient = crc // 256 if crc >= 0 else -((-crc) // 256)
        crc = ((crc << 8) ^ _CRC_TABLE[(quotient ^ b) & 0xFF]) & 0xFFFFFFFF
        if crc >= 0x80000000:
            crc -= 0x100000000
    return crc & 0xFFFFFFFF


# --- Command encoding ------------------------------------------------------


def cmd_write_op(cmd: int, hdr: int, addr: int, data_len: int) -> bytes:
    """WriterOperation.cmd_write_op.

    Note byte 2 is always zero: the vendor masks `hdr` to 8 bits before
    shifting it right by 8. Reproduced rather than corrected -- the device
    parses what the vendor app sends.
    """
    out = bytearray(7 if cmd == CMD_PAGE_ERASE else 9)
    out[0] = cmd & 0xFF
    out[1] = hdr & 0xFF
    out[2] = 0
    out[3:7] = struct.pack("<I", addr & 0xFFFFFFFF)
    if cmd != CMD_PAGE_ERASE:
        out[7:9] = struct.pack("<H", data_len & 0xFFFF)
    return bytes(out)


def cmd_operation(cmd: int, data_len: int, addr: int) -> bytes | None:
    if cmd in (CMD_WRITE_MEM, CMD_WRITE_DATA):
        return cmd_write_op(cmd, 9, addr, data_len)
    if cmd in (CMD_GET_STR_BASE, CMD_NVDS_TYPE):
        return cmd_write_op(cmd, 3, 0, 0)
    if cmd == CMD_PAGE_ERASE:
        return cmd_write_op(cmd, 7, addr, 0)
    return None


def build_command(cmd: int, addr: int = 0, data: bytes = b"", data_len: int = 0) -> bytes:
    """WriterOperation.send_data payload."""
    head = cmd_operation(cmd, data_len, addr)
    if cmd in (CMD_GET_STR_BASE, CMD_PAGE_ERASE, CMD_NVDS_TYPE):
        assert head is not None
        return head
    if cmd == CMD_REBOOT:
        return bytes([cmd & 0xFF])
    assert head is not None
    return head + data


def build_reboot(crc: int, length: int) -> bytes:
    """WriterOperation.send_data_long -- the commit command."""
    out = bytearray(11)
    out[0] = CMD_REBOOT
    out[1] = 10
    out[2] = 0
    out[3:7] = struct.pack("<I", length & 0xFFFFFFFF)
    out[7:11] = struct.pack("<I", crc & 0xFFFFFFFF)
    return bytes(out)


def reply_u32(value: bytes) -> int:
    """WriterOperation.bytetoint -- little-endian u32 at offset 4."""
    if len(value) < 8:
        raise OtaError(f"short OTA reply: {value.hex()}")
    return struct.unpack("<I", value[4:8])[0]


def reply_u8(value: bytes) -> int:
    if len(value) < 5:
        raise OtaError(f"short OTA reply: {value.hex()}")
    return value[4]


# --- Firmware image --------------------------------------------------------


@dataclass(frozen=True)
class Firmware:
    path: Path
    data: bytes
    version_code: int
    crc: int

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def version(self) -> str:
        v = self.version_code
        return f"V{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"

    @property
    def pages(self) -> int:
        return -(-self.size // PAGE_SIZE)

    @classmethod
    def load(cls, path: str | Path) -> "Firmware":
        p = Path(path)
        data = p.read_bytes()
        if len(data) <= CRC_SKIP_BYTES:
            raise OtaError(f"{p} is only {len(data)} bytes -- not a firmware image")
        if len(data) % 4:
            raise OtaError(f"{p} length {len(data)} is not word-aligned; refusing")
        version_code = struct.unpack_from("<I", data, VERSION_CODE_OFFSET)[0]
        if not 0 < version_code < 0x01000000:
            raise OtaError(
                f"{p} has an implausible version code 0x{version_code:08x} at "
                f"offset 0x{VERSION_CODE_OFFSET:02x}; refusing to treat it as firmware"
            )
        return cls(path=p, data=data, version_code=version_code, crc=firmware_crc(data))


@dataclass(frozen=True)
class OtaInfo:
    """Result of the read-only probe."""

    nvds_type: int
    large_mtu: bool  # the 0x10 bit -- the 8010H variant
    base_addr: int
    mtu: int

    @property
    def variant(self) -> str:
        return "8010H (device-directed address)" if self.large_mtu else "8010 (fixed A/B banks)"


class OtaClient:
    """Drives the OTA service. Read-only until :meth:`flash` is called."""

    def __init__(self, client: BleakClient, *, verbose: bool = False):
        self._c = client
        self._verbose = verbose
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()
        self._started = False

    def _log(self, *a):
        if self._verbose:
            print(*a)

    async def start(self):
        if self._started:
            return
        svc = self._c.services.get_service(OTA_SERVICE_UUID)
        if svc is None:
            raise OtaError("device does not expose the OTA service")
        await self._c.start_notify(OTA_NOTIFY_UUID, self._on_notify)
        self._started = True

    async def stop(self):
        if self._started:
            with contextlib.suppress(Exception):
                await self._c.stop_notify(OTA_NOTIFY_UUID)
            self._started = False

    def _on_notify(self, _sender, data: bytearray):
        self._log(f"  ota <- {bytes(data).hex()}")
        self._replies.put_nowait(bytes(data))

    async def _exchange(self, payload: bytes, timeout: float = 10.0) -> bytes:
        """Write a command (acknowledged) and wait for its notification."""
        while not self._replies.empty():
            self._replies.get_nowait()
        self._log(f"  ota -> {payload.hex()}")
        await self._c.write_gatt_char(OTA_WRITE_UUID, payload, response=True)
        try:
            return await asyncio.wait_for(self._replies.get(), timeout)
        except asyncio.TimeoutError as e:
            raise OtaError(f"no OTA reply to {payload[:1].hex()} within {timeout}s") from e

    # -- read-only ----------------------------------------------------------

    async def probe(self) -> OtaInfo:
        """Query the device. Issues only NVDS_TYPE and GET_STR_BASE, both of
        which read state -- nothing is erased or written."""
        await self.start()
        nvds = await self._exchange(build_command(CMD_NVDS_TYPE))
        flags = reply_u8(nvds)
        large = bool(flags & 0x10)
        base = await self._exchange(build_command(CMD_GET_STR_BASE))
        return OtaInfo(
            nvds_type=flags,
            large_mtu=large,
            base_addr=reply_u32(base),
            mtu=getattr(self._c, "mtu_size", 23),
        )

    def plan(self, fw: Firmware, info: OtaInfo) -> tuple[int, int]:
        """Work out (target address, chunk size) and refuse anything unsafe.

        Raises before any destructive command is issued.
        """
        if info.large_mtu:
            # On this variant GET_STR_BASE returns the *destination*, not the
            # running image -- the device picks the spare bank itself. Verified
            # on a Zoom75 module: nvds 0x11, base 0x32000, well clear of the
            # bank the 145 KB shipped image boots from.
            target = info.base_addr
            if target == 0:
                raise OtaAborted(
                    "refusing: device nominated address 0x0, which is the boot "
                    "bank. That is not a destination this variant should return."
                )
        else:
            target = ADDR_BANK_B if info.base_addr == ADDR_BANK_A else ADDR_BANK_A
            if target == info.base_addr:
                raise OtaAborted(
                    f"refusing: computed target 0x{target:x} is the bank the device "
                    "is currently running from"
                )
            gap = abs(ADDR_BANK_B - ADDR_BANK_A)
            if fw.size > gap:
                raise OtaAborted(
                    f"refusing: image is {fw.size} bytes but the fixed banks are only "
                    f"0x{gap:x} ({gap}) apart, so the write would run into the other "
                    "bank. This image does not match this device's flash layout."
                )
        if target % PAGE_SIZE:
            raise OtaAborted(f"refusing: target 0x{target:x} is not page-aligned")
        end = target + fw.pages * PAGE_SIZE
        if end > MAX_FLASH:
            raise OtaAborted(
                f"refusing: writing {fw.pages} pages at 0x{target:x} would reach "
                f"0x{end:x}, past the 0x{MAX_FLASH:x} flash limit"
            )

        chunk = info.mtu - 3 - WRITE_HEADER_LEN
        if chunk < 16:
            raise OtaAborted(f"refusing: negotiated MTU {info.mtu} leaves only {chunk} bytes per chunk")
        return target, chunk

    # -- destructive --------------------------------------------------------

    async def flash(
        self,
        fw: Firmware,
        info: OtaInfo,
        *,
        i_understand_this_rewrites_flash: bool = False,
        progress=None,
        step_timeout: float = 15.0,
    ):
        """Erase the spare bank, write the image, then commit with REBOOT.

        The keyword-only confirmation flag has a deliberately awkward name: no
        caller reaches this by accident.
        """
        if not i_understand_this_rewrites_flash:
            raise OtaAborted("flash() requires explicit confirmation")

        target, chunk = self.plan(fw, info)
        await self.start()

        # --- erase the spare bank -----------------------------------------
        for i in range(fw.pages):
            if not self._c.is_connected:
                raise OtaError("disconnected during erase")
            addr = target + i * PAGE_SIZE
            await self._exchange(build_command(CMD_PAGE_ERASE, addr=addr), step_timeout)
            if progress:
                progress("erase", i + 1, fw.pages)

        # --- write --------------------------------------------------------
        addr = target
        last_len = 0
        sent = 0
        total_chunks = -(-fw.size // chunk)
        for i in range(total_chunks):
            if not self._c.is_connected:
                raise OtaError("disconnected during write")
            piece = fw.data[i * chunk : (i + 1) * chunk]
            await self._exchange(
                build_command(CMD_WRITE_DATA, addr=addr, data=piece, data_len=len(piece)),
                step_timeout,
            )
            addr += len(piece)
            last_len = len(piece)
            sent += 1
            if progress:
                progress("write", sent, total_chunks)

        # --- confirm the device landed where we think ---------------------
        expected = addr - last_len
        ack = await self._exchange(build_command(CMD_GET_STR_BASE), step_timeout)
        if reply_u32(ack) != expected:
            raise OtaError(
                f"device reports last write at 0x{reply_u32(ack):x}, expected "
                f"0x{expected:x}. NOT committing -- the running firmware is untouched."
            )

        # --- commit -------------------------------------------------------
        # Only now does the bootloader consider the new image. It validates the
        # CRC itself and keeps running the old bank if it does not match.
        self._log("  ota -> reboot/commit")
        await self._c.write_gatt_char(
            OTA_WRITE_UUID, build_reboot(fw.crc, fw.size), response=True
        )
        if progress:
            progress("commit", 1, 1)
