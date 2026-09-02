# Zoom75 screen module — BLE protocol

Reverse-engineered from the vendor Android app **PocketWuque**
(`com.app.smartkeyboard` v1.1.3), specifically
`com.blala.blalable.{Utils,BleConstant,BleManager,BleOperateManager}`,
`com.blala.blalable.keyboard.{KeyBoardConstant,FlashCallback}` and
`com.app.smartkeyboard.CustomDialActivity`. Everything below was then confirmed
against real hardware (Zoom75, firmware reporting `c0030001…`).

## Topology

The screen is its **own BLE peripheral**, independent of the keyboard's HID
interfaces. It advertises as `Zoom75 Screen` with service UUID
`1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa` even while the keyboard itself is
connected over USB or 2.4 GHz.

Its advertised address is a **non-resolvable private address** (`02:…`), so
match on the service UUID or name rather than caching a MAC. It stops
advertising while connected — a leaked connection from a killed process looks
exactly like "device not found".

## GATT

Service `1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa`

| Characteristic | Properties | Role |
|---|---|---|
| `1f400001-aab4-14a3-f1ba-f61f35cddbaa` | write, write-nr | commands |
| `1f400002-aab4-14a3-f1ba-f61f35cddbaa` | notify | replies |
| `1f400003-aab4-14a3-f1ba-f61f35cddbaa` | write, write-nr | bulk image data |
| `1f400004-aab4-14a3-f1ba-f61f35cddbaa` | notify | bulk status |

A second service `02f00000-…-fe00` exists (characteristics `ff00`–`ff03`,
one of which reads the ASCII string `ntf_enable`). It appears to be stock
vendor-SDK plumbing and is not needed.

Standard battery service `0x180F` / `0x2A19` is present and readable.

**MTU must be 247.** The bulk chunks are 244 bytes and an ATT write carries
MTU−3. The device negotiates 247 on its own, but BlueZ only *reports* the real
MTU once a write socket is acquired — with bleak, call
`client._backend._acquire_mtu()` after connecting or you will see the 23-byte
default and undersized writes.

## Framing

Both directions use the same envelope:

```
88 00 00 LL LL LL LL XX  <payload>
│  │     │            │
│  │     │            └─ XOR of every payload byte
│  │     └────────────── payload length, big-endian u32
│  └──────────────────── two reserved zero bytes
└─────────────────────── product id (0x88)
```

The payload begins with a 2-byte opcode. **The device does not validate the XOR
byte** — the vendor app hard-codes it to `00` in the bulk header and in the
`cmd_local_dial` literal, and the screen accepts both.

Replies use `request_opcode + 1`: `0x0013` → `0x0014`, `0x0903` → `0x0904`.

## Opcodes

Implemented and confirmed:

| Opcode | Direction | Meaning |
|---|---|---|
| `0x0001` / `0x0002` | req / rep | firmware + device info (reply embeds the BLE MAC) |
| `0x0013` / `0x0014` | req / rep | device status; last payload byte is the state |
| `0x0401` | req | set clock: `YY YY MM DD hh mm ss` (year big-endian) |
| `0x011C` | req | screen mode, args `01 00 01 <1\|2>` |
| `0x040D` | req | select built-in screen style / clock face |
| `0x0903` / `0x0904` | req / rep | announce an upload |
| `0x0803` / `0x0804` | req / rep | begin the flash write |
| `0x0805` / `0x0806` | bulk / rep | image data and its per-block acknowledgement |
| `0x0907` | req | revert to the built-in dial |
| `0x0702` | rep | generic ack, echoes `01 <requested opcode>` |
| `0x030A` | rep | image committed; payload echoes the type tag |

Present in the app but not implemented here: weather (`0x040C`, `0x0412`),
alarms (`0x0025`/`0x0026`, `0x0435`), notebook entries (`0x040A`,
`0x0015`/`0x0016`, `0x040B`), phone notifications (`0x0501`), usage stats
(`0x0023`/`0x0024`).

## Panel format

320 × 172, **RGB565 big-endian**, row-major, top-left first — 110,080 bytes
per frame.

```
value = (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)     # emitted as >H
```

## Upload state machine

1. **Announce** `0x0903`.
   - still: `01 00 0B <uiFeature:u32be = 65533> <size:u32be> FF FF FF`
   - animated: `04 00 08 00 00 FF FC <size:u32be> 05 00 14` + 20 × `FF`

   Reply `0x0904`: status `1` rejected, `5` busy; `2` and `4` both proceed.
   The device does **not** validate the declared size here — an over-large
   image is only rejected later, as a GATT error mid-write.

2. **Begin** `0x0803` with `00 FF FF FF 00 FF FF FF`. Reply `0x0804` status `2`.

3. **Wait to arm.** Poll `0x0013` until the status byte reads `3`.

4. **Stream** to `1f400003`. The image is cut into 4096-byte blocks, each block
   into 243-byte chunks, and every chunk gets a trailing XOR byte — 244 bytes
   on the wire. The very first chunk is prefixed with a 25-byte header, which
   displaces 25 bytes of payload, so every later chunk offset *within the first
   block* is shifted back by 25:

   ```
   88 00 00 <size+17 : u32be> 00 08 05 01 00 09
   00 FF FF FF   00 FF FF FF   02 02 FF FF
   ```

   The screen acknowledges **each 4096-byte block** on `0x0806` with status
   `5`, then the whole transfer with status `2`, then sends `0x030A`. Status
   `1` or `6` means it aborted.

**Use acknowledged writes.** With write-without-response the panel silently
drops packets and renders a torn image; this was the single biggest gotcha.
Pacing each block against its own `0x0806` ack is more reliable than a fixed
delay.

Throughput tops out around **3.3 KB/s** (~35 s for a still frame). The limit is
the panel, not the link: it needs roughly 70 ms to commit each 243-byte chunk,
which is why unacknowledged writes corrupt the image instead of speeding it up.

If a transfer is interrupted, the next one can fail with a GATT
`UNLIKELY_ERROR` mid-write. Disconnect cleanly and re-run; the fresh
`0x0903`/`0x0803` handshake clears the state.

## Animated container ("DLX")

| Offset | Size | Contents |
|---|---|---|
| 0 | 28 | `00 'D' 'L' 'X' FC FF 00 00 60 01 03 00 <nframes> 00 00 01` + 12 × `00` |
| 28 | 324 | filler, all `FF` |
| 352 | 16 | four little-endian u32 section offsets: B, C, D, end |
| 368 | 84 | section B — three 28-byte descriptors |
| … | 4·n | section C — per-frame little-endian offsets into D |
| … | 110080·n | section D — the RGB565 frames |
| … | 4 | trailer `FC FF 00 00` |

Each 28-byte descriptor carries the panel geometry at bytes 10–13 as two
big-endian u16 (`0140 00AC` = 320 × 172). The second descriptor (`20 03`)
holds the frame delay at byte 4 as `11 − speed` (speed 1–10, vendor default 5)
and the frame count at byte 7.

The vendor app thins animations to **9 frames** for this device before
uploading.

## Known vendor bug

`FlashCallback.getDialContent` computes its chunk count as
`ceil(len / 243)` without accounting for the 25-byte header, so any payload
whose first block ends within 25 bytes of a chunk boundary is silently
truncated. Real frames are ≥ 4096 bytes and unaffected; this implementation
computes `ceil((len + 25) / 243)` for the first block instead.


## Firmware update (OTA)

The screen exposes a **second service** which is a **FreqChip** flash
programmer. (The vendor code writes an NVDS dump to a `Freqchip/` directory and
the command set matches that SDK.)

| UUID | Role |
|---|---|
| `02f00000-0000-0000-0000-00000000fe00` | OTA service |
| `…ff01` | commands (write) |
| `…ff02` | replies (notify) — reads the ASCII string `ntf_enable` |

### Commands

| Code | Name | Notes |
|---|---|---|
| 0 | `NVDS_TYPE` | read-only; bit `0x10` of reply byte 4 selects the variant |
| 1 | `GET_STR_BASE` | read-only; returns a flash base address |
| 3 | `PAGE_ERASE` | erases one 4096-byte page |
| 4 | `CHIP_ERASE` | **never issued** — would destroy both banks |
| 5 | `WRITE_DATA` | address + payload |
| 6 / 8 | `READ_DATA` / `READ_MEM` | unused by the app |
| 7 | `WRITE_MEM` | unused by the app |
| 9 | `REBOOT` | commit: carries length + CRC |

Encoding (`WriterOperation.cmd_write_op`) — all little-endian:

```
PAGE_ERASE   03 07 00 <addr:u32>                       (7 bytes)
queries      <cmd> 03 00 00000000 0000                 (9 bytes)
WRITE_DATA   05 09 00 <addr:u32> <len:u16> <payload>    (9 + n)
REBOOT       09 0A 00 <length:u32> <crc:u32>            (11 bytes)
```

Byte 2 is always zero: the vendor masks the header field to 8 bits *before*
shifting it right by 8. Replies carry a little-endian u32 at offset 4. All
writes are acknowledged (`WRITE_TYPE_DEFAULT`).

## Complete opcode inventory

Every command reachable in the vendor app, with what it actually does on
generation-1 hardware. "ack only" means the device returns the generic `0x0702`
acknowledgement echoing the opcode — so the opcode is recognised — but no data
frame and no visible effect.

| Opcode | Command | Status here |
|---|---|---|
| `0x0001` | device version / product / MAC | **verified** |
| `0x0013` | device status | **verified** |
| `0x0015` | stored-note timestamp | **verified** — returns the note's stamp |
| `0x0017` | system data | ack only |
| `0x0019` | find device | ack only, no observable effect |
| `0x0023` | per-day usage stats | ack only, no data |
| `0x0025` | read alarms | ack only, no data |
| `0x0101` | unbind ("recycle device") | 2nd-gen only; implemented, never fired |
| `0x011B` | select built-in dial by index | SDK only; implemented, never fired |
| `0x011C` | screen mode | ack |
| `0x0161` | set current face (privileged) | SDK only; never fired |
| `0x0236` | backlight level + timeout | SDK only; implemented, never fired |
| `0x0401` | set clock | **verified** — must be followed by `0x0402` |
| `0x0402` | device info | **verified** |
| `0x0407` | weather | 2nd-gen only; implemented, unverified |
| `0x040A` | write note | **verified** — the note is viewable on the device |
| `0x040B` | delete note | implemented |
| `0x040C` | set alarms | 2nd-gen only; implemented, unverified |
| `0x040D` | screen / clock style | 2nd-gen only; acked, no effect here |
| `0x0501` | push notification | **acked but nothing renders** on gen-1 |
| `0x0560` | power off (privileged) | SDK only; never fired |
| `0x05FE` | test mode (privileged) | SDK only; never fired |
| `0x0702` | generic ack (`01` + request opcode) | **verified** |
| `0x0803`/`0x0805`/`0x0903`/`0x0907` | dial upload and restore | **verified** |

### A second BLE protocol: 0x54 ("T")

The BLE receive handler accepts **two** framings, not one. Disassembly of the
screen module's own firmware (load base `0x10000000`) shows the dispatcher at
`0x1001e4a4`:

```
1001e4c2  ldrb  r1, [r4]      ; first byte received
1001e4c4  cmp   r1, #0x54     ; 'T' -> a second command space
1001e4c6  beq   0x1001e4da
1001e4c8  cmp   r7, #8        ; the 0x88 path needs >= 8 bytes
1001e4ca  blo   0x1001e5ae
1001e4cc  cmp   r1, #0x88     ; the space the phone app uses
1001e4ce  bne   0x1001e5ae
```

The `0x54` space is dispatched on the second byte across 19 sub-commands
(`0x01`-`0x0A`, `0x40`, `0x44`-`0x46`, `0x50`, `0x93`, `0x97`, `0xF0`, `0xF1`)
and the vendor phone app never uses any of it. Frames are bare — magic,
sub-command, arguments — with no length field and no checksum. Replies echo
`54 <sub>` and are **not** `0x88`-framed.

**`0x0A` reads back the real-time clock**, which the `0x88` space cannot do:

```
-> 54 0a
<- 54 0a 07ea 09 02 09 1f 01     = 2026-09-02 09:31:01
      \__ year BE  \_ month, day, hour, minute, second
```

Exposed as `z75 time --read`. The seconds field was confirmed by reading three
times in a row and watching only the last byte advance.

> **`54 01` resets the module.** Sending it dropped the BLE link with a GATT
> error and cleared the real-time clock to a 2023-03-03 default, losing a
> correct setting. Sub-command `0x50` is a setter reading three argument bytes
> (`0x1001e8fa`). The remaining fifteen are unprobed and at least one more may
> be destructive. Probe this space deliberately or not at all.

### Why the sensor screens still cannot be driven over BLE

The screen module *does* parse the `0xA5` sensor protocol itself — there is a
bounds-checked dispatch table at `0x1000eed4`:

```
1000eed0  ldrb  r0, [r6]
1000eed4  cmp   r0, #0xa5
1000eed6  beq   0x1000eeec       ; -> index a handler table by [r6+1]
```

But the BLE receive path filters on the first byte for `0x54` or `0x88` only,
and `0xA5` matches neither. Confirmed empirically: raw `0x1C`-framed HID
reports written to either BLE characteristic draw **no reply at all**, while
malformed `0x88` frames still get the generic ack. The `0xA5` dispatcher is fed
from the keyboard MCU's link, not from BLE.

The one remaining avenue is a `0x54` or `0x88` sub-command that forwards a
payload into that dispatcher. Fifteen `0x54` sub-commands are unprobed.

### The two command spaces do not overlap

The module answers on two transports that carry **different, non-overlapping
protocols**:

| | framing | carries |
|---|---|---|
| BLE | `88 00 00 <len32> <xor> <opcode16> …` | dial images, clock, notes, notifications — the phone app's feature set |
| USB raw HID | `1C … <crc16> A5 <field8> <len16> …` | CPU/GPU temperature, fan RPM, network, weather — the built-in live screens |

There is no known way to reach the `0xA5` sensor space from BLE. Tested
directly: sending the BLE weather command `0x0407` to generation-1 hardware is
acknowledged and then **ignored** — the weather screen keeps reading 0. That
command is only wired up for the 2nd-generation module in the vendor app, and
the vendor's own architecture corroborates the split: they wrote BLE weather for
the 2nd-gen module but ship a *Windows USB* application to feed the same screens
on generation 1. Had BLE worked there, they would have used it.

**An acknowledgement proves nothing about support.** This firmware returns the
generic `0x0702` ack echoing the request opcode for commands it does not
implement — confirmed for notifications (`0x0501`), usage stats (`0x0023`),
alarms (`0x0025`) and weather (`0x0407`). Only a visible change on the panel, or
a data frame in reply, demonstrates that a command did anything.

### Undocumented commands

Six commands exist in the vendor SDK and are **never called from anywhere in
the app**: find device (`0x0019`), backlight (`0x0236`), select local dial
(`0x011B`), set current face (`0x0161`), power off (`0x0560`) and test mode
(`0x05FE`).

The last three share a magic tail `A1 FE 74 69`, which reads as a deliberate
guard against issuing them by accident:

```
0x0161  01 61 <index> A1 FE 74 69 02
0x0560  05 60 <mode>  A1 FE 74 69
0x05FE  05 FE 54      A1 FE 74 69
```

They are implemented behind `z75 danger ...` and an explicit `--i-understand`,
and none of them has been fired on hardware.

### Timestamps: the vendor's own bug

Device timestamps (notes, usage stats) count seconds from **2000-01-01** against
the wall clock `0x0401` set. The module has no timezone concept.

The vendor app decodes them by adding `946656000`, which is
2000-01-01T00:00:00**+08:00** — its developers' timezone baked in. Outside UTC+8
the app's own note timestamps are wrong by eight hours. The correct constant is
`946684800`, confirmed here: a note written at 08:28:38 local came back as
`841652918`, which is exactly 2000-01-01 + 841652918s.

### Sequence

1. `NVDS_TYPE` → if reply byte 4 has `0x10` set, request MTU 512 (the "8010H"
   variant), else MTU 247 ("8010"). Chunk size is `mtu - 3 - 9`.
2. `GET_STR_BASE` → a flash base address.
   - **8010**: banks are fixed at `0x00000` and `0x14000`; the host writes to
     whichever one the device is *not* running from.
   - **8010H**: the device nominates the destination directly.
3. `PAGE_ERASE` × `ceil(size / 4096)`, stepping 4096 from the target.
4. `WRITE_DATA` chunks, advancing the address, waiting for both the write
   acknowledgement and a notification after each.
5. Wait for the device to report the last chunk's start address, then `REBOOT`
   with the total length and CRC. The bootloader validates the CRC itself.

This is dual-bank: a transfer that fails or is interrupted damages only the
spare bank, and the device keeps running the image it booted from. That
property is what makes the operation survivable, and every guard in `ota.py`
exists to preserve it.

### The CRC is not stock CRC-32

`OtaUtils` shifts **left** while indexing a **reflected** table, and indexes it
with `crc / 256` — Java integer division, which truncates toward zero. On a
negative (high-bit-set) accumulator that differs from `crc >>> 8`, and the two
diverge completely: the shipped Zoom75 image is `0xf7db5f5c` the vendor's way
and `0xa0ff8a33` the obvious way. `getCRC32new` also reads the file in
256-byte blocks and **skips block 0**, so the first 256 bytes are excluded from
the CRC while still being written to flash.

### Image format

Version code is a little-endian u32 at offset `0x18`, matching both the value
the device reports over `0x0002` and the vendor API's `versionCode`. The
shipped image `…_0XC003_V00014F_…bin` is 148,988 bytes and decodes to V0.1.79 /
code 335 — exactly what the hardware here reports.

Note 148,988 bytes **exceeds** the `0x14000` gap between the hard-coded 8010
banks, so any device running a published image of this size must be using the
device-directed (8010H) path. `plan()` refuses the fixed-bank case when the
image would not fit, rather than letting a write run into the live bank.

### Confirmed on hardware

A read-only probe of a Zoom75 module (firmware V0.1.79) returns:

```
-> 00 03 00 00000000 0000        NVDS_TYPE
<- 00 00 01 00 11                reply byte 4 = 0x11, so bit 0x10 set -> 8010H
-> 01 03 00 00000000 0000        GET_STR_BASE
<- 00 01 04 00 00 20 03 00       u32 at offset 4 = 0x00032000
```

So this module is the **8010H** variant and nominates `0x32000` as the
destination — comfortably clear of the bank a 145 KB image boots from, and
consistent with a 512 KB part holding two ~145 KB slots. The fixed
`0x0`/`0x14000` constants do **not** apply to it.

### Update API

```
GET https://wuquedistribution.com:12349/checkUpdate
      ?firmwareVersionCode=<code>&productNumber=<product>
```

Returns `data.firmware` as `{fileName, ota, versionCode, forceUpdate, fileSize}`
or `null` when nothing newer exists. The download itself is plain HTTP on port
12348.

### Recovery

There is none over BLE if the bootloader is damaged. The dual-bank design means
a failed *transfer* is recoverable (retry), but a bad *commit* is not.
