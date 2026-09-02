"""`z75 fw ...` -- firmware inspection, update check, and (guarded) flashing."""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import ota

UPDATE_API = "https://wuquedistribution.com:12349/checkUpdate"
MIN_BATTERY = 50


def check_update(product: str, version_code: int, timeout: float = 20.0) -> dict | None:
    """Ask the vendor API whether anything newer exists. Read-only."""
    url = f"{UPDATE_API}?" + urllib.parse.urlencode(
        {"firmwareVersionCode": version_code, "productNumber": product}
    )
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = json.load(r)
    return (body.get("data") or {}).get("firmware")


def download(entry: dict, out_dir: Path, timeout: float = 120.0) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / entry["fileName"]
    with urllib.request.urlopen(entry["ota"], timeout=timeout) as r:
        dest.write_bytes(r.read())
    return dest


def describe(fw: ota.Firmware) -> str:
    return (
        f"  file       {fw.path.name}\n"
        f"  size       {fw.size} bytes ({fw.pages} pages of {ota.PAGE_SIZE})\n"
        f"  version    {fw.version}  (code {fw.version_code})\n"
        f"  crc        0x{fw.crc:08x}"
    )


# --- commands --------------------------------------------------------------


async def cmd_fw_info(args):
    fw = ota.Firmware.load(args.path)
    print("firmware image:")
    print(describe(fw))
    return 0


async def cmd_fw_check(args, open_screen):
    screen = await open_screen(args)
    try:
        info = await screen.device_info()
        battery = await screen.battery()
    finally:
        await screen.disconnect()
    print(f"device       product {info.product}  {info.version} (code {info.version_code})")
    print(f"             mac {info.mac}  generation {info.generation}")
    if battery is not None:
        print(f"battery      {battery}%")
    try:
        entry = check_update(info.product, info.version_code)
    except Exception as e:
        print(f"update check failed: {e}", file=sys.stderr)
        return 1
    if not entry:
        print("update       none -- the device is on the newest published firmware")
        return 0
    print(f"update       {entry['fileName']}")
    print(f"             version code {entry['versionCode']}, {entry.get('fileSize', '?')} KB")
    print(f"             {entry['ota']}")
    if entry["versionCode"] <= info.version_code:
        print("             (not newer than what is installed)")
    return 0


async def cmd_fw_download(args, open_screen):
    if args.product and args.version_code is not None:
        product, code = args.product, args.version_code
    else:
        screen = await open_screen(args)
        try:
            info = await screen.device_info()
        finally:
            await screen.disconnect()
        product, code = info.product, info.version_code
    entry = check_update(product, args.version_code if args.version_code is not None else 1)
    if not entry:
        print("no firmware published for this product")
        return 1
    dest = download(entry, Path(args.out))
    fw = ota.Firmware.load(dest)
    print(f"downloaded {dest}")
    print(describe(fw))
    if fw.version_code != entry["versionCode"]:
        print(
            f"WARNING: header version {fw.version_code} != API version "
            f"{entry['versionCode']}",
            file=sys.stderr,
        )
        return 1
    return 0


async def cmd_fw_probe(args, open_screen):
    """Read-only query of the OTA service."""
    screen = await open_screen(args)
    try:
        info = await screen.device_info()
        client = ota.OtaClient(screen.client, verbose=args.verbose)
        probe = await client.probe()
        await client.stop()
    finally:
        await screen.disconnect()
    print(f"device       product {info.product}  {info.version} (code {info.version_code})")
    print(f"nvds type    0x{probe.nvds_type:02x}")
    print(f"variant      {probe.variant}")
    print(f"base addr    0x{probe.base_addr:08x}")
    print(f"mtu          {probe.mtu}  ->  {probe.mtu - 3 - ota.WRITE_HEADER_LEN} bytes/chunk")
    if args.path:
        fw = ota.Firmware.load(args.path)
        print("\nimage:")
        print(describe(fw))
        try:
            target, chunk = ota.OtaClient.plan(client, fw, probe)
        except ota.OtaError as e:
            print(f"\nPLAN REJECTED: {e}")
            return 1
        print("\nplan (not executed):")
        print(f"  erase      {fw.pages} pages from 0x{target:08x} to "
              f"0x{target + fw.pages * ota.PAGE_SIZE - 1:08x}")
        print(f"  write      {fw.size} bytes in {-(-fw.size // chunk)} chunks of {chunk}")
        print(f"  commit     length {fw.size}, crc 0x{fw.crc:08x}")
        if probe.large_mtu:
            print("  note       this variant nominates its own destination, so the")
            print("             target being the reported address is expected")
    return 0


async def cmd_fw_flash(args, open_screen):
    fw = ota.Firmware.load(args.path)
    screen = await open_screen(args)
    try:
        # Everything below the OTA service is read-only display-protocol
        # traffic, so all the cheap refusals happen before the OTA service is
        # touched at all. An accidental invocation never reaches the flash.
        info = await screen.device_info()
        battery = await screen.battery()

        print("device :", f"product {info.product} {info.version} (code {info.version_code})")
        print("image  :", f"{fw.path.name} {fw.version} (code {fw.version_code})")
        if battery is not None:
            print("battery:", f"{battery}%")

        if not args.i_understand:
            raise ota.OtaAborted("refusing without --i-understand")
        if battery is not None and battery < MIN_BATTERY and not args.ignore_battery:
            raise ota.OtaAborted(
                f"battery is {battery}% (< {MIN_BATTERY}%). Losing power mid-write is "
                "the main way this bricks. Charge it, or pass --ignore-battery."
            )
        if fw.version_code == info.version_code and not args.allow_same:
            raise ota.OtaAborted(
                f"image {fw.version} is the version already installed. Re-flashing "
                "gains nothing and carries all the risk. Pass --allow-same to override."
            )
        if fw.version_code < info.version_code and not args.allow_downgrade:
            raise ota.OtaAborted(
                f"image {fw.version} is older than the installed {info.version}. "
                "Pass --allow-downgrade to override."
            )
        if not sys.stdin.isatty():
            raise ota.OtaAborted("refusing to flash non-interactively")

        # Read-only OTA query, then work out (and sanity-check) the plan.
        client = ota.OtaClient(screen.client, verbose=args.verbose)
        probe = await client.probe()
        target, chunk = client.plan(fw, probe)
        print("variant:", probe.variant)
        print("plan   :", f"erase {fw.pages} pages at 0x{target:08x}, write {fw.size} B "
                          f"in chunks of {chunk}, commit crc 0x{fw.crc:08x}")

        if input("\nType exactly FLASH to proceed: ").strip() != "FLASH":
            raise ota.OtaAborted("not confirmed")

        def progress(phase, done, total):
            if sys.stderr.isatty():
                sys.stderr.write(f"\r  {phase:<6} {done}/{total}   ")
                sys.stderr.flush()
                if done == total:
                    sys.stderr.write("\n")

        await client.flash(
            fw, probe, i_understand_this_rewrites_flash=True, progress=progress
        )
        print("committed -- the device reboots and validates the CRC itself.")
        print("If it rejects the image it keeps running the old bank.")
    finally:
        with contextlib.suppress(Exception):
            await screen.disconnect()
    return 0
