"""Check the encoders against literals lifted from the decompiled vendor app."""
import struct
from zoom75 import protocol as p
from zoom75 import image as im
from zoom75 import hid as _hid

fails = []
def eq(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      got  {got}")
        print(f"      want {want}")
        fails.append(name)

# BleOperateManager.getKeyBoardStatus: getFullPackage(new byte[]{0,19,0})
eq("status frame", p.cmd_status().hex(), "8800000000000313001300")

# BleOperateManager.setLocalKeyBoardDial: hard-coded literal
eq("local dial", p.cmd_local_dial().hex(), "880000000000060009070000fffe")

# KeyBoardConstant.getDialStartArray
eq("dial begin", p.cmd_dial_begin().hex(),
   "88000000" + "00000a" + "0b" + "0803" + "00ffffff" + "00ffffff")

# KeyBoardConstant.getDialByte, type != 2 (still image)
size = p.FRAME_BYTES
body = bytes([1, 0, 11]) + struct.pack(">I", 65533) + struct.pack(">I", size) + b"\xff\xff\xff"
want = "88" + "0000" + struct.pack(">I", 16).hex() + f"{p.xor(b'\x09\x03'+body):02x}" + "0903" + body.hex()
eq("dial info (still)", p.cmd_dial_info(size, False).hex(), want)

# CustomDialActivity.keyValue -> the 25-byte bulk prefix
eq("flash header", p.flash_header(size).hex(),
   "880000" + struct.pack(">I", size + 17).hex() + "00" + "0805010009"
   + "00ffffff" + "00ffffff" + "0202ffff")
eq("flash header length", len(p.flash_header(size)), 25)

# BleConstant.syncTime()
t = p.cmd_sync_time(2026, 9, 2, 13, 45, 30)
eq("sync time", t.hex(), "88" + "0000" + "00000009"
   + f"{p.xor(bytes([4,1,0x07,0xea,9,2,13,45,30])):02x}"
   + "0401" + "07ea" + "0902" + "0d" + "2d" + "1e")

# --- bulk splitter round-trips and matches the app's chunk geometry --------
data = bytes(range(256)) * 500  # 128000 bytes
blocks = p.build_flash_blocks(data)
eq("block count", len(blocks), -(-len(data) // 4096))
eq("first block chunks", len(blocks[0]), 17)
eq("first chunk size", len(blocks[0][0]), 244)
eq("first chunk starts with header", blocks[0][0][:25], p.flash_header(len(data)))

rebuilt = bytearray()
for bi, block in enumerate(blocks):
    for ci, chunk in enumerate(block):
        body, chk = chunk[:-1], chunk[-1]
        assert chk == p.xor(body), f"bad xor at {bi}/{ci}"
        rebuilt += body[25:] if (bi == 0 and ci == 0) else body
eq("bulk round-trip", bytes(rebuilt), data)
eq("every chunk <= 244", max(len(c) for b in blocks for c in b), 244)

# exact-multiple and tiny inputs
for n in (243, 244, 4096, 4097, 110080, 300):
    bl = p.build_flash_blocks(bytes(n))
    rb = bytearray()
    for bi, block in enumerate(bl):
        for ci, chunk in enumerate(block):
            body = chunk[:-1]
            rb += body[25:] if (bi == 0 and ci == 0) else body
    eq(f"round-trip n={n}", len(rb), n)

# --- image ----------------------------------------------------------------
eq("frame bytes", p.FRAME_BYTES, 320 * 172 * 2)
from PIL import Image
red = Image.new("RGB", (320, 172), (255, 0, 0))
eq("rgb565 red", im.to_rgb565(red)[:2].hex(), "f800")
green = Image.new("RGB", (320, 172), (0, 255, 0))
eq("rgb565 green", im.to_rgb565(green)[:2].hex(), "07e0")
blue = Image.new("RGB", (320, 172), (0, 0, 255))
eq("rgb565 blue", im.to_rgb565(blue)[:2].hex(), "001f")
eq("rgb565 length", len(im.to_rgb565(red)), p.FRAME_BYTES)

# animation container layout
frames = [im.to_rgb565(red), im.to_rgb565(green)]
blob = im.build_animation(frames, speed=5)
eq("dlx magic", blob[0:4], b"\x00DLX")
eq("dlx frame count", blob[12], 2)
eq("dlx section table", struct.unpack("<4I", blob[352:368]),
   (368, 368 + 84, 368 + 84 + 8, 368 + 84 + 8 + 2 * p.FRAME_BYTES))
eq("dlx geometry (rec 1)", struct.unpack(">HH", blob[368 + 10 : 368 + 14]), (320, 172))
eq("dlx speed field", blob[368 + 28 + 4], 6)   # 11 - 5
eq("dlx frames field", blob[368 + 28 + 7], 2)
eq("dlx total size", len(blob), 368 + 84 + 8 + 2 * p.FRAME_BYTES + 4)
eq("dlx trailer", blob[-4:].hex(), "fcff0000")

# reply parsing, using a real 11-byte shape from the app's callbacks
raw = p.frame(b"\x09\x04\x04")
r = p.parse(raw)
eq("parse len", len(raw), 11)
eq("parse opcode", r.opcode, b"\x09\x04")
eq("parse status", r.status, 4)
eq("parse rejects junk", p.parse(b"\x01\x02"), None)

# --- commands added in the full-coverage pass ------------------------------
import datetime as _dt

# Notifications use 4-byte TLV lengths (Java toByteArray(int)); notes/alarms use 2.
n = p.cmd_notify(13, "CI", "ok")
eq("notify opcode", n[8:10], b"\x05\x01")
eq("notify prefix", n[10:14].hex(), "0100010d")
eq("notify title tlv", n[14:19].hex(), "0200000004")
eq("notify title utf16", n[19:23].decode("utf-16-be"), "CI")
eq("notify body tlv", n[23:28].hex(), "0300000004")

first, second = p.cmd_note("t", "b", _dt.datetime(2026, 9, 2, 7, 0, 0))
eq("note opcode", first[8:10], b"\x04\x0a")
eq("note stamp tlv (2-byte len)", first[10:13].hex(), "010008")
eq("note year BE", first[13:15].hex(), "07ea")
eq("note weekday sun=0", first[20], 3)          # 2026-09-02 is a Wednesday
eq("note title tlv", first[21:24].hex(), "020002")
eq("note body frame", second[10:13].hex(), "030002")
eq("note delete is BE", p.cmd_note_delete(0x01020304)[10:14].hex(), "01020304")

a = p.cmd_alarm_set([(True, 0, 7, 30)])
eq("alarm opcode", a[8:10], b"\x04\x0c")
eq("alarm len field", a[11:13].hex(), "0007")
eq("alarm body", a[13:20].hex(), "05050002" + "00" + "07" + "1e")
try:
    p.cmd_alarm_set([(True, 0, 0, 0)] * 11)
    print("FAIL  alarm index cap"); fails.append("alarm index cap")
except ValueError:
    print("PASS  alarm index cap")

# privileged commands carry the SDK magic tail
eq("power off", p.cmd_power_off(1)[8:].hex(), "056001a1fe7469")
eq("test mode", p.cmd_test_mode()[8:].hex(), "05fe54a1fe7469")
eq("find device", p.cmd_find_device()[8:].hex(), "0019")
eq("backlight", p.cmd_backlight(3, 10)[8:].hex(), "0236030a")
eq("unbind", p.cmd_unbind()[8:].hex(), "0101")

# Device epoch: verified against a real note written at 08:28:38 local.
eq("device epoch", p.DEVICE_EPOCH, 946684800)
eq("device time decode", str(p.device_time(841652918)), "2026-09-02 08:28:38")
eq("device time round-trip", p.to_device_time(p.device_time(841652918)), 841652918)
eq("vendor epoch is 8h out", p.DEVICE_EPOCH - p.VENDOR_EPOCH, 28800)

# --- raw-HID reports (MeletrixID protocol) ---------------------------------
from zoom75 import hid as _hid

eq("crc16 ccitt-false check value", _hid.crc16_ccitt(b"123456789"), 0x29B1)

r = _hid.report_cpu_temp(55)
eq("hid report size", len(r), 32)
eq("hid magic", r[0], 0x1C)
eq("hid outer type sensor", r[1], 0)
eq("hid outer len sensor", r[5], 8)
eq("hid marker", r[8], 0xA5)
eq("hid cpu field", r[9], 0x37)
eq("hid data len BE", r[10:12].hex(), "0003")
eq("hid cpu value", r[14], 55)
eq("hid crc placement", _hid.crc16_ccitt(r[:6] + b"\x00\x00" + r[8:]),
   (r[7] << 8) | r[6])

eq("hid gpu field", _hid.report_gpu_temp(40)[9], 0x38)
eq("hid fan field", _hid.report_fan_rpm(1058)[9], 0x39)
eq("hid fan value BE", _hid.report_fan_rpm(1058)[13:15].hex(), "0422")
eq("hid net field", _hid.report_net_speed(1234)[9], 0x3D)
eq("hid net value BE", _hid.report_net_speed(1234)[13:17].hex(), "000004d2")

t = _hid.report_time(_dt.datetime(2026, 9, 2, 9, 15, 30))
eq("hid time outer type", t[1], 3)
eq("hid time outer len", t[5], 15)
eq("hid time data len", t[10:12].hex(), "000a")
eq("hid time year BE", t[14:16].hex(), "07ea")
eq("hid time weekday sun=0", t[21], 3)

w = _hid.report_weather(6, 18)
eq("hid weather field", w[9], 0x3B)
eq("hid weather outer len", w[5], 9)
eq("hid weather icon", w[13], 6)
eq("hid weather temp", w[14:16].hex(), "0012")
eq("hid weather literal checksum", w[16], 0xFF)
eq("hid negative temp sign bit", _hid.encode_temp(-5), 0x8005)
eq("hid encode positive", _hid.encode_temp(18), 18)
eq("wmo rain -> icon 6", _hid.wmo_to_icon(61), 6)
eq("wmo clear night -> icon 4", _hid.wmo_to_icon(0, is_night=True), 4)
eq("wmo snow -> icon 7", _hid.wmo_to_icon(73), 7)

# --- dashboard header: address, with the interface name as fallback --------
from zoom75 import dashboard as _dash
from zoom75.stats import Metric as _Metric, Sample as _Sample, iface_ip as _iface_ip
import time as _time

def _mk(ip, iface):
    return _Sample(host="host", when=_time.localtime(),
                   cpu=_Metric("CPU", 10.0, "10%", "50"), mem=_Metric("MEM", 20.0, "2G", "2/8G"),
                   gpu=None, net_iface=iface, net_ip=ip, rx_bps=1.0, tx_bps=2.0)

eq("renders with an address", _dash.render(_mk("192.0.2.10", "eth0")).size, (320, 172))
eq("renders without one", _dash.render(_mk(None, "eth0")).size, (320, 172))
eq("renders with neither", _dash.render(_mk(None, "")).size, (320, 172))
eq("loopback address resolves", _iface_ip("lo"), "127.0.0.1")
eq("unknown interface is None", _iface_ip("definitely-not-a-nic"), None)
eq("empty interface is None", _iface_ip(""), None)

# --- the undocumented 0x54 ("T") BLE command space --------------------------
eq("T frame is bare", p.cmd_t_get_clock().hex(), "540a")
eq("T magic", p.T_MAGIC, 0x54)
# Real reply captured from hardware at 2026-09-02 09:31:01.
eq("T clock parse", str(p.parse_t_clock(bytes.fromhex("540a07ea0902091f01"))),
   "2026-09-02 09:31:01")
eq("T clock seconds advance",
   p.parse_t_clock(bytes.fromhex("540a07e70303120312")).second, 18)
eq("T clock rejects 0x88 frames", p.parse_t_clock(b"\x88\x00\x00"), None)
eq("T clock rejects short", p.parse_t_clock(bytes.fromhex("540a07ea09")), None)
eq("T clock rejects bad date", p.parse_t_clock(bytes.fromhex("540affff6363636363")), None)
eq("reset sub-command is flagged", p.T_RESET_DANGEROUS, 0x01)

# --- device profiles -------------------------------------------------------
from zoom75 import devices as _dev

eq("zoom75 geometry", (_dev.ZOOM75.width, _dev.ZOOM75.height), (320, 172))
eq("zoom75 frame bytes", _dev.ZOOM75.frame_bytes, 110080)
eq("second-gen geometry", (_dev.SECOND_GEN.width, _dev.SECOND_GEN.height), (390, 390))
eq("zoom75 is the default", _dev.DEFAULT.name, "zoom75")
eq("only zoom75 claims verified", [d.name for d in _dev.ALL if d.verified], ["zoom75"])
eq("usb lookup finds zoom75", _dev.by_usb(0x1EA7, 0xCED3).name, "zoom75")
eq("usb lookup finds tiga", _dev.by_usb(0x1EA7, 0xCEDD).name, "tiga")
eq("usb lookup misses unknown", _dev.by_usb(0x1234, 0x5678), None)
eq("bundled models", sorted(d.name for d in _dev.ALL if d.hid_style == "bundled"),
   ["dyna", "hetix", "tiga"])
try:
    _dev.by_name("nope"); print("FAIL  unknown device rejected"); fails.append("unknown device")
except ValueError:
    print("PASS  unknown device rejected")

# geometry actually drives the encoders
from PIL import Image as _Im
for _d in (_dev.ZOOM75, _dev.SECOND_GEN):
    _img = _Im.new("RGB", (700, 500), (1, 2, 3))
    eq(f"{_d.name} fit size", im.fit(_img, "cover", _d).size, (_d.width, _d.height))
    eq(f"{_d.name} rgb565 length", len(im.to_rgb565(im.fit(_img, "cover", _d), _d)), _d.frame_bytes)
_a = im.build_animation([bytes(_dev.SECOND_GEN.frame_bytes)] * 2, device=_dev.SECOND_GEN)
eq("second-gen animation geometry", struct.unpack(">HH", _a[368 + 10:368 + 14]), (390, 390))

# --- bundled raw-HID reports (Tiga / Dyna / Hetix) -------------------------
_b = _hid.report_bundle(55, 40, 35, 1200, 4096)
eq("bundle field", _b[9], 0xFF)
eq("bundle outer", (_b[1], _b[5]), (2, 16))
eq("bundle data len", _b[10:12].hex(), "000b")
eq("bundle cpu/gpu/ssd", (_b[14], _b[16], _b[18]), (55, 40, 35))
eq("bundle fan wraps to one byte", _b[20], 1200 & 0xFF)
eq("bundle net BE", _b[21:23].hex(), "1000")
eq("bundle literal checksum", _b[23], 0xFF)
eq("bundle crc", _hid.crc16_ccitt(_b[:6] + b"\x00\x00" + _b[8:]), (_b[7] << 8) | _b[6])

_bw = _hid.report_bundle_weather(6, 17, 21, -3)
eq("bundle weather field", _bw[9], 0xFE)
eq("bundle weather outer", (_bw[1], _bw[5]), (2, 13))
eq("bundle weather temps", (_bw[14:16].hex(), _bw[16:18].hex(), _bw[18:20].hex()),
   ("0011", "0015", "8003"))

# --- extras ----------------------------------------------------------------
from zoom75 import extras as _ex
_track = {"player": "x", "status": "Playing", "title": "T" * 60,
          "artist": "A" * 40, "album": "B" * 40, "art": None}
eq("now-playing renders", _ex.render_now_playing(_track).size, (320, 172))
eq("now-playing on second-gen", _ex.render_now_playing(_track, _dev.SECOND_GEN).size, (390, 390))
eq("now-playing frame length", len(_ex.now_playing_frame(_track)), 110080)
eq("art url must be local", _ex._local_art("https://example.com/a.png"), None)
eq("empty art url", _ex._local_art(""), None)
eq("first of a list", _ex._first(["a", "b"]), "a")
eq("first of empty", _ex._first([]), "")

print()
print(f"{'ALL PASS' if not fails else str(len(fails)) + ' FAILURES: ' + ', '.join(fails)}")
raise SystemExit(1 if fails else 0)
