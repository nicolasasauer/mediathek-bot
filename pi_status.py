"""
System-Status und Überwachung für den Raspberry Pi.

Liest alles direkt aus /proc, /sys und ein paar Kommandos (vcgencmd, systemctl,
docker), damit keine Extra-Pakete nötig sind. Alles ist fehlertolerant: Was
nicht lesbar ist, wird einfach weggelassen.
"""
import asyncio
import os
import shutil
from pathlib import Path

# ---------------------------------------------------------------- Konfiguration
TEMP_WARN = float(os.environ.get("TEMP_WARN", "75"))          # °C
DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "10"))  # % frei
SERVICES = [s.strip() for s in os.environ.get("SERVICES", "").split(",") if s.strip()]
DOCKER_CONTAINERS = [
    c.strip() for c in os.environ.get("DOCKER_CONTAINERS", "").split(",") if c.strip()
]
DISK_PATHS = [
    p.strip() for p in os.environ.get("DISK_PATHS", "/").split(",") if p.strip()
]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # Sekunden

THROTTLE_FLAGS = {
    0: "Unterspannung",
    1: "Takt begrenzt",
    2: "gedrosselt",
    3: "Temperaturlimit",
}


# ---------------------------------------------------------------- Einzelwerte
def cpu_temp() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def uptime_seconds() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def fmt_duration(seconds: float) -> str:
    d, rest = divmod(int(seconds), 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d} T {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def load_avg() -> str | None:
    try:
        return " / ".join(Path("/proc/loadavg").read_text().split()[:3])
    except OSError:
        return None


def memory() -> tuple[int, int] | None:
    """(benutzt, gesamt) in Bytes."""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            info[key] = int(val.split()[0]) * 1024
        total = info["MemTotal"]
        return total - info["MemAvailable"], total
    except (OSError, KeyError, ValueError):
        return None


def disk(path: str) -> tuple[int, int, int] | None:
    """(benutzt, gesamt, frei) in Bytes."""
    try:
        u = shutil.disk_usage(path)
        return u.used, u.total, u.free
    except OSError:
        return None


async def run(*cmd: str, timeout: float = 5) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode().strip()
    except (OSError, asyncio.TimeoutError):
        return None


async def throttled() -> tuple[list[str], list[str]] | None:
    """(aktuell, seit_boot) als Liste von Klartext-Flags. None wenn vcgencmd fehlt."""
    out = await run("vcgencmd", "get_throttled")
    if not out or "=" not in out:
        return None
    try:
        val = int(out.split("=")[1], 16)
    except ValueError:
        return None
    now = [name for bit, name in THROTTLE_FLAGS.items() if val & (1 << bit)]
    past = [name for bit, name in THROTTLE_FLAGS.items() if val & (1 << (bit + 16))]
    return now, past


async def service_state(name: str) -> str:
    return await run("systemctl", "is-active", name) or "unbekannt"


async def container_state(name: str) -> str:
    return await run("docker", "inspect", "-f", "{{.State.Status}}", name) or "unbekannt"


# ---------------------------------------------------------------- Status-Text
def _size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


async def status_text(extra_disks: list[str] | None = None) -> str:
    lines = ["🖥 Pi-Status"]

    t = cpu_temp()
    if t is not None:
        icon = "🔥" if t >= TEMP_WARN else "🌡"
        lines.append(f"{icon} CPU: {t:.1f} °C")

    thr = await throttled()
    if thr is not None:
        now, past = thr
        if now:
            lines.append(f"⚠️ Jetzt: {', '.join(now)}")
        elif past:
            lines.append(f"⚡ Seit Boot aufgetreten: {', '.join(past)}")
        else:
            lines.append("⚡ Stromversorgung ok")

    la = load_avg()
    if la:
        lines.append(f"📊 Last: {la}")

    mem = memory()
    if mem:
        used, total = mem
        lines.append(f"🧠 RAM: {_size(used)} / {_size(total)} ({used / total:.0%})")

    for path in dict.fromkeys(DISK_PATHS + (extra_disks or [])):
        d = disk(path)
        if d:
            used, total, free = d
            icon = "🔴" if free / total * 100 < DISK_WARN_PCT else "💾"
            lines.append(f"{icon} {path}: {_size(free)} frei von {_size(total)}")

    up = uptime_seconds()
    if up is not None:
        lines.append(f"⏱ Läuft seit {fmt_duration(up)}")

    if SERVICES or DOCKER_CONTAINERS:
        lines.append("")
        for s in SERVICES:
            st = await service_state(s)
            lines.append(f"{'✅' if st == 'active' else '❌'} {s}: {st}")
        for c in DOCKER_CONTAINERS:
            st = await container_state(c)
            lines.append(f"{'✅' if st == 'running' else '❌'} {c} (Docker): {st}")

    return "\n".join(lines)


# ---------------------------------------------------------------- Überwachung
async def current_problems(extra_disks: list[str] | None = None) -> dict[str, str]:
    """Liefert {schlüssel: beschreibung} für alles, was gerade nicht ok ist."""
    problems: dict[str, str] = {}

    t = cpu_temp()
    if t is not None and t >= TEMP_WARN:
        problems["temp"] = f"🔥 CPU-Temperatur hoch: {t:.1f} °C"

    thr = await throttled()
    if thr is not None and thr[0]:
        problems["throttle"] = f"⚡ {', '.join(thr[0])} (Netzteil prüfen!)"

    for path in dict.fromkeys(DISK_PATHS + (extra_disks or [])):
        d = disk(path)
        if d:
            _, total, free = d
            if free / total * 100 < DISK_WARN_PCT:
                problems[f"disk:{path}"] = f"💾 Wenig Speicher auf {path}: nur {_size(free)} frei"

    for s in SERVICES:
        st = await service_state(s)
        if st != "active":
            problems[f"svc:{s}"] = f"❌ Dienst {s} ist {st}"

    for c in DOCKER_CONTAINERS:
        st = await container_state(c)
        if st != "running":
            problems[f"docker:{c}"] = f"❌ Container {c} ist {st}"

    return problems


async def monitor_loop(notify, extra_disks: list[str] | None = None) -> None:
    """
    Prüft regelmäßig und ruft notify(text) auf – jedes Problem nur einmal,
    und eine Entwarnung, sobald es wieder ok ist.
    """
    active: dict[str, str] = {}
    while True:
        try:
            problems = await current_problems(extra_disks)
            for key, text in problems.items():
                if key not in active:
                    await notify(f"⚠️ Warnung\n{text}")
            for key, text in active.items():
                if key not in problems:
                    await notify(f"✅ Wieder ok\n{text.split(' ', 1)[1]}")
            active = problems
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 – Überwachung darf nie abstürzen
            print(f"Monitor-Fehler: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


def boot_note() -> str | None:
    """Hinweis, falls der Pi gerade erst hochgefahren ist (z.B. nach Stromausfall)."""
    up = uptime_seconds()
    if up is not None and up < 600:
        return f"🔄 Der Pi wurde neu gestartet (läuft seit {fmt_duration(up)})."
    return None


if __name__ == "__main__":  # schneller Test: python pi_status.py
    print(asyncio.run(status_text()))
    print(asyncio.run(current_problems()) or "Keine Probleme")
