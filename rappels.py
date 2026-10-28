#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""kitot : rappels de rendez-vous CalDAV pour KDE Plasma.

Sous-commandes :
  set-password   enregistre le mot de passe CalDAV dans KWallet (Secret Service)
  calendars      liste les agendas découverts sur le serveur
  list           affiche les rdv de la fenêtre à venir, avec leurs alarmes
  digest         envoie tout de suite le récapitulatif des rdv du jour
  pause [DUREE]  met les rappels en silence (ex. 1h, 2h, 45m ; défaut 1h)
  resume         réactive les rappels
  daemon         boucle : relève les agendas, envoie les rappels, signale les rdv déplacés/annulés

Politique de rappel (config ~/.config/kitot/config.ini) :
  - tous les agendas du compte sont surveillés (ou ceux listés dans `calendars`)
  - cascade de rappels avant chaque rdv (before_minutes = 30,5,0) ; le dernier est critique
  - les alarmes définies dans l'agenda (VALARM) sont respectées en plus
  - récapitulatif des rdv du jour à digest_hour, et des rdv du lendemain à eve_digest_hour
  - rdv déplacé ou annulé dans la fenêtre surveillée => notification
  - rattrapage : un rappel manqué (PC en veille) est envoyé tant que le rdv n'est pas fini
  - boutons sur les rappels : « Rappeler dans 5 min », « Rejoindre Teams », « Silence 1 h / 2 h »
  - au moment du rdv : « J'y suis » arrête tous les rappels du rdv (« Rejoindre Teams » aussi)
"""

import argparse
import configparser
import getpass
import html
import json
import locale
import logging
import re
import shutil
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import caldav
import keyring
from caldav.elements import ical

APP = "kitot"
APP_NAME = "Kitot"
CONFIG_DIR = Path.home() / ".config" / APP
CONFIG_FILE = CONFIG_DIR / "config.ini"
STATE_DIR = Path.home() / ".local" / "state" / APP
STATE_FILE = STATE_DIR / "state.json"
SILENCE_FILE = STATE_DIR / "silence"
AGENDA_FILE = STATE_DIR / "agenda.json"   # lu par l'icône de la boîte à miniatures (tray.py)
COMMANDS_FILE = STATE_DIR / "commands"    # commandes envoyées par l'icône (une ligne JSON par commande)
ICON_DIR = STATE_DIR / "icons"
PALETTE = ["#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#e53935", "#00897b"]
CAL_COLORS: dict[str, str] = {}           # nom d'agenda -> couleur #rrggbb (fournie par le serveur)
REDISCOVER_SECONDS = 3600  # fréquence de re-découverte des agendas

DEFAULTS = {
    "url": "",                    # URL CalDAV « home » du compte, ex. https://serveur/dav/home/moi@exemple.fr/
    "user": "",                   # identifiant de connexion (selon le serveur : uid ou adresse mail)
    "calendars": "",              # noms d'agendas à surveiller, séparés par des virgules (vide = tous)
    "before_minutes": "30,5,0",   # cascade de rappels, en minutes avant le rdv (0 = à l'heure du rdv)
    "critical_last": "true",      # dernier rappel de la cascade en urgence critique (reste affiché)
    "sound_last": "true",         # son d'alarme sur le dernier rappel (à l'heure du rdv)
    "sound_reminders": "true",    # son discret sur les autres rappels, récapitulatifs et changements
    "use_calendar_alarms": "true",  # respecter aussi les VALARM de l'agenda
    "snooze_minutes": "5",        # durée du bouton « Rappeler dans X min »
    "digest_hour": "8",           # heure du récapitulatif du jour (0 = désactivé)
    "eve_digest_hour": "17",      # heure du récapitulatif des rdv du lendemain (0 = désactivé)
    "notify_changes": "true",     # signaler les rdv déplacés / annulés
    "interval_seconds": "300",    # fréquence de relevé du serveur
    "window_hours": "48",         # fenêtre de rdv surveillés (>= plus longue alarme agenda)
    "allday_hour": "8",           # heure de rappel des événements "journée entière"
}

log = logging.getLogger(APP)


# ---------------------------------------------------------------- config

@dataclass
class Config:
    url: str
    user: str
    calendars: list[str]
    befores: list[int]
    critical_last: bool
    sound_last: bool
    sound_reminders: bool
    use_alarms: bool
    snooze: timedelta
    digest_hour: int
    eve_digest_hour: int
    notify_changes: bool
    interval: int
    window: timedelta
    allday_hour: int


def load_config() -> Config:
    cp = configparser.ConfigParser()
    cp.read(CONFIG_FILE)
    if "caldav" not in cp:
        cp["caldav"] = {}
    sec = cp["caldav"]
    changed = False
    for k, v in DEFAULTS.items():
        if k not in sec:
            sec[k] = v
            changed = True
    if changed:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_FILE.open("w") as f:
            cp.write(f)
        log.info("Configuration mise à jour : %s", CONFIG_FILE)
    if not sec["url"].strip() or not sec["user"].strip():
        sys.exit(f"Renseignez « url » et « user » dans {CONFIG_FILE} (voir config.example.ini).")
    befores = sorted({int(x) for x in sec["before_minutes"].replace(";", ",").split(",") if x.strip()},
                     reverse=True)
    return Config(
        url=sec["url"], user=sec["user"],
        calendars=[c.strip() for c in sec["calendars"].split(",") if c.strip()],
        befores=befores,
        critical_last=sec.getboolean("critical_last"), sound_last=sec.getboolean("sound_last"),
        sound_reminders=sec.getboolean("sound_reminders"),
        use_alarms=sec.getboolean("use_calendar_alarms"),
        snooze=timedelta(minutes=sec.getint("snooze_minutes")),
        digest_hour=sec.getint("digest_hour"), eve_digest_hour=sec.getint("eve_digest_hour"),
        notify_changes=sec.getboolean("notify_changes"), interval=sec.getint("interval_seconds"),
        window=timedelta(hours=sec.getint("window_hours")), allday_hour=sec.getint("allday_hour"),
    )


def get_password(cfg: Config) -> str:
    pw = keyring.get_password(APP, cfg.user)
    if not pw:
        sys.exit(f"Aucun mot de passe enregistré pour {cfg.user}. Lancez : {sys.argv[0]} set-password")
    return pw


# ---------------------------------------------------------------- caldav

def open_client(cfg: Config) -> caldav.DAVClient:
    return caldav.DAVClient(url=cfg.url, username=cfg.user, password=get_password(cfg))


def discover_calendars(client: caldav.DAVClient, cfg: Config) -> dict[str, caldav.Calendar]:
    """Agendas du compte (événements uniquement), indexés par nom affiché."""
    found: dict[str, caldav.Calendar] = {}
    # une erreur (réseau absent au démarrage, serveur indisponible) remonte à l'appelant,
    # qui réessaie plus tard ; pas de repli sur l'URL « home », qui n'est pas un agenda
    cals = client.principal().calendars()
    for cal in cals:
        try:
            comps = cal.get_supported_components()
        except Exception:  # noqa: BLE001
            comps = ["VEVENT"]
        if comps and "VEVENT" not in comps:
            continue
        try:
            name = cal.get_display_name() or str(cal.url).rstrip("/").rsplit("/", 1)[-1]
        except Exception:  # noqa: BLE001
            name = str(cal.url).rstrip("/").rsplit("/", 1)[-1]
        if cfg.calendars and name not in cfg.calendars:
            continue
        found[name] = cal
        CAL_COLORS[name] = calendar_color(cal, name)
    for wanted in cfg.calendars:
        if wanted not in found:
            log.warning("Agenda « %s » demandé dans la config mais introuvable sur le serveur", wanted)
    return found


def calendar_color(cal: caldav.Calendar, name: str) -> str:
    """Couleur de l'agenda (propriété calendar-color), sinon couleur stable tirée de la palette."""
    try:
        col = str(cal.get_property(ical.CalendarColor()) or "")
    except Exception:  # noqa: BLE001
        col = ""
    if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", col):
        return col[:7].lower()
    return PALETTE[zlib.crc32(name.encode()) % len(PALETTE)]


def calendar_icon(name: str, when) -> str | None:
    """Icône de notification : page d'éphéméride à la couleur de l'agenda, au jour du rdv.

    Dessinée par icons.py (Python du système + PyQt6) et mise en cache ; None en cas d'échec,
    la notification garde alors l'icône d'agenda du thème.
    """
    color = CAL_COLORS.get(name)
    if not color:
        return None
    day = when.date() if isinstance(when, datetime) else when
    dow = day.strftime("%a").upper().rstrip(".")[:3]
    path = ICON_DIR / f"v3-{color[1:]}-{day.isoformat()}.png"
    if not path.exists():
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        for old in ICON_DIR.glob("*.png"):       # ménage : icônes d'anciennes versions ou de jours passés
            if not old.name.startswith("v3-") or old.stem[-10:] < (date.today() - timedelta(days=2)).isoformat():
                old.unlink(missing_ok=True)
        script = Path(__file__).with_name("icons.py")
        try:
            subprocess.run(["/usr/bin/python3", "-I", str(script), color, dow, str(day.day), str(path)],
                           check=True, timeout=30, capture_output=True, text=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Icône de l'agenda « %s » non générée : %s", name, exc)
            return None
    return str(path)


def to_local(value):
    """datetime -> heure locale (un datetime naïf est considéré local) ; date -> inchangé."""
    if isinstance(value, datetime):
        return value.astimezone()
    return value


def as_dt(v) -> datetime:
    """Valeur comparable : une date (journée entière) devient minuit local."""
    if isinstance(v, datetime):
        return v
    return datetime.combine(v, dtime.min).astimezone()


URL_RE = re.compile(r"https?://[^\s<>\"']+")


def find_link(comp) -> tuple[str, str] | None:
    """(libellé du bouton, URL) : lien Teams en priorité, sinon premier lien trouvé."""
    candidates = []
    for prop in ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "URL", "LOCATION", "DESCRIPTION"):
        val = comp.get(prop)
        if val:
            candidates += URL_RE.findall(str(val))
    if not candidates:
        return None
    for url in candidates:
        if "teams.microsoft.com" in url or "teams.live.com" in url:
            return ("Rejoindre Teams", url.rstrip(".,;)"))
    return ("Ouvrir le lien", candidates[0].rstrip(".,;)"))


def parse_vevent(comp, calname: str) -> dict | None:
    dtstart = comp.get("DTSTART")
    if dtstart is None or str(comp.get("STATUS", "")).upper() == "CANCELLED":
        return None
    s = to_local(dtstart.dt)
    dtend = comp.get("DTEND")
    e = to_local(dtend.dt) if dtend is not None else None
    rid = comp.get("RECURRENCE-ID")
    uid = str(comp.get("UID"))
    key = f"{calname}|{uid}" + ("|" + as_dt(to_local(rid.dt)).isoformat() if rid is not None else "")
    alarms = []
    for a in comp.walk("VALARM"):
        trig = a.get("TRIGGER")
        if trig is not None:
            alarms.append(trig.dt)
    return {
        "key": key, "uid": uid, "calendar": calname,
        "summary": str(comp.get("SUMMARY", "(sans titre)")),
        "location": str(comp.get("LOCATION", "")),
        "start": s, "end": e, "allday": not isinstance(s, datetime),
        "alarms": alarms, "link": find_link(comp),
    }


def fetch_events(cals: dict[str, caldav.Calendar], start: datetime, end: datetime) -> dict[str, dict]:
    """Occurrences (récurrences déroulées) de tous les agendas entre start et end, indexées par clé."""
    events: dict[str, dict] = {}
    for name, cal in cals.items():
        for ev in cal.search(start=start, end=end, event=True, expand=True):
            for comp in ev.icalendar_instance.walk("VEVENT"):
                parsed = parse_vevent(comp, name)
                if parsed:
                    events[parsed["key"]] = parsed
    return events


def event_end(ev: dict) -> datetime:
    if ev["end"] is not None:
        return as_dt(ev["end"])
    return as_dt(ev["start"]) + timedelta(hours=1)


def lookup_uid(cal: caldav.Calendar, uid: str, calname: str) -> list[dict] | None:
    """Rdv (toutes composantes) pour un UID, None s'il n'existe plus sur le serveur."""
    try:
        ev = cal.event_by_uid(uid)
    except caldav.lib.error.NotFoundError:
        return None
    return [p for p in (parse_vevent(c, calname) for c in ev.icalendar_instance.walk("VEVENT")) if p]


# ---------------------------------------------------------------- notifications (D-Bus)

URGENCY = {"low": 0, "normal": 1, "critical": 2}


class Notifier:
    """Notifications via org.freedesktop.Notifications, avec boutons d'action.

    Les clics sur les boutons arrivent par signal D-Bus ; `pump()` les dépile et appelle
    le callback associé à la notification. Repli sur notify-send (sans boutons) si D-Bus
    est indisponible.
    """

    def __init__(self):
        self.conn = None
        self.handlers: dict[int, dict] = {}  # id de notification -> {clé d'action: callback}
        self._connect()

    def _connect(self):
        try:
            from jeepney import DBusAddress, MatchRule, message_bus
            from jeepney.io.blocking import Proxy, open_dbus_connection
            self.addr = DBusAddress("/org/freedesktop/Notifications",
                                    bus_name="org.freedesktop.Notifications",
                                    interface="org.freedesktop.Notifications")
            self.conn = open_dbus_connection(bus="SESSION")
            proxy = Proxy(message_bus, self.conn)
            self._queues = []
            for member in ("ActionInvoked", "NotificationClosed"):
                rule = MatchRule(type="signal", interface=self.addr.interface, member=member)
                proxy.AddMatch(rule)
                self._queues.append((member, self.conn.filter(rule, bufsize=64).queue))
        except Exception as exc:  # noqa: BLE001
            log.warning("D-Bus indisponible (%s) : repli sur notify-send, sans boutons", exc)
            self.conn = None

    def notify(self, title: str, body: str, urgency: str = "normal", replace_id: int = 0,
               actions: list[tuple[str, str, object]] | None = None, sound: str | None = None,
               markup: str | None = None, image: str | None = None) -> int:
        """actions : liste de (clé, libellé, callback). markup : corps enrichi (HTML simple)."""
        actions = actions or []
        nid = 0
        if self.conn is not None:
            try:
                from jeepney import new_method_call
                flat = [x for key, label, _ in actions for x in (key, label)]
                hints = {"urgency": ("y", URGENCY.get(urgency, 1)), "desktop-entry": ("s", APP)}
                if image:
                    hints["image-path"] = ("s", image)
                text = markup if markup is not None else html.escape(body, quote=False)
                msg = new_method_call(self.addr, "Notify", "susssasa{sv}i",
                                      (APP_NAME, replace_id, "office-calendar", title,
                                       text, flat, hints, 0))
                nid = self.conn.send_and_get_reply(msg, timeout=10).body[0]
            except Exception as exc:  # noqa: BLE001
                log.error("Notification D-Bus échouée (%s), tentative de reconnexion", exc)
                self._connect()
        if not nid:
            nid = self._notify_send(title, body, urgency, replace_id)
        if replace_id:
            self.handlers.pop(replace_id, None)
        if nid and actions:
            self.handlers[nid] = {key: cb for key, _, cb in actions}
        if sound and not self.inhibited():
            play_sound(sound)
        return nid

    def inhibited(self) -> bool:
        """Mode « Ne pas déranger » de Plasma actif ?"""
        if self.conn is None:
            return False
        try:
            from jeepney import Properties
            return bool(self.conn.send_and_get_reply(Properties(self.addr).get("Inhibited"), timeout=5).body[0][1])
        except Exception:  # noqa: BLE001
            return False

    def close(self, nid: int):
        if self.conn is None or not nid:
            return
        try:
            from jeepney import new_method_call
            self.conn.send_and_get_reply(new_method_call(self.addr, "CloseNotification", "u", (nid,)), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.handlers.pop(nid, None)

    @staticmethod
    def _notify_send(title, body, urgency, replace_id) -> int:
        cmd = ["notify-send", "-p", "-a", APP_NAME, "-i", "office-calendar", "-u", urgency, "-t", "0"]
        if replace_id:
            cmd += ["-r", str(replace_id)]
        try:
            out = subprocess.run(cmd + [title, body], capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip() or 0)
        except Exception as exc:  # noqa: BLE001
            log.error("notify-send a échoué : %s", exc)
            return 0

    def pump(self, seconds: float, idle=None):
        """Attend `seconds` en traitant les clics sur les boutons.

        `idle` est appelé toutes les 2 s environ ; s'il renvoie True, l'attente s'arrête.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (idle and idle()):
                break
            if self.conn is None:
                time.sleep(min(remaining, 2))
                continue
            try:
                self.conn.recv_messages(timeout=min(remaining, 2))
            except TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.error("Connexion D-Bus perdue (%s), reconnexion", exc)
                self._connect()
                time.sleep(2)
                continue
            for member, queue in self._queues:
                while queue:
                    msg = queue.popleft()
                    nid = msg.body[0]
                    if member == "ActionInvoked":
                        cb = self.handlers.get(nid, {}).get(msg.body[1])
                        if cb:
                            try:
                                cb()
                            except Exception as exc:  # noqa: BLE001
                                log.error("Action « %s » échouée : %s", msg.body[1], exc)
                    else:  # NotificationClosed
                        self.handlers.pop(nid, None)


SOUND_ALARM, SOUND_SOFT = "alarm-clock-elapsed", "message-new-instant"


def play_sound(name: str):
    """Joue un son du thème (le serveur de notifications Plasma ne joue pas ceux des notifications D-Bus)."""
    for name in (f"{name}.oga", "message-new-instant.oga", "bell.oga"):
        for d in ("/usr/share/sounds/ocean/stereo", "/usr/share/sounds/freedesktop/stereo"):
            f = Path(d) / name
            if f.exists():
                player = shutil.which("pw-play") or shutil.which("paplay")
                if player:
                    subprocess.Popen([player, str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return


def open_url(url: str):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("Ouverture de %s", url)


# ---------------------------------------------------------------- formats

def fmt_when(v, with_day=True) -> str:
    if isinstance(v, datetime):
        return v.strftime("%a %d/%m %H:%M") if with_day else v.strftime("%H:%M")
    return v.strftime("%a %d/%m") + " (journée)" if with_day else "journée"


MULTI_CAL = False  # mis à True quand plusieurs agendas sont surveillés : affiche le nom de l'agenda


def fmt_event(ev: dict, with_day=True) -> str:
    when = fmt_when(ev["start"], with_day)
    if not ev["allday"] and ev["end"]:
        when += "-" + ev["end"].strftime("%H:%M")
    loc = f" @ {ev['location']}" if ev["location"] else ""
    cal = f"  [{ev['calendar']}]" if MULTI_CAL else ""
    return f"{when}  {ev['summary']}{loc}{cal}"


def fmt_markup(ev: dict) -> str:
    """Corps de notification enrichi : titre en gras, horaire, lieu, agenda."""
    esc = lambda t: html.escape(t, quote=False)  # noqa: E731
    when = fmt_when(ev["start"])
    if not ev["allday"] and ev["end"]:
        when += "-" + ev["end"].strftime("%H:%M")
    lines = [f"<b>{esc(ev['summary'])}</b>", esc(when)]
    if ev["location"] and not URL_RE.fullmatch(ev["location"].strip()):
        lines[-1] += f" · <i>{esc(ev['location'])}</i>"
    if MULTI_CAL:
        lines.append(esc(ev["calendar"]))
    # pas de lien dans le texte : le bouton « Rejoindre Teams » / « Ouvrir le lien » s'en charge
    return "\n".join(lines)


def fmt_delta(td: timedelta) -> str:
    mins = int(abs(td).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m:02d}" if m else f"{h} h"


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([hm]?)\s*", text.lower())
    if not m:
        sys.exit(f"Durée invalide : {text!r} (exemples : 1h, 2h, 45m)")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(minutes=n)


# ---------------------------------------------------------------- état persistant

def load_state() -> dict:
    state = {"notified": {}, "acked": {}, "known": {}, "digest": "", "digest_eve": ""}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict) and "notified" in data:
                state.update(data)
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def get_silence() -> datetime | None:
    """Fin du silence en cours, ou None. Le fichier est partagé avec les commandes pause/resume."""
    if not SILENCE_FILE.exists():
        return None
    try:
        until = datetime.fromisoformat(SILENCE_FILE.read_text().strip())
    except ValueError:
        SILENCE_FILE.unlink(missing_ok=True)
        return None
    if until <= datetime.now().astimezone():
        SILENCE_FILE.unlink(missing_ok=True)
        return None
    return until


def set_silence(until: datetime | None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if until is None:
        SILENCE_FILE.unlink(missing_ok=True)
    else:
        SILENCE_FILE.write_text(until.isoformat())


# ---------------------------------------------------------------- logique de rappel

def stages_for(ev: dict, cfg: Config) -> list[tuple[str, datetime, bool]]:
    """Étapes de rappel d'un rdv : (label, instant de déclenchement, dernier rappel ?)."""
    start = as_dt(ev["start"])
    if ev["allday"]:
        stages = [("allday", datetime.combine(ev["start"], dtime(cfg.allday_hour)).astimezone(), False)]
    else:
        stages = [(f"-{m}", start - timedelta(minutes=m), i == len(cfg.befores) - 1)
                  for i, m in enumerate(cfg.befores)]
    if cfg.use_alarms:
        for trig in ev["alarms"]:
            fire = start + trig if isinstance(trig, timedelta) else to_local(trig)
            if all(fire != f for _, f, _ in stages):
                stages.append((f"alarm{trig}", fire, False))
    return stages


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = load_state()
        self.notifier = Notifier()
        self.client = open_client(cfg)
        self.cals: dict[str, caldav.Calendar] = {}
        self.cache: dict[str, dict] = {}
        self.nids: dict[str, int] = {}        # clé de rdv -> id de notification (pour remplacement)
        self.snoozes: dict[str, datetime] = {}  # clé de rdv -> instant du rappel différé
        self.silenced = False
        self.next_fetch = 0.0
        self.last_fetch: str | None = None
        self.last_error: str | None = None
        self._exported = ""

    # ---- actions des boutons

    def actions_for(self, ev: dict, at_start: bool) -> list[tuple[str, str, object]]:
        """Boutons du rappel. Au moment du rdv (ou après) : « J'y suis » en premier, sans les silences."""
        snooze = ("snooze", f"Rappeler dans {int(self.cfg.snooze.total_seconds() // 60)} min",
                  lambda: self.snooze(ev))
        link = []
        if ev["link"]:
            label, url = ev["link"]
            link = [("open", label, lambda: (self.ack(ev, quiet=True), open_url(url)))]
        if at_start:
            return [("ack", "J'y suis", lambda: self.ack(ev))] + link + [snooze]
        return [snooze] + link + [("silence1", "Silence 1 h", lambda: self.silence(timedelta(hours=1))),
                                  ("silence2", "Silence 2 h", lambda: self.silence(timedelta(hours=2)))]

    def sound_for(self, alarm: bool = False) -> str | None:
        if alarm and self.cfg.sound_last:
            return SOUND_ALARM
        return SOUND_SOFT if self.cfg.sound_reminders else None

    def ack_key(self, ev: dict) -> str:
        return f"{ev['key']}|{as_dt(ev['start']).isoformat()}"

    def ack(self, ev: dict, quiet: bool = False):
        """Rdv pris en compte : plus aucun rappel (cascade restante, report) pour cette occurrence."""
        self.state["acked"][self.ack_key(ev)] = datetime.now().astimezone().isoformat()
        self.snoozes.pop(ev["key"], None)
        self.notifier.close(self.nids.pop(ev["key"], 0))
        save_state(self.state)
        self.export_agenda(force=True)
        log.info("%s : %s", "Rejoint" if quiet else "J'y suis", fmt_event(ev))

    def snooze(self, ev: dict):
        self.snoozes[ev["key"]] = datetime.now().astimezone() + self.cfg.snooze
        log.info("Rappel différé de %s : %s", fmt_delta(self.cfg.snooze), fmt_event(ev))

    def silence(self, duration: timedelta):
        until = datetime.now().astimezone() + duration
        set_silence(until)
        log.info("Silence jusqu'à %s", until.strftime("%H:%M"))
        self.notifier.notify("Rappels en silence", f"Jusqu'à {until.strftime('%H:%M')}.",
                             actions=[("resume", "Réactiver", self.resume)])

    def resume(self):
        set_silence(None)
        log.info("Silence levé")

    # ---- envoi des rappels

    def remind(self, ev: dict, label: str, last: bool, now: datetime, snoozed: bool = False):
        start = as_dt(ev["start"])
        if ev["allday"]:
            title = f"Aujourd'hui : {ev['summary']}"
        elif start - now < timedelta(minutes=1) and start > now - timedelta(minutes=1):
            title = f"C'est l'heure : {ev['summary']}"
        elif start > now:
            title = f"Dans {fmt_delta(start - now)} : {ev['summary']}"
        else:
            title = f"A commencé il y a {fmt_delta(now - start)} : {ev['summary']}"
        urgent = (last or snoozed) and self.cfg.critical_last
        at_start = not ev["allday"] and start - now < timedelta(minutes=1)
        nid = self.notifier.notify(title, fmt_event(ev), urgency="critical" if urgent else "normal",
                                   replace_id=self.nids.get(ev["key"], 0), actions=self.actions_for(ev, at_start),
                                   sound=self.sound_for(last or snoozed),
                                   markup=fmt_markup(ev), image=calendar_icon(ev["calendar"], ev["start"]))
        if nid:
            self.nids[ev["key"]] = nid
        log.info("Rappel (%s) : %s", label, fmt_event(ev))

    def process_reminders(self, now: datetime):
        for ev in self.cache.values():
            start = as_dt(ev["start"])
            if now > event_end(ev) or self.ack_key(ev) in self.state["acked"]:
                self.snoozes.pop(ev["key"], None)
                continue
            # rappels échus non encore envoyés ; après un silence ou un réveil, seul le plus récent part
            due = []
            for label, fire_at, last in stages_for(ev, self.cfg):
                skey = f"{ev['key']}|{start.isoformat()}|{label}"
                if skey not in self.state["notified"] and fire_at <= now:
                    due.append((fire_at, label, last, skey))
            if not due:
                continue
            due.sort()
            _, label, _, _ = due[-1]
            self.remind(ev, label, any(d[2] for d in due), now)
            for *_, skey in due:
                self.state["notified"][skey] = now.isoformat()
            save_state(self.state)
        for key, when in list(self.snoozes.items()):
            if now >= when:
                del self.snoozes[key]
                if key in self.cache:
                    self.remind(self.cache[key], "différé", False, now, snoozed=True)

    def detect_changes(self, now: datetime):
        """Compare les agendas relevés avec le dernier état connu ; signale déplacements et annulations."""
        for key, old in list(self.state["known"].items()):
            old_start = datetime.fromisoformat(old["start"])
            if key in self.cache:
                new = self.cache[key]
                if as_dt(new["start"]) != old_start:
                    self.notifier.notify("Rendez-vous déplacé", f"{old['text']}\n→ {fmt_event(new)}",
                                         image=calendar_icon(new["calendar"], new["start"]), sound=self.sound_for())
                    log.info("Déplacé : %s → %s", old["text"], fmt_event(new))
                continue
            if old_start <= now:
                continue  # rdv passé, disparition normale de la fenêtre
            cal = self.cals.get(old.get("calendar", ""))
            if cal is None:
                continue  # agenda disparu ou retiré de la config : on ne conclut rien
            try:
                comps = lookup_uid(cal, old["uid"], old["calendar"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Vérification de %s impossible : %s", old["text"], exc)
                continue
            future = sorted(as_dt(c["start"]) for c in (comps or [])
                            if as_dt(c["start"]) >= now - timedelta(hours=1))
            if not comps or not future:
                self.notifier.notify("Rendez-vous annulé", old["text"], sound=self.sound_for())
                log.info("Annulé : %s", old["text"])
            elif future[0] != old_start:
                self.notifier.notify("Rendez-vous déplacé", f"{old['text']}\n→ {fmt_when(future[0])}",
                                     sound=self.sound_for())
                log.info("Déplacé : %s → %s", old["text"], fmt_when(future[0]))
        self.state["known"] = {k: {"uid": ev["uid"], "calendar": ev["calendar"],
                                   "start": as_dt(ev["start"]).isoformat(), "text": fmt_event(ev)}
                               for k, ev in self.cache.items()}

    def digests(self, now: datetime):
        today = now.date()
        if self.cfg.digest_hour and self.state["digest"] != today.isoformat() \
                and self.cfg.digest_hour <= now.hour < self.cfg.digest_hour + 4:
            send_digest(self.notifier, self.cache, today, now.strftime("Agenda du %A %d %B"), always=True,
                        sound=self.sound_for())
            self.state["digest"] = today.isoformat()
            save_state(self.state)
        if self.cfg.eve_digest_hour and self.state["digest_eve"] != today.isoformat() \
                and self.cfg.eve_digest_hour <= now.hour < self.cfg.eve_digest_hour + 4:
            send_digest(self.notifier, self.cache, today + timedelta(days=1), "Demain", always=False,
                        sound=self.sound_for())
            self.state["digest_eve"] = today.isoformat()
            save_state(self.state)

    # ---- échanges avec l'icône de la boîte à miniatures

    def export_agenda(self, force: bool = False):
        """Écrit agenda.json (si changé) : rdv de la fenêtre, couleurs, état des relevés."""
        evs = []
        for ev in sorted(self.cache.values(), key=lambda e: as_dt(e["start"])):
            snooze = self.snoozes.get(ev["key"])
            evs.append({
                "key": ev["key"], "ack_key": self.ack_key(ev), "calendar": ev["calendar"],
                "color": CAL_COLORS.get(ev["calendar"], PALETTE[0]),
                "summary": ev["summary"], "location": ev["location"],
                "start": as_dt(ev["start"]).isoformat(), "end": event_end(ev).isoformat(),
                "allday": ev["allday"], "link": list(ev["link"]) if ev["link"] else None,
                "acked": self.ack_key(ev) in self.state["acked"],
                "snoozed_until": snooze.isoformat() if snooze else None,
            })
        data = {"events": evs, "calendars": {n: CAL_COLORS.get(n, PALETTE[0]) for n in self.cals},
                "last_fetch": self.last_fetch, "error": self.last_error}
        text = json.dumps(data, ensure_ascii=False, indent=1)
        if force or text != self._exported:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = AGENDA_FILE.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(AGENDA_FILE)
            self._exported = text

    def process_commands(self) -> bool:
        """Traite les commandes de l'icône. Renvoie True pour interrompre l'attente (relevé immédiat)."""
        if not COMMANDS_FILE.exists():
            return False
        work = COMMANDS_FILE.with_suffix(".work")
        try:
            COMMANDS_FILE.replace(work)
            lines = work.read_text().splitlines()
            work.unlink()
        except OSError:
            return False
        wake = False
        for line in lines:
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("cmd") == "refresh":
                log.info("Relevé demandé depuis l'icône")
                self.next_fetch = 0.0
                wake = True
            elif cmd.get("cmd") == "ack":
                ev = next((e for e in self.cache.values() if self.ack_key(e) == cmd.get("ack_key")), None)
                if ev:
                    self.ack(ev)
            elif cmd.get("cmd") == "silence":
                wake = True  # le fichier silence est écrit par l'icône ; on le prend en compte tout de suite
        return wake

    # ---- boucle principale

    def run(self):
        cfg = self.cfg
        log.info("Démon lancé : rappels %s min avant, alarmes agenda %s, récap %sh / veille %sh, relevé toutes les %ss",
                 cfg.befores, "oui" if cfg.use_alarms else "non", cfg.digest_hour or "-",
                 cfg.eve_digest_hour or "-", cfg.interval)
        global MULTI_CAL  # noqa: PLW0603
        self.next_fetch = next_discover = 0.0
        fetched_once = False
        while True:
            now = datetime.now().astimezone()
            until = get_silence()
            if until and not self.silenced:
                log.info("Rappels en silence jusqu'à %s", until.strftime("%H:%M"))
            elif self.silenced and not until:
                log.info("Rappels réactivés")
                self.notifier.notify("Rappels réactivés", "Les rappels manqués pendant le silence suivent.")
            self.silenced = until is not None

            if time.monotonic() >= self.next_fetch:
                try:
                    if time.monotonic() >= next_discover or not self.cals:
                        new_cals = discover_calendars(self.client, cfg)
                        if new_cals:
                            if set(new_cals) != set(self.cals):
                                log.info("Agendas surveillés : %s", ", ".join(new_cals))
                            self.cals = new_cals
                            MULTI_CAL = len(self.cals) > 1
                        next_discover = time.monotonic() + REDISCOVER_SECONDS
                    self.cache = fetch_events(self.cals, now - timedelta(hours=1), now + cfg.window)
                    fetched_once = True
                    self.last_fetch, self.last_error = datetime.now().astimezone().isoformat(), None
                    self.next_fetch = time.monotonic() + cfg.interval
                    log.info("Relevé OK : %d rdv dans les %s (%d agendas)", len(self.cache), cfg.window, len(self.cals))
                    if cfg.notify_changes and not self.silenced:
                        self.detect_changes(now)
                        save_state(self.state)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Relevé impossible (%s), nouvel essai dans 60 s", exc)
                    self.last_error = str(exc)[:200]
                    self.next_fetch = time.monotonic() + 60

            if fetched_once and not self.silenced:
                self.digests(now)
                self.process_reminders(now)

            cutoff = (now - timedelta(days=7)).isoformat()
            for bucket in ("notified", "acked"):
                self.state[bucket] = {k: v for k, v in self.state[bucket].items() if v > cutoff}
            self.export_agenda()
            self.notifier.pump(min(30, cfg.interval), idle=self.process_commands)


def send_digest(notifier: Notifier, cache: dict, day, title_prefix: str, always: bool, sound: str | None = None):
    evs = sorted((ev for ev in cache.values() if as_dt(ev["start"]).date() == day),
                 key=lambda ev: as_dt(ev["start"]))
    if not evs and not always:
        return
    body = "\n".join(fmt_event(ev, with_day=False) for ev in evs) or "Aucun rendez-vous."
    title = f"{title_prefix} : {len(evs)} rdv" if evs else title_prefix
    notifier.notify(title, body, sound=sound)
    log.info("Récapitulatif « %s » envoyé : %d rdv", title_prefix, len(evs))


# ---------------------------------------------------------------- commandes

def setup_calendars(cfg: Config) -> dict[str, caldav.Calendar]:
    global MULTI_CAL  # noqa: PLW0603
    cals = discover_calendars(open_client(cfg), cfg)
    if not cals:
        sys.exit("Aucun agenda trouvé sur le serveur.")
    MULTI_CAL = len(cals) > 1
    return cals


def cmd_set_password(cfg: Config, _args):
    pw = getpass.getpass(f"Mot de passe CalDAV pour {cfg.user} : ")
    keyring.set_password(APP, cfg.user, pw)
    print("Mot de passe enregistré dans le trousseau (KWallet).")


def cmd_calendars(cfg: Config, _args):
    for name, cal in setup_calendars(cfg).items():
        print(f"{name}  ->  {cal.url}")


def cmd_list(cfg: Config, args):
    cals = setup_calendars(cfg)
    now = datetime.now().astimezone()
    events = fetch_events(cals, now - timedelta(hours=1), now + timedelta(hours=args.hours))
    if not events:
        print(f"Aucun rdv dans les {args.hours} prochaines heures.")
    for ev in sorted(events.values(), key=lambda e: as_dt(e["start"])):
        line = fmt_event(ev)
        if ev["alarms"]:
            line += "   [alarmes agenda : " + ", ".join(
                fmt_delta(a) + " avant" if isinstance(a, timedelta) else str(a) for a in ev["alarms"]) + "]"
        if ev["link"]:
            line += f"   [{ev['link'][0]}]"
        print(line)


def cmd_digest(cfg: Config, _args):
    cals = setup_calendars(cfg)
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    send_digest(Notifier(), fetch_events(cals, start, start + timedelta(days=1)), now.date(),
                now.strftime("Agenda du %A %d %B"), always=True)


def cmd_pause(_cfg: Config, args):
    until = datetime.now().astimezone() + parse_duration(args.duration)
    set_silence(until)
    print(f"Rappels en silence jusqu'à {until.strftime('%H:%M')}.")


def cmd_resume(_cfg: Config, _args):
    set_silence(None)
    print("Rappels réactivés.")


def cmd_daemon(cfg: Config, _args):
    Daemon(cfg).run()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password").set_defaults(func=cmd_set_password)
    sub.add_parser("calendars").set_defaults(func=cmd_calendars)
    pl = sub.add_parser("list")
    pl.add_argument("--hours", type=int, default=24)
    pl.set_defaults(func=cmd_list)
    sub.add_parser("digest").set_defaults(func=cmd_digest)
    pp = sub.add_parser("pause")
    pp.add_argument("duration", nargs="?", default="1h")
    pp.set_defaults(func=cmd_pause)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("daemon").set_defaults(func=cmd_daemon)
    args = p.parse_args()
    args.func(load_config(), args)


if __name__ == "__main__":
    main()
