"""PS5 Pro Lager-Watcher fuer PlayStation Direct Deutschland.

Prueft zwischen START und ENDE (Standard 09:00-11:00) alle 20 Sekunden, ob die
PS5 Pro auf playstation.com/de-de (frueher direct.playstation.com) kaufbar ist.
Wenn ja: Windows-Benachrichtigung + Alarmton + Produktseite im Browser oeffnen
(+ optional Push aufs Handy ueber ntfy.sh).

Aufruf:
  python ps5pro_watch.py            # laeuft im Zeitfenster aus config.json
  python ps5pro_watch.py --once     # nur ein einziger Check (zum Testen)
  python ps5pro_watch.py --now      # sofort starten, Zeitfenster ignorieren (bis ENDE)
  python ps5pro_watch.py --test-alarm  # Alarm einmal ausloesen
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, time as dtime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "ps5pro_watch.log"
CONFIG = HERE / "config.json"

DEFAULTS = {
    "url": "https://www.playstation.com/de-de/ps5/ps5-pro/buy-now/",
    "start": "09:00",
    "end": "11:00",
    "interval_seconds": 10,
    "open_browser": True,
    "ntfy_topic": "",  # z.B. "ps5pro-geheim-1234" -> Push aufs Handy mit der ntfy-App
    "alarm_repeat_minutes": 5,  # bei weiterhin verfuegbar: Alarm hoechstens alle X Minuten
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


IS_CLOUD = bool(os.environ.get("CI"))  # GitHub Actions setzt CI=true

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # Windows ohne tzdata -> lokale Zeit (ist eh Berlin)
    BERLIN = None


def now():
    return datetime.now(BERLIN) if BERLIN else datetime.now()


def load_config():
    cfg = dict(DEFAULTS)
    for f in (CONFIG, HERE / "config.local.json"):  # config.local.json kommt nicht auf GitHub
        if f.exists():
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    if IS_CLOUD:
        cfg["open_browser"] = False
    return cfg


def log(msg):
    line = f"{now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.geturl(), r.read().decode("utf-8", errors="replace")


def check(url):
    """Gibt (status, details) zurueck. status: 'IN_STOCK', 'OUT_OF_STOCK', 'QUEUE', 'UNKNOWN'."""
    # Cache-Buster, damit kein CDN eine alte Version liefert
    sep = "&" if "?" in url else "?"
    code, final_url, html = fetch(f"{url}{sep}_={int(time.time())}")

    # Bei grossen Drops schickt Sony alle in eine Warteschlange (queue-it)
    if "queue" in final_url.lower() or "queue-it" in html[:20000].lower():
        return "QUEUE", f"Weiterleitung in Warteschlange: {final_url}"

    avail = re.search(r'"availability"\s*:\s*\\?"https?://schema\.org/(\w+)', html)
    cta = re.search(r'"activeCtaId"\s*:\s*\\?"([A-Z_]+)', html)
    avail = avail.group(1) if avail else None
    cta = cta.group(1) if cta else None
    sold_out_text = "Derzeit nicht vorrätig" in html or "Nicht zum Kauf erhältlich" in html
    details = f"HTTP {code}, availability={avail}, cta={cta}, ausverkauft-Text={sold_out_text}"

    if avail is None and cta is None:
        return "UNKNOWN", details + " (Seite sieht anders aus als erwartet)"
    if avail in ("InStock", "LimitedAvailability", "PreOrder", "OnlineOnly") or \
            (cta is not None and cta != "UNAVAILABLE"):
        return "IN_STOCK", details
    return "OUT_OF_STOCK", details


def toast(title, text):
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast scenario="alarm"><visual><binding template="ToastGeneric"><text>{title}</text><text>{text}</text></binding></visual><audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="true"/><actions><action content="OK" arguments="ok"/></actions></toast>')
$t = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
    except Exception as e:
        log(f"Toast fehlgeschlagen: {e}")


def beep():
    try:
        import winsound
        for _ in range(6):
            winsound.Beep(1500, 250)
            winsound.Beep(1000, 250)
    except Exception:
        pass


def ntfy(topic, title, text, url):
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), method="POST",
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "urgent",
                     "Tags": "rotating_light", "Click": url})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"ntfy fehlgeschlagen: {e}")


def alarm(cfg, status, details):
    title = "PS5 Pro VERFUEGBAR!" if status == "IN_STOCK" else "PS5 Pro: Warteschlange aktiv!"
    text = "Jetzt bei PlayStation Direct kaufen!" if status == "IN_STOCK" else "Moeglicher Drop - schnell rein!"
    log(f"*** ALARM: {title} ({details})")
    ntfy(cfg["ntfy_topic"], title, text, cfg["url"])
    if sys.platform == "win32" and not IS_CLOUD:
        toast(title, text)
        if cfg["open_browser"]:
            webbrowser.open(cfg["url"])
        beep()


def start_message(cfg):
    """Kurze Info aufs Handy, dass der Scanner laeuft (inkl. aktuellem Status)."""
    try:
        status, _ = check(cfg["url"])
    except Exception as e:
        status = f"Fehler beim ersten Check ({e})"
    where = "GitHub" if IS_CLOUD else "PC"
    text = {"OUT_OF_STOCK": "PS5 Pro aktuell ausverkauft",
            "IN_STOCK": "PS5 Pro VERFUEGBAR!",
            "QUEUE": "Warteschlange aktiv!",
            "UNKNOWN": "Seite sieht anders aus, bitte pruefen"}.get(status, status)
    if cfg["ntfy_topic"]:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{cfg['ntfy_topic']}",
                data=f"Scanner laeuft ({where}) bis {cfg['end']} Uhr, alle {cfg['interval_seconds']} s. "
                     f"Status: {text}".encode("utf-8"),
                method="POST",
                headers={"Title": "PS5 Pro Scanner gestartet", "Priority": "low", "Tags": "eyes"})
            urllib.request.urlopen(req, timeout=15).read()
            log("Start-Nachricht aufs Handy geschickt")
        except Exception as e:
            log(f"Start-Nachricht fehlgeschlagen: {e}")


def parse_hm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def main():
    cfg = load_config()
    args = set(sys.argv[1:])

    if "--test-alarm" in args:
        alarm(cfg, "IN_STOCK", "Test-Alarm")
        return
    if "--once" in args:
        status, details = check(cfg["url"])
        log(f"{status}  {details}")
        return

    windows = cfg.get("windows") or [[cfg["start"], cfg["end"]]]
    mins = lambda t: t.hour * 60 + t.minute
    n = mins(now())
    # Naechstes Zeitfenster, das noch nicht vorbei ist
    upcoming = [(s, e) for s, e in windows if mins(parse_hm(e)) > n]
    if not upcoming:
        log(f"Alle Zeitfenster ({windows}) fuer heute vorbei, beende.")
        return
    cfg["start"], cfg["end"] = upcoming[0]
    start, end = parse_hm(cfg["start"]), parse_hm(cfg["end"])
    if "--now" not in args:
        # GitHub startet pro Fenster zweimal (wegen Sommer-/Winterzeit). Der Lauf,
        # der mehr als 60 Min zu frueh kommt, beendet sich sofort.
        mins_early = mins(start) - n
        if mins_early > 60:
            log(f"Zu frueh ({mins_early} Min vor {cfg['start']}), der andere Lauf uebernimmt.")
            return
        while now().time() < start:
            time.sleep(10)

    log(f"Start: pruefe alle {cfg['interval_seconds']}s bis {cfg['end']} -> {cfg['url']}")
    start_message(cfg)
    last_alarm = 0.0
    checks = 0
    while now().time() < end:
        t0 = time.time()
        try:
            status, details = check(cfg["url"])
            checks += 1
            if status in ("IN_STOCK", "QUEUE"):
                if time.time() - last_alarm > cfg["alarm_repeat_minutes"] * 60:
                    alarm(cfg, status, details)
                    last_alarm = time.time()
                else:
                    log(f"{status} (weiterhin)  {details}")
            elif status == "UNKNOWN":
                log(f"UNKNOWN  {details}")
            elif checks % max(1, 300 // cfg["interval_seconds"]) == 1:  # nur ca. alle 5 Min loggen
                log(f"{status}  {details}")
        except urllib.error.HTTPError as e:
            log(f"HTTP-Fehler {e.code} (evtl. kurz geblockt)")
        except Exception as e:
            log(f"Fehler: {e}")
        # 20 s +/- ein paar Sekunden, damit es nicht wie ein starrer Bot aussieht
        wait = cfg["interval_seconds"] + random.uniform(-2, 2) - (time.time() - t0)
        time.sleep(max(1, wait))
    log(f"Ende des Zeitfensters, {checks} Checks gemacht.")


if __name__ == "__main__":
    main()
