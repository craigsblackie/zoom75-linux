"""Long-running daemon that drives everything on the Zoom75 screen.

Runs as a system service so that changing what the screen shows requires root.
Because it runs as root it needs no udev rule, which means the keyboard's input
interfaces stay `crw------- root root` and no unprivileged process can read
keystrokes from them. See systemd/zoom75-screen.service for the sandbox.

Two independent channels, each degrading on its own:

* USB raw HID  -- CPU/GPU temperature, fan RPM, network throughput, weather,
  clock. This is what the built-in screens read.
* BLE          -- the rendered dashboard image and the module's clock. Optional
  and off by default, because an upload takes ~35s and holds the link.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import tomllib
from pathlib import Path

from . import hid as rawhid
from .stats import Sampler

log = logging.getLogger("zoom75")

CONFIG_PATH = Path(os.environ.get("ZOOM75_CONFIG", "/etc/zoom75/config.toml"))

DEFAULTS: dict = {
    "hid": {
        "enabled": True,
        "interval": 2.0,
        "clock_interval": 300.0,
        "cpu": True,
        "gpu": True,
        "fan": True,
        "network": True,
        "clock": True,
    },
    "weather": {"enabled": False, "latitude": 51.5072, "longitude": -0.1276, "interval": 1800.0},
    "network": {"interface": ""},
    "ble": {"enabled": False, "dashboard_interval": 300.0, "clock_interval": 21600.0},
}


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read the root-owned config. A missing file is fine; a malformed one is
    not, because silently falling back would hide a bad edit."""
    if not path.exists():
        log.warning("no config at %s, using defaults", path)
        return _merge(DEFAULTS, {})
    with path.open("rb") as fh:
        return _merge(DEFAULTS, tomllib.load(fh))


class HidFeed:
    """Pushes sensor values over USB. Survives the keyboard being unplugged."""

    def __init__(self, cfg: dict, sampler: Sampler):
        self.cfg = cfg
        self.sampler = sampler
        self.device: rawhid.RawHidDevice | None = None
        self._warned = False
        self._last_error: str | None = None

    def _ensure(self) -> bool:
        if self.device is not None and Path(self.device.path).exists():
            return True
        try:
            self.device = rawhid.find_device()
            if not self._warned and self._last_error is None:
                log.info("raw-HID device %s", self.device.path)
            self._warned = False
            return True
        except rawhid.HidError as e:
            if not self._warned:
                log.warning("%s", e)
                self._warned = True
            self.device = None
            return False

    def _write(self, report: bytes):
        assert self.device is not None
        self.device.write(report)

    def _fail(self, what: str, err: Exception):
        """Log a write failure once per distinct cause, not once per cycle.

        Keyed on the error alone: the same permission problem surfacing from
        the sensor and clock tasks is one fault, not two.
        """
        cause = str(err)
        if cause != self._last_error:
            log.warning("%s: %s", what, cause)
            self._last_error = cause
        self.device = None

    def _ok(self):
        if self._last_error is not None:
            log.info("hid writes recovered")
            self._last_error = None

    def push_sensors(self):
        if not self._ensure():
            return
        s = self.sampler.sample()
        hid_cfg = self.cfg["hid"]
        cpu_t = getattr(s.cpu, "temp", None) or 0
        gpu_t = int(s.gpu.detail.split("\u00b0")[0]) if (s.gpu and s.gpu.detail) else 0
        net = int((s.rx_bps + s.tx_bps) / 2)
        try:
            if self.device is not None and self.device.device.hid_style == "bundled":
                self._write(rawhid.report_bundle(
                    int(cpu_t), gpu_t, 0, s.fan[0] if s.fan else 0, net))
                self._ok()
                return
            if hid_cfg["cpu"]:
                temp = getattr(s.cpu, "temp", None)
                if temp:
                    self._write(rawhid.report_cpu_temp(int(temp)))
            if hid_cfg["gpu"] and s.gpu and s.gpu.detail:
                self._write(rawhid.report_gpu_temp(int(s.gpu.detail.split("°")[0])))
            if hid_cfg["fan"] and s.fan:
                self._write(rawhid.report_fan_rpm(s.fan[0]))
            if hid_cfg["network"]:
                # The vendor app averages the two directions; match it.
                self._write(rawhid.report_net_speed(net))
            self._ok()
        except (rawhid.HidError, OSError) as e:
            self._fail("sensor push failed", e)

    def push_clock(self):
        if not self._ensure() or not self.cfg["hid"]["clock"]:
            return
        try:
            self._write(rawhid.report_time(dt.datetime.now()))
        except (rawhid.HidError, OSError) as e:
            self._fail("clock push failed", e)

    def push_weather(self, icon: int, celsius: int):
        if not self._ensure():
            return
        try:
            self._write(rawhid.report_weather(icon, celsius))
            log.info("weather pushed: icon %d, %d C", icon, celsius)
        except (rawhid.HidError, OSError) as e:
            self._fail("weather push failed", e)


async def fetch_weather(lat: float, lon: float) -> tuple[int, int] | None:
    """Current conditions from Open-Meteo. No API key, no account."""
    import json
    import urllib.request

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&timezone=auto"
    )

    def _get():
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)["current"]

    try:
        cur = await asyncio.to_thread(_get)
    except Exception as e:  # network is best-effort
        log.warning("weather fetch failed: %s", e)
        return None
    hour = int(cur["time"][11:13])
    icon = rawhid.wmo_to_icon(int(cur["weather_code"]), is_night=hour < 6 or hour >= 19)
    return icon, round(float(cur["temperature_2m"]))


async def _every(interval: float, fn, *args):
    """Run fn on a fixed period, never letting one failure kill the loop."""
    while True:
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("task %s failed", getattr(fn, "__name__", fn))
        await asyncio.sleep(interval)


async def run(cfg: dict):
    sampler = Sampler(cfg["network"]["interface"] or None)
    sampler.sample()  # prime the counters so the first push has real rates
    tasks = []

    if cfg["hid"]["enabled"]:
        feed = HidFeed(cfg, sampler)
        tasks.append(_every(cfg["hid"]["interval"], feed.push_sensors))
        if cfg["hid"]["clock"]:
            tasks.append(_every(cfg["hid"]["clock_interval"], feed.push_clock))

        if cfg["weather"]["enabled"]:
            async def weather_tick():
                got = await fetch_weather(
                    cfg["weather"]["latitude"], cfg["weather"]["longitude"]
                )
                if got:
                    feed.push_weather(*got)

            tasks.append(_every(cfg["weather"]["interval"], weather_tick))

    if cfg["ble"]["enabled"]:
        tasks.append(_every(cfg["ble"]["dashboard_interval"], _ble_dashboard, sampler))
        tasks.append(_every(cfg["ble"]["clock_interval"], _ble_clock))

    if not tasks:
        log.error("nothing enabled in the config; exiting")
        return 1
    log.info("started with %d task(s)", len(tasks))
    await asyncio.gather(*tasks)
    return 0


async def _ble_dashboard(sampler: Sampler):
    from . import dashboard, image as im
    from .client import Zoom75Screen

    screen = await Zoom75Screen.find(timeout=15)
    await screen.connect()
    try:
        await screen.upload(im.to_rgb565(dashboard.render(sampler.sample())))
        log.info("ble dashboard updated")
    finally:
        await screen.disconnect()


async def _ble_clock():
    from .client import Zoom75Screen

    screen = await Zoom75Screen.find(timeout=15)
    await screen.connect()
    try:
        await screen.sync_time()
        log.info("ble clock synced")
    finally:
        await screen.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s"
    )  # journald adds its own timestamps
    cfg = load_config()
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
