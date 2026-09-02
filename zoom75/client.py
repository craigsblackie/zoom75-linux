"""Async BLE client for the Zoom75 screen module."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as p


class Zoom75Error(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    """Decoded 0x0002 reply. `product` and `version_code` are exactly the two
    values the vendor's update API is keyed on."""

    product: str
    version: str
    version_code: int
    mac: str
    generation: int
    raw: bytes

    @classmethod
    def parse(cls, payload: bytes) -> "DeviceInfo":
        if len(payload) < 12:
            raise Zoom75Error(f"short device-info reply: {payload.hex()}")
        major, minor, patch = payload[2], payload[3], payload[4]
        return cls(
            product=payload[0:2].hex(),
            version=f"V{major}.{minor}.{patch}",
            version_code=(major << 16) | (minor << 8) | patch,
            mac=":".join(f"{b:02x}" for b in payload[5:11]),
            generation=payload[-1],
            raw=payload,
        )


class Zoom75Screen:
    def __init__(self, address_or_device: str | BLEDevice, *, timeout: float = 20.0, verbose: bool = False):
        self._target = address_or_device
        self._timeout = timeout
        self._verbose = verbose
        self._client: BleakClient | None = None
        self._waiters: dict[bytes, asyncio.Future] = {}
        self._listeners: list[Callable[[p.Reply], None]] = []

    # -- discovery ----------------------------------------------------------

    @staticmethod
    async def discover(timeout: float = 10.0) -> list[BLEDevice]:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for dev, adv in found.values():
            name = adv.local_name or dev.name or ""
            if p.SERVICE_UUID.lower() in [u.lower() for u in adv.service_uuids] or "zoom75" in name.lower().replace(" ", ""):
                out.append(dev)
        return out

    @staticmethod
    def _is_match(dev: BLEDevice, adv) -> bool:
        name = adv.local_name or dev.name or ""
        uuids = [u.lower() for u in adv.service_uuids]
        return p.SERVICE_UUID.lower() in uuids or "zoom75" in name.lower().replace(" ", "")

    @classmethod
    async def find(cls, timeout: float = 10.0, **kw) -> "Zoom75Screen":
        """Scan, returning as soon as the screen appears rather than always
        burning the full timeout."""
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_detect(dev: BLEDevice, adv):
            if not found.done() and cls._is_match(dev, adv):
                found.set_result(dev)

        async with BleakScanner(detection_callback=on_detect):
            try:
                device = await asyncio.wait_for(asyncio.shield(found), timeout)
            except asyncio.TimeoutError:
                device = None
        if device is not None:
            return cls(device, **kw)
        devices = await cls.discover(0.1)
        if not devices:
            raise Zoom75Error(
                "no Zoom75 screen found. It stops advertising while connected, so "
                "if a previous run was killed try: bluetoothctl disconnect <addr>, "
                "or pass a known address with -a."
            )
        return cls(devices[0], **kw)

    # -- connection ---------------------------------------------------------

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    @property
    def client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise Zoom75Error("not connected")
        return self._client

    async def connect(self):
        self._client = BleakClient(self._target, timeout=self._timeout)
        await self._client.connect()
        # BlueZ only reveals the negotiated ATT MTU once a write/notify socket
        # is acquired; without this bleak reports the 23-byte default.
        with contextlib.suppress(Exception):
            await self._client._backend._acquire_mtu()
        await self._client.start_notify(p.NOTIFY_UUID, self._on_notify)
        with contextlib.suppress(Exception):
            await self._client.start_notify(p.FLASH_NOTIFY_UUID, self._on_notify)
        # The panel needs a moment after subscribing before it answers.
        await asyncio.sleep(0.4)

    async def disconnect(self):
        if self._client is not None and self._client.is_connected:
            with contextlib.suppress(Exception):
                await self._client.stop_notify(p.NOTIFY_UUID)
            await self._client.disconnect()
        self._client = None

    @property
    def mtu(self) -> int:
        return getattr(self._client, "mtu_size", 23)

    # -- transport ----------------------------------------------------------

    def _log(self, *a):
        if self._verbose:
            print(*a)

    def _on_notify(self, _sender, data: bytearray):
        raw = bytes(data)
        self._log(f"  <- {raw.hex()}")
        reply = p.parse(raw)
        if reply is None:
            return
        for cb in list(self._listeners):
            cb(reply)
        fut = self._waiters.pop(reply.opcode, None)
        if fut is not None and not fut.done():
            fut.set_result(reply)

    async def send(self, data: bytes, *, char: str | None = None, response: bool = True):
        self._log(f"  -> {data.hex()}")
        await self.client.write_gatt_char(char or p.WRITE_UUID, data, response=response)

    async def request(self, data: bytes, expect: bytes, timeout: float = 6.0) -> p.Reply:
        """Send a command and wait for the matching reply opcode (request+1)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters[expect] = fut
        try:
            await self.send(data)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise Zoom75Error(f"timed out waiting for reply {expect.hex()}") from e
        finally:
            self._waiters.pop(expect, None)

    async def query(self, data: bytes, expect: bytes, timeout: float = 4.0) -> p.Reply | None:
        """A read that the device may answer with data *or* with a bare ack.

        Several query opcodes are acknowledged (0x0702 echoing the request) but
        never followed by a data frame -- either the feature is absent on this
        hardware or there is simply nothing recorded. Returns None in that case
        rather than raising.
        """
        loop = asyncio.get_running_loop()
        data_fut: asyncio.Future = loop.create_future()
        ack_fut: asyncio.Future = loop.create_future()
        self._waiters[expect] = data_fut
        self._waiters[p.OP_ACK] = ack_fut
        try:
            await self.send(data)
            done, _ = await asyncio.wait(
                {data_fut, ack_fut}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if data_fut in done:
                return data_fut.result()
            if ack_fut in done:
                # Give the data frame a moment to arrive after the ack.
                with contextlib.suppress(asyncio.TimeoutError):
                    return await asyncio.wait_for(asyncio.shield(data_fut), 1.5)
                return None
            raise Zoom75Error(f"no response to {data[8:10].hex()}")
        finally:
            self._waiters.pop(expect, None)
            self._waiters.pop(p.OP_ACK, None)

    # -- commands -----------------------------------------------------------

    async def status(self) -> int:
        reply = await self.request(p.cmd_status(), b"\x00\x14")
        return reply.payload[-1]

    async def firmware(self) -> p.Reply:
        return await self.request(p.cmd_device_version(), b"\x00\x02")

    async def device_info(self) -> DeviceInfo:
        return DeviceInfo.parse((await self.firmware()).payload)

    async def system_data(self) -> p.Reply | None:
        return await self.query(p.cmd_system_data(), b"\x00\x18")

    async def battery(self) -> int | None:
        with contextlib.suppress(Exception):
            return (await self.client.read_gatt_char(p.BATTERY_LEVEL_UUID))[0]
        return None

    async def sync_time(self, when: dt.datetime | None = None, *, english: bool = True) -> dt.datetime:
        """Set the module's real-time clock. Returns the instant that was sent.

        The device answers with the generic ack (0x0702) echoing the request
        opcode; there is no read-back command, so that ack is the only
        confirmation available.
        """
        t = when or dt.datetime.now()
        reply = await self.request(
            p.cmd_sync_time(t.year, t.month, t.day, t.hour, t.minute, t.second), p.OP_ACK
        )
        if reply.payload[1:3] != p.OP_SYNC_TIME:
            raise Zoom75Error(
                f"device acked {reply.payload.hex()}, expected an ack for "
                f"{p.OP_SYNC_TIME.hex()}"
            )
        # The app never sends the time on its own -- every connect is
        # sync_time followed by this, so it is sent here too.
        await self.request(p.cmd_device_info(english=english), p.OP_ACK)
        return t

    async def set_screen_mode(self, mode: int):
        await self.send(p.cmd_screen_mode(mode))

    async def set_style(self, *, screen: int | None = None, clock: int | None = None):
        if screen is not None:
            await self.send(p.cmd_style(True, screen))
        if clock is not None:
            await self.send(p.cmd_style(False, clock))

    # -- notifications, notes, stats ---------------------------------------

    async def notify(self, app_id: int, title: str, body: str):
        """Push a notification to the panel. app_id selects the icon."""
        await self.request(p.cmd_notify(app_id, title, body), p.OP_ACK)

    async def write_note(self, title: str, content: str, when: dt.datetime | None = None):
        """Store a note. Sent as two frames, the second only after the first
        is acked -- the app does the same."""
        first, second = p.cmd_note(title, content, when or dt.datetime.now())
        await self.request(first, p.OP_ACK)
        await self.request(second, p.OP_ACK)

    async def note_info(self):
        """Timestamp of the stored note, or None if there is none."""
        reply = await self.query(p.cmd_note_info(), b"\x00\x16")
        return p.parse_note_info(reply.payload) if reply else None

    async def delete_note(self, device_time: int):
        await self.request(p.cmd_note_delete(device_time), p.OP_ACK)

    async def use_time(self):
        """Per-day usage. Returns [(unix_seconds, minutes)]."""
        reply = await self.query(p.cmd_use_time(), b"\x00\x24")
        return p.parse_use_time(reply.payload) if reply else None

    async def alarms(self):
        reply = await self.query(p.cmd_alarm_read(), b"\x00\x26")
        return p.parse_alarms(reply.payload) if reply else None

    async def set_alarms(self, alarms):
        await self.request(p.cmd_alarm_set(alarms), p.OP_ACK)

    async def set_weather(self, **kw):
        await self.request(p.cmd_weather(**kw), p.OP_ACK)

    async def find_device(self):
        await self.request(p.cmd_find_device(), p.OP_ACK)

    async def set_backlight(self, level: int, timeout: int):
        await self.request(p.cmd_backlight(level, timeout), p.OP_ACK)

    async def select_local_dial(self, index: int):
        await self.request(p.cmd_set_local_dial(index), p.OP_ACK)

    # -- privileged --------------------------------------------------------
    # None of these are sent by the vendor app on this hardware, and the last
    # two carry the SDK's magic tail. They are reachable only with an explicit
    # acknowledgement so nothing fires them by accident.

    async def unbind(self, *, confirm: bool = False):
        if not confirm:
            raise Zoom75Error("unbind() requires confirm=True")
        await self.send(p.cmd_unbind())

    async def power_off(self, mode: int = 1, *, confirm: bool = False):
        if not confirm:
            raise Zoom75Error("power_off() requires confirm=True")
        await self.send(p.cmd_power_off(mode))

    async def enter_test_mode(self, *, confirm: bool = False):
        if not confirm:
            raise Zoom75Error("enter_test_mode() requires confirm=True")
        await self.send(p.cmd_test_mode())

    async def restore_builtin(self):
        await self.send(p.cmd_local_dial())

    # -- image upload -------------------------------------------------------

    async def upload(
        self,
        data: bytes,
        *,
        animated: bool = False,
        chunk_delay: float = 0.0,
        block_timeout: float = 8.0,
        progress: Callable[[int, int], None] | None = None,
    ):
        """Flash a still frame or animation blob to the panel.

        The panel acknowledges every 4096-byte block on opcode 0x0806 with
        status 5, and the whole transfer with status 2, so each block is paced
        against its own acknowledgement rather than a fixed delay.
        """
        # An ATT write carries MTU-3 bytes; a chunk is 243 payload + 1 XOR.
        needed = p.CHUNK_SIZE + 1 + 3
        if self.mtu < needed:
            raise Zoom75Error(
                f"negotiated MTU is {self.mtu}, need >= {needed} for "
                f"{p.CHUNK_SIZE + 1}-byte chunks. Try reconnecting."
            )

        info = await self.request(p.cmd_dial_info(len(data), animated), b"\x09\x04")
        if info.status == p.DIAL_INFO_INVALID:
            raise Zoom75Error("screen rejected the image size/descriptor")
        if info.status == p.DIAL_INFO_BUSY:
            raise Zoom75Error("screen is busy")

        begin = await self.request(p.cmd_dial_begin(), b"\x08\x04")
        if begin.status != 2:
            raise Zoom75Error(f"screen refused to start the transfer (status {begin.status})")

        for _ in range(20):
            if await self.status() == p.STATUS_READY_FOR_FLASH:
                break
            await asyncio.sleep(0.15)
        else:
            raise Zoom75Error("screen never became ready for the flash write")

        acks: asyncio.Queue[int] = asyncio.Queue()

        def watch(reply: p.Reply):
            if reply.opcode == b"\x08\x06":
                acks.put_nowait(reply.status)

        self._listeners.append(watch)
        try:
            blocks = p.build_flash_blocks(data)
            total = sum(len(b) for b in blocks)
            sent = 0
            for block_i, block in enumerate(blocks):
                for chunk in block:
                    await self.send(chunk, char=p.FLASH_WRITE_UUID, response=True)
                    sent += 1
                    if progress:
                        progress(sent, total)
                    if chunk_delay:
                        await asyncio.sleep(chunk_delay)
                try:
                    code = await asyncio.wait_for(acks.get(), block_timeout)
                except asyncio.TimeoutError as e:
                    raise Zoom75Error(
                        f"no acknowledgement for block {block_i + 1}/{len(blocks)}"
                    ) from e
                if code in (p.FLASH_FAILED, p.FLASH_ERROR_EXIT):
                    raise Zoom75Error(
                        f"screen aborted at block {block_i + 1}/{len(blocks)} (code {code})"
                    )
            # A final status 2 arrives once the panel has committed the image.
            try:
                while True:
                    code = await asyncio.wait_for(acks.get(), 3.0)
                    if code == p.FLASH_SUCCESS:
                        break
                    if code in (p.FLASH_FAILED, p.FLASH_ERROR_EXIT):
                        raise Zoom75Error(f"screen rejected the image (code {code})")
            except asyncio.TimeoutError:
                pass  # the last block ack may already have been the final one
        finally:
            self._listeners.remove(watch)
