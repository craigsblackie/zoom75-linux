"""Command line front-end: z75 <command>."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt_module
import signal
import sys
import time

from bleak.exc import BleakError

from . import dashboard
from . import devices as devmod
from . import extras
from . import fwcli
from . import hid as rawhid
from . import image as im
from . import protocol as p
from . import ota
from .client import Zoom75Error, Zoom75Screen
from .stats import Sampler


def _dev(args):
    return devmod.by_name(getattr(args, "device", devmod.DEFAULT.name))


def _progress(sent: int, total: int):
    if not sys.stderr.isatty():
        return  # keep logs and pipes readable
    pct = sent * 100 // total
    bar = "#" * (pct // 4)
    sys.stderr.write(f"\r  [{bar:<25}] {pct:3d}%  {sent}/{total} packets")
    sys.stderr.flush()
    if sent == total:
        sys.stderr.write("\n")


async def _open(args) -> Zoom75Screen:
    if args.address:
        screen = Zoom75Screen(args.address, verbose=args.verbose)
    else:
        screen = await Zoom75Screen.find(timeout=args.scan_timeout, verbose=args.verbose)
    await screen.connect()
    return screen


async def cmd_scan(args):
    devices = await Zoom75Screen.discover(args.scan_timeout)
    if not devices:
        print("no Zoom75 screen found")
        return 1
    for d in devices:
        print(f"{d.address}  {d.name}")
    return 0


async def cmd_info(args):
    screen = await _open(args)
    try:
        print(f"address      {screen.client.address}")
        print(f"mtu          {screen.mtu}")
        print(f"status       {await screen.status()}")
        batt = await screen.battery()
        if batt is not None:
            print(f"battery      {batt}%")
        try:
            print(f"firmware     {(await screen.firmware()).payload.hex()}")
        except Zoom75Error:
            pass
        # system_data() is a query(): it returns None when the device acks but
        # sends no data frame, which is what this hardware does.
        sysdata = await screen.system_data()
        if sysdata is not None:
            print(f"system       {sysdata.payload.hex()}")
    finally:
        await screen.disconnect()
    return 0


async def cmd_image(args):
    data = im.load_still(args.path, args.fit, _dev(args))
    screen = await _open(args)
    try:
        await screen.upload(data, animated=False, chunk_delay=args.delay, progress=_progress)
        print("image sent")
    finally:
        await screen.disconnect()
    return 0


async def cmd_gif(args):
    frames = im.load_frames(args.path, args.fit, max_frames=args.max_frames, device=_dev(args))
    if not frames:
        print("no frames in that file", file=sys.stderr)
        return 1
    blob = im.build_animation(frames, speed=args.speed, device=_dev(args))
    print(f"{len(frames)} frames, {len(blob)} bytes")
    screen = await _open(args)
    try:
        await screen.upload(blob, animated=True, chunk_delay=args.delay, progress=_progress)
        print("animation sent")
    finally:
        await screen.disconnect()
    return 0


async def cmd_text(args):
    data = im.render_text(
        args.text.replace("\\n", "\n"),
        device=_dev(args),
        font_path=args.font,
        size=args.size,
        fg=_color(args.fg),
        bg=_color(args.bg),
    )
    screen = await _open(args)
    try:
        await screen.upload(data, animated=False, chunk_delay=args.delay, progress=_progress)
        print("text sent")
    finally:
        await screen.disconnect()
    return 0



async def cmd_stats(args):
    sampler = Sampler(args.iface)

    def frame():
        img = dashboard.render(sampler.sample())
        if args.save:
            img.save(args.save)
        return im.to_rgb565(img)

    if args.dry_run:
        frame()
        print(f"rendered to {args.save}" if args.save else "rendered (use --save to keep it)")
        return 0

    screen = await _open(args)
    last_sync = 0.0
    try:
        while True:
            started = time.monotonic()
            try:
                # We are already connected, so keeping the module's real-time
                # clock correct costs one small write.
                if args.sync_every and (last_sync == 0.0 or started - last_sync >= args.sync_every):
                    with contextlib.suppress(Zoom75Error):
                        await screen.sync_time()
                        last_sync = started
                await screen.upload(frame(), chunk_delay=args.delay, progress=_progress)
                print(f"updated at {time.strftime('%H:%M:%S')}")
            except (Zoom75Error, BleakError) as e:
                if not args.watch:
                    raise
                # A dashboard is meant to run unattended; reconnect and carry on.
                print(f"refresh failed ({e}); reconnecting", file=sys.stderr)
                with contextlib.suppress(Exception):
                    await screen.disconnect()
                await asyncio.sleep(5)
                with contextlib.suppress(Exception):
                    screen = await _open(args)
                last_sync = 0.0
                continue
            if not args.watch:
                break
            elapsed = time.monotonic() - started
            if elapsed > args.interval:
                print(
                    f"note: the upload took {elapsed:.0f}s, longer than the "
                    f"{args.interval:.0f}s interval", file=sys.stderr)
            await asyncio.sleep(max(0.0, args.interval - elapsed))
    finally:
        with contextlib.suppress(Exception):
            await screen.disconnect()
    return 0



# `fw` subcommands live in fwcli; they are handed _open so they share the same
# discovery and connection path as everything else.
async def cmd_fw(args):
    if args.fw_cmd == "info":
        return await fwcli.cmd_fw_info(args)
    if args.fw_cmd == "check":
        return await fwcli.cmd_fw_check(args, _open)
    if args.fw_cmd == "download":
        return await fwcli.cmd_fw_download(args, _open)
    if args.fw_cmd == "probe":
        return await fwcli.cmd_fw_probe(args, _open)
    if args.fw_cmd == "flash":
        return await fwcli.cmd_fw_flash(args, _open)
    raise Zoom75Error(f"unknown fw command {args.fw_cmd}")



async def cmd_notify(args):
    screen = await _open(args)
    try:
        await screen.notify(args.app, args.title, args.body)
        print(f"notification sent (app id {args.app})")
    finally:
        await screen.disconnect()
    return 0


async def cmd_note(args):
    screen = await _open(args)
    try:
        await screen.write_note(args.title, args.content)
        stamp = await screen.note_info()
        print(f"note stored (device timestamp {stamp:%Y-%m-%d %H:%M:%S})" if stamp else "note stored")
    finally:
        await screen.disconnect()
    return 0


async def cmd_usage(args):
    screen = await _open(args)
    try:
        rows = await screen.use_time()
    finally:
        await screen.disconnect()
    if rows is None:
        print("this device acknowledges 0x0023 but returns no usage data")
        return 0
    if not rows:
        print("no usage data recorded")
        return 0
    for when, minutes in rows:
        print(f"{when:%Y-%m-%d}  {minutes:5d} min")
    return 0


async def cmd_alarms(args):
    screen = await _open(args)
    try:
        if args.set is not None:
            entries = []
            for item in filter(None, args.set.split(",")):
                hh, _, mm = item.strip().partition(":")
                entries.append((True, args.repeat, int(hh), int(mm)))
            await screen.set_alarms(entries)
            print(f"{len(entries)} alarm(s) set")
        rows = await screen.alarms()
    finally:
        await screen.disconnect()
    if rows is None:
        print("this device acknowledges 0x0025 but returns no alarm data")
        return 0
    if not rows:
        print("no alarms set")
    for a in rows:
        state = "on " if a["enabled"] else "off"
        print(f"  [{a['index']}] {state} {a['hour']:02d}:{a['minute']:02d}  repeat 0b{a['repeat']:07b}")
    return 0


async def cmd_find(args):
    screen = await _open(args)
    try:
        await screen.find_device()
        print("find-device sent")
    finally:
        await screen.disconnect()
    return 0


async def cmd_backlight(args):
    screen = await _open(args)
    try:
        await screen.set_backlight(args.level, args.timeout)
        print(f"backlight level {args.level}, timeout {args.timeout}")
    finally:
        await screen.disconnect()
    return 0


async def cmd_danger(args):
    """Commands the vendor app never sends to this hardware."""
    if not args.i_understand:
        print(
            f"'{args.danger_cmd}' is not sent by the vendor app on generation-1 "
            "hardware and its effect here is unverified.\n"
            "Re-run with --i-understand if you want to try it.",
            file=sys.stderr,
        )
        return 2
    screen = await _open(args)
    try:
        if args.danger_cmd == "poweroff":
            await screen.power_off(args.mode, confirm=True)
        elif args.danger_cmd == "testmode":
            await screen.enter_test_mode(confirm=True)
        elif args.danger_cmd == "unbind":
            await screen.unbind(confirm=True)
        print(f"{args.danger_cmd} sent")
    finally:
        with contextlib.suppress(Exception):
            await screen.disconnect()
    return 0



def _fetch_weather(lat: float, lon: float):
    """Current conditions from Open-Meteo -- no API key needed."""
    import json as _json
    import urllib.request
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&timezone=auto"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        cur = _json.load(r)["current"]
    wmo = int(cur["weather_code"])
    temp = round(float(cur["temperature_2m"]))
    hour = int(cur["time"][11:13])
    icon = rawhid.wmo_to_icon(wmo, is_night=hour < 6 or hour >= 19)
    return icon, temp, f"wmo {wmo} -> icon {icon}, {temp}C at {cur['time']}"


def _hid_values(sampler):
    """The four values the built-in screens expect."""
    sample = sampler.sample()
    cpu_t = getattr(sample.cpu, "temp", None)
    gpu_t = None
    if sample.gpu and sample.gpu.detail:
        gpu_t = int(sample.gpu.detail.split("°")[0])
    fan = sample.fan[0] if sample.fan else 0
    net = int((sample.rx_bps + sample.tx_bps) / 2)   # the vendor app averages them
    return (int(cpu_t) if cpu_t else 0), (gpu_t or 0), fan, net, sample


async def cmd_hid(args):
    from .stats import Sampler
    if args.hid_cmd == "probe":
        for d in rawhid.find_devices():
            print(f"{d.path}  vid {d.vendor_id:04x} pid {d.product_id:04x}")
        if not rawhid.find_devices():
            print("no Zoom75 raw-HID interface found")
            return 1
        cpu, gpu, fan, net, sample = _hid_values(Sampler(args.iface))
        print(f"\nwould send: cpu {cpu}C  gpu {gpu}C  fan {fan} rpm  net {net} B/s")
        if sample.fan:
            print(f"fan source: {sample.fan[1]}")
        for name, rep in [("cpu", rawhid.report_cpu_temp(cpu)), ("gpu", rawhid.report_gpu_temp(gpu)),
                          ("fan", rawhid.report_fan_rpm(fan)), ("net", rawhid.report_net_speed(net))]:
            print(f"  {name}: {rep.hex()}")
        return 0

    device = rawhid.find_device()
    sampler = Sampler(args.iface)

    def push_once():
        cpu, gpu, fan, net, sample = _hid_values(sampler)
        if device.device.hid_style == "bundled":
            # Tiga / Dyna / Hetix take everything in one report.
            device.write(rawhid.report_bundle(cpu, gpu, 0, fan, net))
        else:
            device.write(rawhid.report_cpu_temp(cpu))
            device.write(rawhid.report_gpu_temp(gpu))
            device.write(rawhid.report_fan_rpm(fan))
            device.write(rawhid.report_net_speed(net))
        return cpu, gpu, fan, net

    if args.hid_cmd == "weather":
        code, temp = args.code, args.temp
        if code is None or temp is None:
            code, temp, desc = _fetch_weather(args.lat, args.lon)
            print(f"open-meteo: {desc}")
        device.write(rawhid.report_weather(code, temp))
        print(f"weather sent: icon {code}, {temp}C")
        return 0

    if args.hid_cmd == "time":
        device.write(rawhid.report_time(dt_module.datetime.now()))
        print("clock report sent over USB")
        return 0

    while True:
        cpu, gpu, fan, net = push_once()
        if args.sync_time:
            device.write(rawhid.report_time(dt_module.datetime.now()))
        print(f"{time.strftime('%H:%M:%S')}  cpu {cpu}C  gpu {gpu}C  fan {fan} rpm  "
              f"net {net} B/s", flush=True)
        if not args.watch:
            return 0
        await asyncio.sleep(args.interval)



async def cmd_devices(args):
    for d in devmod.ALL:
        mark = "verified" if d.verified else "untested"
        pids = ", ".join(f"{d.usb_vid:04x}:{p:04x}" for p in d.usb_pids) or "-"
        print(f"{d.name:11} {d.width:>4}x{d.height:<4} hid={d.hid_style:8} {mark:9} {pids}")
        if d.notes:
            print(f"            {d.notes}")
    return 0


async def cmd_slideshow(args):
    paths = extras.slideshow_paths(args.folder, shuffle=args.shuffle)
    if not paths:
        print(f"no images in {args.folder}", file=sys.stderr)
        return 1
    device = _dev(args)
    print(f"{len(paths)} image(s); {args.interval:.0f}s between uploads "
          f"(each takes ~35s on top)")
    screen = await _open(args)
    try:
        while True:
            for path in paths:
                try:
                    await screen.upload(im.load_still(path, args.fit, device),
                                        progress=_progress)
                    print(f"{time.strftime('%H:%M:%S')}  {path.name}", flush=True)
                except (Zoom75Error, BleakError) as e:
                    print(f"skipped {path.name}: {e}", file=sys.stderr)
                if not args.loop and path is paths[-1]:
                    return 0
                await asyncio.sleep(args.interval)
    finally:
        with contextlib.suppress(Exception):
            await screen.disconnect()


async def cmd_nowplaying(args):
    device = _dev(args)

    async def frame():
        track = await extras.now_playing()
        if track is None:
            return None, None
        return extras.now_playing_frame(track, device), track

    if args.dry_run:
        data, track = await frame()
        if track is None:
            print("no MPRIS player is reporting a track")
            return 1
        print(f"{track['artist']} - {track['title']} ({track['status']})")
        if args.save:
            extras.render_now_playing(track, device).save(args.save)
            print(f"rendered to {args.save}")
        return 0

    screen = await _open(args)
    last = None
    try:
        while True:
            data, track = await frame()
            if track is None:
                print("no player reporting a track", flush=True)
            elif (track["title"], track["artist"]) != last:
                await screen.upload(data, progress=_progress)
                last = (track["title"], track["artist"])
                print(f"{time.strftime('%H:%M:%S')}  {track['artist']} - {track['title']}",
                      flush=True)
            if not args.watch:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        with contextlib.suppress(Exception):
            await screen.disconnect()


async def cmd_time(args):
    async def sync_once() -> str:
        screen = await _open(args)
        try:
            t = await screen.sync_time()
            return f"{t:%Y-%m-%d %H:%M:%S}"
        finally:
            with contextlib.suppress(Exception):
                await screen.disconnect()

    if args.read:
        screen = await _open(args)
        try:
            got = await screen.read_clock()
        finally:
            with contextlib.suppress(Exception):
                await screen.disconnect()
        if got is None:
            print("no clock read-back")
            return 1
        drift = (dt_module.datetime.now() - got).total_seconds()
        print(f"module clock  {got:%Y-%m-%d %H:%M:%S}")
        print(f"host clock    {dt_module.datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"drift         {drift:+.0f}s")
        return 0

    if not args.watch:
        print(f"module clock set to {await sync_once()}")
        return 0

    # Deliberately disconnect between syncs: the screen stops advertising while
    # connected, so holding the link open for hours would lock out every other
    # command for the sake of one small write.
    while True:
        try:
            print(f"{time.strftime('%H:%M:%S')}  synced -> {await sync_once()}", flush=True)
        except (Zoom75Error, BleakError) as e:
            print(f"{time.strftime('%H:%M:%S')}  sync failed: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(min(60.0, args.interval))
            continue
        await asyncio.sleep(args.interval)


async def cmd_mode(args):
    screen = await _open(args)
    try:
        await screen.set_screen_mode(args.mode)
        print(f"screen mode {args.mode}")
    finally:
        await screen.disconnect()
    return 0


async def cmd_style(args):
    screen = await _open(args)
    try:
        await screen.set_style(screen=args.screen, clock=args.clock)
        print("style set")
    finally:
        await screen.disconnect()
    return 0


async def cmd_restore(args):
    screen = await _open(args)
    try:
        await screen.restore_builtin()
        print("restored built-in dial")
    finally:
        await screen.disconnect()
    return 0


async def cmd_raw(args):
    payload = bytes.fromhex(args.hex.replace(" ", ""))
    data = payload if args.framed else p.frame(payload)
    screen = await _open(args)
    try:
        print(f"-> {data.hex()}")
        if args.expect:
            reply = await screen.request(data, bytes.fromhex(args.expect))
            print(f"<- {reply.raw.hex()}")
        else:
            await screen.send(data)
            await asyncio.sleep(args.wait)
    finally:
        await screen.disconnect()
    return 0


async def cmd_watch(args):
    screen = await _open(args)
    screen._listeners.append(lambda r: print(f"<- {r.opcode.hex()} {r.payload.hex()}"))
    try:
        print("listening for notifications, ctrl-c to stop")
        await asyncio.sleep(args.seconds)
    finally:
        await screen.disconnect()
    return 0


def _color(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="z75", description="Control the Zoom75 keyboard's BLE screen")
    ap.add_argument("-a", "--address", help="BLE address (skips scanning)")
    ap.add_argument("-t", "--scan-timeout", type=float, default=10.0)
    ap.add_argument("-v", "--verbose", action="store_true", help="dump every packet")
    ap.add_argument("-D", "--device", default=devmod.DEFAULT.name,
                    choices=sorted(devmod.BY_NAME),
                    help="screen model (default zoom75; only zoom75 is hardware-verified)")
    ap.add_argument("--delay", type=float, default=0.0, help="extra pause between bulk packets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="list nearby Zoom75 screens").set_defaults(fn=cmd_scan)
    sub.add_parser("info", help="show status, battery and firmware").set_defaults(fn=cmd_info)

    s = sub.add_parser("image", help="upload a still image")
    s.add_argument("path")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("gif", help="upload an animation")
    s.add_argument("path")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s.add_argument("--speed", type=int, default=im.GIF_SPEED_DEFAULT, help="1 slowest .. 10 fastest")
    s.add_argument("--max-frames", type=int, default=im.MAX_FRAMES,
                   help=f"thin the animation to this many frames (default {im.MAX_FRAMES})")
    s.set_defaults(fn=cmd_gif)

    s = sub.add_parser("text", help="render text and upload it")
    s.add_argument("text")
    s.add_argument("--font")
    s.add_argument("--size", type=int)
    s.add_argument("--fg", default="ffffff")
    s.add_argument("--bg", default="000000")
    s.set_defaults(fn=cmd_text)

    s = sub.add_parser("stats", help="show CPU / GPU / memory / network stats")
    s.add_argument("--watch", action="store_true", help="keep refreshing")
    s.add_argument("--interval", type=float, default=60.0,
                   help="target seconds between refresh starts (default 60; "
                        "a single upload already takes ~35s)")
    s.add_argument("--iface", help="network interface (default: the one with the default route)")
    s.add_argument("--save", help="also write the rendered frame to this PNG")
    s.add_argument("--dry-run", action="store_true", help="render only, do not connect")
    s.add_argument("--sync-every", type=float, default=21600.0,
                   help="also sync the module clock this often, in seconds (default 6h; 0 disables)")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("time", help="set the module's own clock from this machine")
    s.add_argument("--read", action="store_true",
                   help="read the module clock back instead of setting it")
    s.add_argument("--watch", action="store_true",
                   help="keep re-syncing, releasing the connection between runs")
    s.add_argument("--interval", type=float, default=21600.0,
                   help="seconds between re-syncs in --watch mode (default 6h)")
    s.set_defaults(fn=cmd_time)

    s = sub.add_parser("mode", help="set screen mode (1 clock, 2 image)")
    s.add_argument("mode", type=int, choices=[1, 2])
    s.set_defaults(fn=cmd_mode)

    s = sub.add_parser("style",
                       help="select a built-in style (2nd-gen devices only; the app never "
                            "sends this to a generation-1 Zoom75)")
    s.add_argument("--screen", type=int)
    s.add_argument("--clock", type=int)
    s.set_defaults(fn=cmd_style)

    sub.add_parser("restore", help="switch back to the built-in dial").set_defaults(fn=cmd_restore)

    fw = sub.add_parser("fw", help="firmware: check for updates, inspect, flash")
    fwsub = fw.add_subparsers(dest="fw_cmd", required=True)
    fwsub.add_parser("check", help="report the installed version and ask the vendor API for updates")
    q = fwsub.add_parser("info", help="parse a firmware file locally (no device)")
    q.add_argument("path")
    q = fwsub.add_parser("download", help="fetch the newest published firmware")
    q.add_argument("--out", default="firmware", help="destination directory")
    q.add_argument("--product", help="product number, e.g. c003 (default: ask the device)")
    q.add_argument("--version-code", type=int, help="pretend to be this version when asking")
    q = fwsub.add_parser("probe", help="read-only query of the OTA service; add a file to dry-run the plan")
    q.add_argument("path", nargs="?", help="optional firmware file to plan against")
    q = fwsub.add_parser("flash", help="WRITE FIRMWARE -- see PROTOCOL.md before using")
    q.add_argument("path")
    q.add_argument("--i-understand", dest="i_understand", action="store_true",
                   help="required: acknowledge this rewrites flash and can brick the module")
    q.add_argument("--allow-same", action="store_true", help="permit re-flashing the installed version")
    q.add_argument("--allow-downgrade", action="store_true", help="permit flashing an older version")
    q.add_argument("--ignore-battery", action="store_true", help="skip the battery check (not advised)")
    fw.set_defaults(fn=cmd_fw)

    s = sub.add_parser("notify", help="push a notification to the panel")
    s.add_argument("title")
    s.add_argument("body")
    s.add_argument("--app", type=int, default=p.APP_DISCORD,
                   help="app id selecting the icon: 5 WeChat, 9 QQ, 13 Discord (default 13)")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("note", help="store a note on the device")
    s.add_argument("title")
    s.add_argument("content")
    s.set_defaults(fn=cmd_note)

    sub.add_parser("usage", help="per-day usage statistics").set_defaults(fn=cmd_usage)

    s = sub.add_parser("alarms", help="list or set alarms")
    s.add_argument("--set", help='comma-separated times, e.g. "07:30,08:15"')
    s.add_argument("--repeat", type=int, default=0,
                   help="repeat bitmask, bit 0 = Sunday (default 0, one-shot)")
    s.set_defaults(fn=cmd_alarms)

    sub.add_parser("find", help="trigger the device's find-me alert").set_defaults(fn=cmd_find)

    s = sub.add_parser("backlight", help="set backlight level and timeout")
    s.add_argument("level", type=int)
    s.add_argument("timeout", type=int)
    s.set_defaults(fn=cmd_backlight)

    d = sub.add_parser("danger", help="commands the vendor app never sends here (unverified)")
    dsub = d.add_subparsers(dest="danger_cmd", required=True)
    for name, helptext in [("poweroff", "power the module off"),
                           ("testmode", "enter the SDK test mode"),
                           ("unbind", "vendor 'recycle device' / unbind")]:
        q = dsub.add_parser(name, help=helptext)
        q.add_argument("--i-understand", dest="i_understand", action="store_true")
        if name == "poweroff":
            q.add_argument("--mode", type=int, default=1)
    d.set_defaults(fn=cmd_danger)

    h = sub.add_parser("hid", help="feed the built-in CPU/GPU/fan/network screens over USB")
    hsub = h.add_subparsers(dest="hid_cmd", required=True)
    for name, helptext in [("probe", "show the raw-HID device and what would be sent"),
                           ("feed", "push CPU/GPU/fan/network values"),
                           ("time", "push the clock over USB"),
                           ("weather", "push the weather icon and temperature")]:
        q = hsub.add_parser(name, help=helptext)
        q.add_argument("--iface", help="network interface (default: the default route)")
        if name == "feed":
            q.add_argument("--watch", action="store_true", help="keep pushing")
            q.add_argument("--interval", type=float, default=2.0, help="seconds between pushes")
            q.add_argument("--sync-time", action="store_true", help="also push the clock each cycle")
        if name == "weather":
            q.add_argument("--code", type=int, help="icon: 0 clear, 1 partly, 3 partly-night, "
                                                   "4 clear-night, 5 cloud, 6 rain, 7 snow, 8 storm")
            q.add_argument("--temp", type=int, help="temperature in Celsius")
            q.add_argument("--lat", type=float, default=51.5072, help="latitude for auto-fetch")
            q.add_argument("--lon", type=float, default=-0.1276, help="longitude for auto-fetch")
    h.set_defaults(fn=cmd_hid)

    s = sub.add_parser("slideshow", help="cycle through a folder of images")
    s.add_argument("folder")
    s.add_argument("--interval", type=float, default=60.0, help="pause between uploads")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s.add_argument("--shuffle", action="store_true")
    s.add_argument("--loop", action="store_true", help="repeat forever")
    s.set_defaults(fn=cmd_slideshow)

    s = sub.add_parser("nowplaying", help="show the current MPRIS track with album art")
    s.add_argument("--watch", action="store_true", help="keep it updated as tracks change")
    s.add_argument("--interval", type=float, default=15.0, help="how often to check for a change")
    s.add_argument("--dry-run", action="store_true", help="report the track, do not upload")
    s.add_argument("--save", help="also write the rendered frame to this PNG")
    s.set_defaults(fn=cmd_nowplaying)

    s = sub.add_parser("devices", help="list supported screen models")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("raw", help="send a raw payload (hex)")
    s.add_argument("hex")
    s.add_argument("--framed", action="store_true", help="hex already includes the 88 header")
    s.add_argument("--expect", help="reply opcode to wait for, e.g. 0014")
    s.add_argument("--wait", type=float, default=1.0)
    s.set_defaults(fn=cmd_raw)

    s = sub.add_parser("watch", help="print notifications")
    s.add_argument("--seconds", type=float, default=60.0)
    s.set_defaults(fn=cmd_watch)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Turn SIGTERM into KeyboardInterrupt so the `finally: disconnect()` blocks
    # run -- a half-torn-down BLE link leaves the screen unable to advertise.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        return asyncio.run(args.fn(args))
    except ota.OtaAborted as e:
        print(f"aborted (nothing was written): {e}", file=sys.stderr)
        return 2
    except (Zoom75Error, ota.OtaError, rawhid.HidError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
