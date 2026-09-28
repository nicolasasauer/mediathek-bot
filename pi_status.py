"""
System-Status und Überwachung für den Raspberry Pi.

Liest alles direkt aus /proc, /sys und ein paar Kommandos (vcgencmd, systemctl,
docker), damit keine Extra-Pakete nötig sind. Alles ist fehlertolerant: Was
nicht lesbar ist, wird einfach weggelassen.
"""
import asyncio
import os
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------- Konfiguration
TEMP_WARN = float(os.environ.get("TEMP_WARN", "75"))          # °C
DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "10"))  # % frei
SERVICES = [s.strip() for s in os.environ.get("SERVICES", "").split(",") if s.strip()]
DOCKER_CONTAINERS = [
    c.strip() for c in os.environ.get("DOCKER_CONTAINERS", "").split(",") if c.strip()
]
# Web-Checks "name=url": erkennt Dienste, die zwar laufen, aber nicht mehr antworten
HTTP_CHECKS = dict(
    item.split("=", 1)
    for item in os.environ.get("HTTP_CHECKS", "").split(",")
    if "=" in item
)
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "10"))
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


_last_restarts: dict[str, int] = {}


async def container_info(name: str, track: bool = True) -> tuple[bool, str, int]:
    """(ok, beschreibung, neue_neustarts_seit_letzter_prüfung).

    track=False (für /status) verändert den Zähler nicht, damit die Überwachung
    einen Absturz trotzdem noch meldet.
    """
    out = await run(
        "docker", "inspect", "-f",
        "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}",
        name,
    )
    if not out or out.count("|") != 2:
        return False, "unbekannt", 0
    status, health, restarts = out.split("|")
    try:
        count = int(restarts)
    except ValueError:
        count = 0
    new = max(0, count - _last_restarts.get(name, count))
    if track:
        _last_restarts[name] = count
    desc = status + (f", {health}" if health else "")
    ok = status == "running" and health != "unhealthy" and new == 0
    if new:
        desc += f", {new}× abgestürzt und neu gestartet"
    return ok, desc, new


async def http_check(url: str) -> tuple[bool, str]:
    import httpx  # kommt mit python-telegram-bot

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False) as client:
            r = await client.get(url, follow_redirects=True)
        if r.status_code < 500:
            return True, f"antwortet ({r.status_code})"
        return False, f"Fehler {r.status_code}"
    except httpx.TimeoutException:
        return False, f"keine Antwort nach {HTTP_TIMEOUT:.0f} s"
    except httpx.HTTPError as e:
        return False, f"nicht erreichbar ({type(e).__name__})"


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

    if SERVICES or DOCKER_CONTAINERS or HTTP_CHECKS:
        lines.append("")
        for s in SERVICES:
            st = await service_state(s)
            lines.append(f"{'✅' if st == 'active' else '❌'} {s}: {st}")
        for c in DOCKER_CONTAINERS:
            ok, desc, _ = await container_info(c, track=False)
            lines.append(f"{'✅' if ok else '❌'} {c} (Docker): {desc}")
        for name, url in HTTP_CHECKS.items():
            ok, desc = await http_check(url)
            lines.append(f"{'✅' if ok else '❌'} {name} (Web): {desc}")

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
        ok, desc, _ = await container_info(c)
        if not ok:
            problems[f"docker:{c}"] = f"❌ Container {c}: {desc}"

    for name, url in HTTP_CHECKS.items():
        ok, desc = await http_check(url)
        if not ok:
            problems[f"http:{name}"] = f"❌ {name} hängt: {desc}"

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


MARKER = Path(
    os.environ.get("SHUTDOWN_MARKER", "~/.config/mediathek-bot/clean_shutdown")
).expanduser()


async def shutdown_kind() -> str | None:
    """'reboot', 'poweroff' oder None (nur der Bot wird beendet)."""
    jobs = await run("systemctl", "list-jobs", "--no-legend") or ""
    if "reboot.target" in jobs or "kexec.target" in jobs:
        return "reboot"
    if "poweroff.target" in jobs or "halt.target" in jobs:
        return "poweroff"
    if (await run("systemctl", "is-system-running")) == "stopping":
        return "poweroff"
    return None


def mark_clean_shutdown(kind: str) -> None:
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(f"{kind} {time.time():.0f}")
    except OSError:
        pass


def boot_note() -> str | None:
    """
    Nach dem Hochfahren: sauberer Neustart oder unerwartet weg gewesen (Stromausfall)?
    Der Marker wird beim geordneten Herunterfahren geschrieben und hier wieder gelöscht.
    """
    up = uptime_seconds()
    try:
        marker = MARKER.read_text().split()
        MARKER.unlink()
    except (OSError, IndexError):
        marker = []
    if up is None or up >= 600:
        return None  # Pi läuft schon länger, nur der Bot wurde neu gestartet
    if marker:
        down_for = ""
        try:
            gone = time.time() - float(marker[1]) - up
            if gone > 0:
                down_for = f", war {fmt_duration(gone)} aus"
        except (IndexError, ValueError):
            pass
        return f"🟢 Pi ist wieder da (läuft seit {fmt_duration(up)}{down_for})."
    return (
        f"⚠️ Pi wurde unerwartet neu gestartet (läuft seit {fmt_duration(up)}).\n"
        "Kein geordnetes Herunterfahren – evtl. Stromausfall oder Absturz."
    )


if __name__ == "__main__":  # schneller Test: python pi_status.py
    print(asyncio.run(status_text()))
    print(asyncio.run(current_problems()) or "Keine Probleme")
