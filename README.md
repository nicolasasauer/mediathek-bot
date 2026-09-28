# Mediathek-Bot für den Raspberry Pi

Link per Telegram schicken → Pi lädt mit yt-dlp → Syncthing bringt's aufs Handy.

Gedacht für instabiles Internet unterwegs (z.B. im Zug): Die Mediathek-Apps brechen
Downloads ab und starten von vorne. Der Pi zuhause lädt stattdessen über eine stabile
Leitung, und Syncthing überträgt blockweise und setzt nach Abbrüchen fort.

**Funktionen**
- Link teilen (z.B. aus der ZDF-App) → Download mit Fortschritt im Chat
- Warteschlange für mehrere Links (`/queue`)
- Videos anzeigen und löschen per Button (`/list`)
- Nur freigeschaltete Telegram-User dürfen den Bot nutzen
- Funktioniert mit allem, was [yt-dlp](https://github.com/yt-dlp/yt-dlp) kann (ZDF, ARD, arte, …)

## Setup

### 1. Bot bei Telegram anlegen
1. In Telegram **@BotFather** suchen (blauer Haken) → Start
2. `/newbot` schicken
3. Anzeigenamen wählen, z.B. `Mediathek Pi`
4. Usernamen wählen, er muss auf `bot` enden, z.B. `mein_mediathek_bot`
5. Token kopieren (`7123456789:AAH...`). Er ist das Passwort des Bots, also nirgends teilen!

### 2. Repo auf den Pi holen und installieren
```bash
git clone https://github.com/nicolasasauer/mediathek-bot.git ~/mediathek-bot
cd ~/mediathek-bot
sudo apt update && sudo apt install -y ffmpeg python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Falls dein Benutzer **nicht** `pi` heißt:
```bash
sed -i "s/User=pi/User=$USER/; s|/home/pi|$HOME|g" mediathek-bot.service .env.example
```

### 3. Konfiguration
```bash
cp .env.example .env
nano .env        # Token bei TELEGRAM_BOT_TOKEN eintragen, ALLOWED_USER_IDS vorerst leer
```

### 4. Erster Start und eigene User-ID
```bash
set -a; source .env; set +a
venv/bin/python mediathek_bot.py
```
In Telegram den Bot öffnen (`t.me/<username>`) und **Start** tippen. Er antwortet mit
„Deine User-ID: 12345678“. Dann mit Strg+C stoppen, die ID in `.env` bei
`ALLOWED_USER_IDS=` eintragen, neu starten und einen Link zum Testen schicken.

### 5. Als Dienst einrichten
```bash
sudo cp mediathek-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediathek-bot
systemctl status mediathek-bot       # "active (running)"
journalctl -u mediathek-bot -f       # Logs
```

### 6. Syncthing: Pi ↔ Handy
Auf dem Pi:
```bash
sudo apt install -y syncthing
sudo systemctl enable --now syncthing@$USER
```
Die Weboberfläche vom PC aus per SSH-Tunnel öffnen:
```bash
ssh -L 8384:localhost:8384 <user>@<pi-hostname>
# dann im Browser: http://localhost:8384
```
Am Handy **Syncthing-Fork** installieren (Play Store / F-Droid).

Koppeln:
1. In der Handy-App die Geräte-ID anzeigen lassen.
2. Am Pi unter „Gerät hinzufügen“ die ID eintragen.
3. Am Pi unter „Ordner hinzufügen“ als Pfad `~/Sync/Mediathek` (bzw. dein `DOWNLOAD_DIR`) angeben und im Tab „Teilen“ fürs Handy freigeben.
4. Am Handy die Anfrage annehmen und einen Zielordner wählen.

### 7. Optional: Tailscale für unterwegs
Ohne Tailscale synct das Handy nur im Heim-WLAN. Wenn du auch unterwegs syncen willst:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
Dazu die Tailscale-App aufs Handy, mit demselben Account.

## Nutzung
- In der ZDF-App auf *Teilen* → *Telegram* → Bot tippen, oder den Link einfach reinkopieren
- `/queue` zeigt, was gerade läuft
- `/list` zeigt alle Videos mit Größe; antippen → bestätigen → gelöscht (Syncthing löscht es dann auch am Handy)
- Gesehene Folgen am Handy löschen → verschwinden per Sync auch auf dem Pi

## Update / Wartung
```bash
cd ~/mediathek-bot && git pull
venv/bin/pip install -U -r requirements.txt   # v.a. yt-dlp aktuell halten
sudo systemctl restart mediathek-bot
```
Mediatheken ändern gern mal was. Wenn Downloads plötzlich scheitern, hilft meist ein yt-dlp-Update.

## Hinweis
Nur für den privaten Gebrauch frei zugänglicher Mediathek-Inhalte.

## Lizenz
MIT, siehe [LICENSE](LICENSE).
