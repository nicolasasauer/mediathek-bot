#!/usr/bin/env python3
"""
Mediathek-Download-Bot für den Raspberry Pi.

Schick dem Bot einen Link (ZDF, ARD, arte, ... alles was yt-dlp kann),
der Pi lädt das Video in DOWNLOAD_DIR (z.B. einen Syncthing-Ordner)
und meldet Fortschritt + Fertig im Chat.

Konfiguration über Umgebungsvariablen (siehe .env.example).
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

import camera
import pi_status
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- Konfiguration
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "~/Sync/Mediathek")).expanduser()
# Temp-Ordner bewusst AUSSERHALB des Sync-Ordners, damit Syncthing keine .part-Dateien überträgt
TEMP_DIR = Path(os.environ.get("TEMP_DIR", "~/.cache/mediathek-bot")).expanduser()
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "720"))  # 720p spart Platz am Handy
SUBTITLES = os.environ.get("SUBTITLES", "0") == "1"
PROGRESS_INTERVAL = 8  # Sekunden zwischen Fortschritts-Updates
# Bei diesen Prozentwerten kommt eine eigene Nachricht (mit Benachrichtigung)
MILESTONES = [
    int(x) for x in os.environ.get("MILESTONES", "25,50,75").split(",") if x.strip()
]
PCT_RE = re.compile(r"([\d.]+)%")

# yt-dlp bevorzugt aus dem gleichen venv wie dieses Python nehmen
_venv_ytdlp = Path(sys.executable).parent / "yt-dlp"
YTDLP = str(_venv_ytdlp) if _venv_ytdlp.exists() else (shutil.which("yt-dlp") or "yt-dlp")

URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mediathek-bot")

download_queue: asyncio.Queue = asyncio.Queue()
state = {"current": None}  # Titel des laufenden Downloads


# ---------------------------------------------------------------- Hilfsfunktionen
def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USERS


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def build_command(url: str) -> list[str]:
    fmt = f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b"
    cmd = [
        YTDLP,
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-P", f"home:{DOWNLOAD_DIR}",
        "-P", f"temp:{TEMP_DIR}",
        "-o", "%(title).120B [%(id)s].%(ext)s",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--no-playlist",
        "--newline",
        "--progress",
        "--no-simulate",
        "--print", "before_dl:TITLE %(title)s",
        "--print", "after_move:FILE %(filepath)s",
        "--progress-template",
        "download:PROG %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
    ]
    if SUBTITLES:
        cmd += ["--write-subs", "--sub-langs", "de.*", "--embed-subs"]
    cmd.append(url)
    return cmd


async def safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except BadRequest as e:  # "message is not modified" o.ä. ignorieren
        if "not modified" not in str(e).lower():
            log.warning("Edit fehlgeschlagen: %s", e)


# ---------------------------------------------------------------- Download
async def run_download(bot, chat_id: int, url: str) -> None:
    status = await bot.send_message(chat_id, f"⏳ Starte Download…\n{url}")
    title, filepath = url, None
    last_update = 0.0
    reached: set[int] = set()  # Meilensteine nur einmal melden (yt-dlp lädt Video + Audio getrennt)
    tail: list[str] = []

    proc = await asyncio.create_subprocess_exec(
        *build_command(url),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        tail = (tail + [line])[-5:]

        if line.startswith("TITLE "):
            title = line[6:]
            state["current"] = title
            await safe_edit(status, f"⬇️ {title}\nwird geladen…")
        elif line.startswith("FILE "):
            filepath = line[5:]
        elif line.startswith("PROG "):
            m = PCT_RE.search(line)
            if m:
                pct_val = float(m.group(1))
                new = [ms for ms in MILESTONES if pct_val >= ms and ms not in reached]
                if new:
                    reached.update(new)
                    await bot.send_message(chat_id, f"⏳ {max(new)} % · {title}")
            now = time.monotonic()
            if now - last_update >= PROGRESS_INTERVAL:
                last_update = now
                pct, speed, eta = (line[5:].split("|") + ["", "", ""])[:3]
                await safe_edit(
                    status,
                    f"⬇️ {title}\n{pct.strip()} · {speed.strip()} · noch {eta.strip()}",
                )

    rc = await proc.wait()
    state["current"] = None

    if rc == 0 and filepath and Path(filepath).exists():
        size = human_size(Path(filepath).stat().st_size)
        await safe_edit(status, f"⬇️ {title}\n100 %")
        await bot.send_message(
            chat_id, f"✅ Fertig: {title}\n{size} · wird jetzt aufs Handy synchronisiert"
        )
    else:
        err = "\n".join(tail) or "unbekannter Fehler"
        await safe_edit(status, f"⬇️ {title}\nabgebrochen")
        await bot.send_message(chat_id, f"❌ Fehlgeschlagen: {title}\n\n{err[-800:]}")


async def worker(app: Application) -> None:
    """Arbeitet die Warteschlange nacheinander ab (schont den Pi)."""
    while True:
        chat_id, url = await download_queue.get()
        try:
            await run_download(app.bot, chat_id, url)
        except Exception as e:  # noqa: BLE001
            log.exception("Download-Fehler")
            await app.bot.send_message(chat_id, f"❌ Fehler: {e}")
        finally:
            state["current"] = None
            download_queue.task_done()


# ---------------------------------------------------------------- Handler
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(update):
        await update.message.reply_text(
            f"Nicht freigeschaltet. Deine User-ID: {user.id}\n"
            "Trag sie in ALLOWED_USER_IDS ein und starte den Bot neu."
        )
        return
    await update.message.reply_text(
        "Schick mir einen Mediathek-Link (oder teile ihn aus der ZDF-App), "
        "ich lade ihn auf den Pi.\n\n/queue – Warteschlange anzeigen\n"
        "/list – Videos anzeigen & löschen\n"
        "/status – Zustand des Pi\n"
        "/foto – aktuelles Kamerabild\n"
        "/alarm an|aus – Bewegungsalarm schalten"
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    current = state["current"] or "–"
    await update.message.reply_text(
        f"Läuft gerade: {current}\nIn der Warteschlange: {download_queue.qsize()}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = await pi_status.status_text(extra_disks=[str(DOWNLOAD_DIR)])
    if state["current"]:
        text += f"\n\n⬇️ Lädt gerade: {state['current']}"
    await update.message.reply_text(text)


async def cmd_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if not camera.enabled():
        await update.message.reply_text("Keine Kamera eingerichtet (SNAPSHOT_URL in .env).")
        return
    try:
        img = await camera.snapshot()
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"📷 Kamera nicht erreichbar: {e}")
        return
    await update.message.reply_photo(img, caption="📷 Aktuelles Bild")


async def cmd_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("an", "on", "ein"):
        camera.set_alarm(True)
    elif arg in ("aus", "off"):
        camera.set_alarm(False)
    state_txt = "🔔 an" if camera.alarm_on() else "🔕 aus"
    await update.message.reply_text(
        f"Bewegungsalarm: {state_txt}\n\nUmschalten mit /alarm an oder /alarm aus"
    )


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3"}


def list_videos() -> list[Path]:
    files = [p for p in DOWNLOAD_DIR.glob("*") if p.is_file() and p.suffix in VIDEO_EXTS]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def file_key(path: Path) -> str:
    # Callback-Daten sind auf 64 Byte begrenzt -> kurzer Hash statt Dateiname
    return hashlib.sha1(path.name.encode()).hexdigest()[:12]


def find_by_key(key: str) -> Path | None:
    return next((p for p in list_videos() if file_key(p) == key), None)


def list_markup() -> tuple[str, InlineKeyboardMarkup | None]:
    files = list_videos()
    if not files:
        return "📂 Keine Videos auf dem Pi.", None
    total = sum(p.stat().st_size for p in files)
    buttons = [
        [InlineKeyboardButton(
            f"🗑 {p.stem[:45]} ({human_size(p.stat().st_size)})",
            callback_data=f"del:{file_key(p)}",
        )]
        for p in files[:30]
    ]
    text = f"📂 {len(files)} Videos · {human_size(total)}\nAntippen zum Löschen:"
    return text, InlineKeyboardMarkup(buttons)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text, markup = list_markup()
    await update.message.reply_text(text, reply_markup=markup)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("Nicht erlaubt")
        return
    action, _, key = (query.data or "").partition(":")

    if action == "back":
        await query.answer()
        text, markup = list_markup()
        await query.edit_message_text(text, reply_markup=markup)
        return

    path = find_by_key(key)
    if path is None:
        await query.answer("Datei gibt's nicht mehr")
        text, markup = list_markup()
        await query.edit_message_text(text, reply_markup=markup)
        return

    if action == "del":  # Rückfrage
        await query.answer()
        await query.edit_message_text(
            f"Wirklich löschen?\n{path.name}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Ja, löschen", callback_data=f"delok:{key}"),
                InlineKeyboardButton("↩️ Zurück", callback_data="back:"),
            ]]),
        )
    elif action == "delok":
        path.unlink(missing_ok=True)
        log.info("Gelöscht: %s", path.name)
        await query.answer("Gelöscht 🗑")
        text, markup = list_markup()
        await query.edit_message_text(f"🗑 {path.stem} gelöscht.\n\n{text}", reply_markup=markup)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text(
            f"Nicht freigeschaltet. Deine User-ID: {update.effective_user.id}"
        )
        return
    urls = URL_RE.findall(update.message.text or "")
    if not urls:
        await update.message.reply_text("Kein Link gefunden 🤔")
        return
    for url in urls:
        await download_queue.put((update.effective_chat.id, url))
    pos = download_queue.qsize()
    await update.message.reply_text(
        f"📥 {len(urls)} Link(s) eingereiht" + (f" (Position {pos})" if pos > 1 else "")
    )


async def post_init(app: Application) -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    app.bot_data["worker"] = asyncio.create_task(worker(app))

    async def notify(text: str) -> None:
        for uid in ALLOWED_USERS:  # private Chat-ID == User-ID
            try:
                await app.bot.send_message(uid, text)
            except Exception as e:  # noqa: BLE001
                log.warning("Benachrichtigung an %s fehlgeschlagen: %s", uid, e)

    app.bot_data["monitor"] = asyncio.create_task(
        pi_status.monitor_loop(notify, extra_disks=[str(DOWNLOAD_DIR)])
    )
    if camera.enabled():
        async def on_motion() -> None:
            try:
                img = await camera.snapshot()
            except Exception as e:  # noqa: BLE001
                await notify(f"🚨 Bewegung erkannt (kein Bild: {e})")
                return
            for uid in ALLOWED_USERS:
                try:
                    await app.bot.send_photo(uid, img, caption="🚨 Bewegung erkannt")
                except Exception as e:  # noqa: BLE001
                    log.warning("Foto an %s fehlgeschlagen: %s", uid, e)

        app.bot_data["webhook"] = await camera.start_webhook(on_motion)

    note = pi_status.boot_note()
    if note:
        await notify(note)
    log.info("Bot läuft. Ziel: %s, max. %sp", DOWNLOAD_DIR, MAX_HEIGHT)


async def post_stop(app: Application) -> None:
    """Läuft beim Beenden, solange der Bot noch senden kann."""
    monitor = app.bot_data.get("monitor")
    if monitor:
        monitor.cancel()  # keine Fehlalarme, während Dienste herunterfahren
    kind = await pi_status.shutdown_kind()
    if kind is None:
        return  # nur der Bot wird neu gestartet (z.B. nach git pull)
    pi_status.mark_clean_shutdown(kind)
    text = "🔄 Pi startet neu …" if kind == "reboot" else "⏻ Pi fährt herunter …"
    for uid in ALLOWED_USERS:
        try:
            await asyncio.wait_for(app.bot.send_message(uid, text), 10)
        except Exception as e:  # noqa: BLE001
            log.warning("Abschiedsnachricht fehlgeschlagen: %s", e)


async def post_shutdown(app: Application) -> None:
    """Worker sauber beenden (vermeidet 'Event loop is closed' bei Strg+C)."""
    server = app.bot_data.get("webhook")
    if server:
        server.close()
        await server.wait_closed()
    for name in ("worker", "monitor"):
        task = app.bot_data.get(name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def main() -> None:
    if not ALLOWED_USERS:
        log.warning("ALLOWED_USER_IDS ist leer – Bot antwortet nur mit der User-ID.")
    app = Application.builder().token(TOKEN).post_init(post_init).post_stop(post_stop).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("foto", cmd_foto))
    app.add_handler(CommandHandler("alarm", cmd_alarm))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
