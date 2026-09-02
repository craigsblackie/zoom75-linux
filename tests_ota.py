"""Checks for the OTA encoders, against the decompiled vendor implementation
and the real shipped firmware image."""
import glob
import struct
from zoom75 import ota

fails = []
def eq(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      got  {got!r}\n      want {want!r}")
        fails.append(name)

def raises(name, exc, fn):
    try:
        fn()
    except exc:
        print(f"PASS  {name}")
        return
    except Exception as e:
        print(f"FAIL  {name}: raised {type(e).__name__} not {exc.__name__}")
    else:
        print(f"FAIL  {name}: did not raise")
    fails.append(name)

# --- WriterOperation.cmd_write_op -----------------------------------------
# PAGE_ERASE is 7 bytes: cmd, hdr, 0, addr(LE32). Everything else is 9,
# with a trailing LE16 data length.
eq("page erase encoding", ota.build_command(ota.CMD_PAGE_ERASE, addr=0x14000).hex(),
   "0307" + "00" + struct.pack("<I", 0x14000).hex())
eq("page erase length", len(ota.build_command(ota.CMD_PAGE_ERASE, addr=0)), 7)
eq("nvds type encoding", ota.build_command(ota.CMD_NVDS_TYPE).hex(), "000300" + "00000000" + "0000")
eq("get str base encoding", ota.build_command(ota.CMD_GET_STR_BASE).hex(), "010300" + "00000000" + "0000")
eq("query length", len(ota.build_command(ota.CMD_GET_STR_BASE)), 9)

payload = bytes(range(64))
w = ota.build_command(ota.CMD_WRITE_DATA, addr=0x28000, data=payload, data_len=len(payload))
eq("write hdr", w[:9].hex(), "0509" + "00" + struct.pack("<I", 0x28000).hex() + struct.pack("<H", 64).hex())
eq("write body", w[9:], payload)
eq("write total len", len(w), 9 + 64)
eq("write header size constant", ota.WRITE_HEADER_LEN, 9)

# byte 2 is always zero -- the vendor masks the header field to 8 bits first
eq("hdr byte2 always zero", ota.cmd_write_op(ota.CMD_WRITE_DATA, 0x109, 0, 0)[2], 0)

# --- send_data_long (the commit) ------------------------------------------
eq("reboot encoding", ota.build_reboot(0xF7DB5F5C, 148988).hex(),
   "090a00" + struct.pack("<I", 148988).hex() + struct.pack("<I", 0xF7DB5F5C).hex())
eq("reboot length", len(ota.build_reboot(0, 0)), 11)

# --- reply decoding --------------------------------------------------------
r = bytes([1, 2, 3, 4]) + struct.pack("<I", 0x00014000)
eq("reply_u32", ota.reply_u32(r), 0x14000)
eq("reply_u8", ota.reply_u8(r), 0x00)
raises("reply_u32 rejects short", ota.OtaError, lambda: ota.reply_u32(b"\x01\x02"))

# --- CRC -------------------------------------------------------------------
# Value computed with Java's signed `crc / 256`, not `crc >>> 8`.
img = sorted(glob.glob("firmware/*.bin"))
if img:
    fw = ota.Firmware.load(img[0])
    eq("firmware size", fw.size, 148988)
    eq("firmware version code", fw.version_code, 335)
    eq("firmware version string", fw.version, "V0.1.79")
    eq("firmware crc", f"0x{fw.crc:08x}", "0xf7db5f5c")
    eq("firmware pages", fw.pages, 37)
else:
    print("SKIP  firmware image checks -- run `./z75 fw download` first")

eq("crc skips first 256 bytes", ota.firmware_crc(b"\xa5" * 256), 0)
eq("crc table size", len(ota._CRC_TABLE), 256)
eq("crc table is reflected crc32", ota._CRC_TABLE[1], 0x77073096)

raises("rejects tiny file", ota.OtaError, lambda: ota.Firmware.load("/etc/hostname"))

# --- plan(): the safety rails ---------------------------------------------
class FW:
    def __init__(self, size):
        self.size = size
        self.pages = -(-size // ota.PAGE_SIZE)

def info(base, large, mtu=247):
    return ota.OtaInfo(nvds_type=0x10 if large else 0, large_mtu=large, base_addr=base, mtu=mtu)

c = ota.OtaClient.__new__(ota.OtaClient)

# fixed-bank part: always targets the bank that is NOT running
eq("8010 running A -> target B", c.plan(FW(4096), info(ota.ADDR_BANK_A, False))[0], ota.ADDR_BANK_B)
eq("8010 running B -> target A", c.plan(FW(4096), info(ota.ADDR_BANK_B, False))[0], ota.ADDR_BANK_A)
# an image larger than the bank gap would spill into the live bank
raises("8010 refuses oversize image", ota.OtaAborted,
       lambda: c.plan(FW(148988), info(ota.ADDR_BANK_A, False)))
# device-directed part takes the address the device gives
eq("8010H uses device address", c.plan(FW(148988), info(0x28000, True))[0], 0x28000)
raises("refuses unaligned target", ota.OtaAborted,
       lambda: c.plan(FW(4096), info(0x28001, True)))
raises("refuses tiny MTU", ota.OtaAborted,
       lambda: c.plan(FW(4096), info(0x28000, True, mtu=23)))
eq("chunk size from mtu", c.plan(FW(4096), info(0x28000, True, mtu=247))[1], 247 - 3 - 9)
eq("chunk size at mtu 512", c.plan(FW(4096), info(0x28000, True, mtu=512))[1], 512 - 3 - 9)

# guards added after probing real hardware (nvds 0x11, base 0x32000)
eq("8010H real device values", c.plan(FW(148988), info(0x32000, True))[0], 0x32000)
raises("8010H refuses address 0 (boot bank)", ota.OtaAborted,
       lambda: c.plan(FW(4096), info(0x0, True)))
raises("refuses write past end of flash", ota.OtaAborted,
       lambda: c.plan(FW(148988), info(ota.MAX_FLASH - 4096, True)))
eq("fits just under the flash limit",
   c.plan(FW(4096), info(ota.MAX_FLASH - 4096, True))[0], ota.MAX_FLASH - 4096)

# flash() cannot be reached without the explicit acknowledgement
import asyncio
raises("flash needs confirmation", ota.OtaAborted,
       lambda: asyncio.run(c.flash(FW(4096), info(0x28000, True))))

# chip erase is defined but must never be emitted by any builder
eq("chip erase unused", ota.cmd_operation(ota.CMD_CHIP_ERASE, 0, 0), None)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {', '.join(fails)}")
raise SystemExit(1 if fails else 0)
