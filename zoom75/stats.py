"""System metric collection for the stats dashboard.

Everything here degrades gracefully: a machine with no NVIDIA GPU, no k10temp
or no default route still produces a usable sample rather than raising.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Metric:
    label: str
    percent: float | None = None
    value: str = ""
    detail: str = ""


@dataclass
class Sample:
    host: str
    when: time.struct_time
    cpu: Metric
    mem: Metric
    gpu: Metric | None
    net_iface: str = ""
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    fan: tuple[int, str] | None = None
    extra: dict = field(default_factory=dict)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cpu_jiffies() -> tuple[int, int]:
    """Return (busy, total) from the aggregate line of /proc/stat."""
    line = Path("/proc/stat").read_text().split("\n", 1)[0]
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    total = sum(parts)
    return total - idle, total


def _cpu_temp() -> float | None:
    """Prefer the AMD Tctl / Intel package sensor, else any coretemp input."""
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        name = _read(str(hwmon / "name"))
        if name not in ("k10temp", "coretemp", "zenpower"):
            continue
        for temp in sorted(hwmon.glob("temp*_input")):
            label = _read(str(temp).replace("_input", "_label")) or ""
            if label in ("Tctl", "Tdie", "Package id 0") or not label:
                raw = _read(str(temp))
                if raw:
                    return int(raw) / 1000
    return None


def fan_rpm() -> tuple[int, str] | None:
    """Highest fan reading exposed via hwmon, with its label.

    Prefers a real fan over a pump: on a liquid-cooled box the only sensor the
    kernel exposes may be the AIO pump, which is still a useful number but
    should be labelled as what it is.
    """
    best = None
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        chip = _read(str(hwmon / "name")) or "?"
        for node in sorted(hwmon.glob("fan*_input")):
            raw = _read(str(node))
            if not raw or int(raw) <= 0:
                continue
            label = _read(str(node).replace("_input", "_label")) or f"{chip} {node.name}"
            rpm = int(raw)
            is_pump = "pump" in label.lower()
            rank = (0 if is_pump else 1, rpm)
            if best is None or rank > best[0]:
                best = (rank, (rpm, label))
    return best[1] if best else None


def _mem() -> Metric:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0])  # kB
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", total)
    used = total - avail
    pct = (used / total * 100) if total else 0.0
    return Metric("MEM", pct, f"{used / 1048576:.1f}G", f"{used / 1048576:.1f}/{total / 1048576:.0f}G")


_GPU_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


def _gpu() -> Metric | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True,
        ).stdout.strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError, IndexError):
        return None
    try:
        util, used, total, temp, power = [p.strip() for p in out.split(",")]
        return Metric(
            "GPU",
            float(util),
            f"{float(used) / 1024:.1f}G",
            f"{int(float(temp))}°  {float(power):.0f}W",
        )
    except ValueError:
        return None


def _default_iface() -> str | None:
    """The interface carrying the default route, per /proc/net/route."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 1 and f[1] == "00000000":
                return f[0]
    except OSError:
        pass
    return None


def _net_counters(iface: str) -> tuple[int, int]:
    base = f"/sys/class/net/{iface}/statistics"
    return int(_read(f"{base}/rx_bytes") or 0), int(_read(f"{base}/tx_bytes") or 0)


def human_rate(bps: float) -> str:
    for unit, div in (("GB/s", 1 << 30), ("MB/s", 1 << 20), ("KB/s", 1 << 10)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}"
    return f"{bps:.0f} B/s"


class Sampler:
    """Holds the previous counters so rates can be derived between calls."""

    def __init__(self, iface: str | None = None):
        self.iface = iface or _default_iface() or ""
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_net: tuple[int, int] | None = None
        self._prev_t: float | None = None

    def sample(self, settle: float = 0.4) -> Sample:
        now = time.time()
        cpu_now = _cpu_jiffies()
        net_now = _net_counters(self.iface) if self.iface else (0, 0)

        # On the first call there is no baseline, so take one over a short window.
        if self._prev_cpu is None:
            time.sleep(settle)
            prev_cpu, prev_net, dt = cpu_now, net_now, settle
            cpu_now = _cpu_jiffies()
            net_now = _net_counters(self.iface) if self.iface else (0, 0)
        else:
            prev_cpu, prev_net = self._prev_cpu, self._prev_net  # type: ignore[assignment]
            dt = max(now - (self._prev_t or now), 1e-3)

        d_busy = cpu_now[0] - prev_cpu[0]
        d_total = cpu_now[1] - prev_cpu[1]
        cpu_pct = (d_busy / d_total * 100) if d_total > 0 else 0.0

        rx_bps = max(0.0, (net_now[0] - prev_net[0]) / dt)
        tx_bps = max(0.0, (net_now[1] - prev_net[1]) / dt)

        self._prev_cpu, self._prev_net, self._prev_t = cpu_now, net_now, time.time()

        temp = _cpu_temp()
        cpu = Metric("CPU", cpu_pct, f"{cpu_pct:.0f}%", f"{temp:.0f}°" if temp else "")
        cpu.temp = temp  # type: ignore[attr-defined]
        return Sample(
            fan=fan_rpm(),
            host=socket.gethostname(),
            when=time.localtime(),
            cpu=cpu,
            mem=_mem(),
            gpu=_gpu(),
            net_iface=self.iface,
            rx_bps=rx_bps,
            tx_bps=tx_bps,
        )
