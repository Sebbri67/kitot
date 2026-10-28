#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Icône de la boîte à miniatures de kitot.

Lit l'état publié par le démon (~/.local/state/kitot/agenda.json) et affiche :
  - une icône dont la pastille indique les minutes avant le prochain rdv
    (orange à 30 min, rouge à 5 min et au début du rdv, grisée en silence) ;
  - une infobulle avec le prochain rdv ;
  - au clic, la fenêtre « Agenda » : rdv d'aujourd'hui et de demain, aux couleurs des agendas,
    avec « Rejoindre Teams », « J'y suis », silence et actualisation.

Fonctionne avec le Python du système (paquet python3-pyqt6), sans l'environnement virtuel.
"""

import json
import locale
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from icons import draw_page
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                             QScrollArea, QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

APP = "kitot"
STATE_DIR = Path.home() / ".local" / "state" / APP
AGENDA_FILE = STATE_DIR / "agenda.json"
COMMANDS_FILE = STATE_DIR / "commands"
SILENCE_FILE = STATE_DIR / "silence"

ORANGE, RED, GREY = QColor("#f57c00"), QColor("#d32f2f"), QColor("#757575")
JUST_STARTED = timedelta(minutes=10)   # un rdv commencé depuis moins de 10 min et non acquitté reste « en alerte »


def now() -> datetime:
    return datetime.now().astimezone()


def fmt_delta(td: timedelta) -> str:
    mins = max(0, int(td.total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m:02d}" if m else f"{h} h"


# ---------------------------------------------------------------- données

class Model:
    def __init__(self):
        self.events: list[dict] = []
        self.error: str | None = None
        self.last_fetch: datetime | None = None
        self.loaded = False

    def load(self):
        try:
            data = json.loads(AGENDA_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self.loaded = True
        self.error = data.get("error")
        self.last_fetch = datetime.fromisoformat(data["last_fetch"]) if data.get("last_fetch") else None
        self.events = []
        for e in data.get("events", []):
            e["start"] = datetime.fromisoformat(e["start"])
            e["end"] = datetime.fromisoformat(e["end"])
            self.events.append(e)

    @staticmethod
    def silence_until() -> datetime | None:
        try:
            until = datetime.fromisoformat(SILENCE_FILE.read_text().strip())
        except (OSError, ValueError):
            return None
        return until if until > now() else None

    def next_alert(self) -> dict | None:
        """Prochain rdv qui mérite l'attention : à venir, ou commencé depuis peu et non acquitté."""
        t = now()
        for e in self.events:
            if e["allday"] or e["acked"] or e["end"] <= t:
                continue
            if e["start"] > t or t - e["start"] < JUST_STARTED:
                return e
        return None

    def current(self) -> list[dict]:
        t = now()
        return [e for e in self.events if not e["allday"] and e["start"] <= t < e["end"]]


def send_command(cmd: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with COMMANDS_FILE.open("a") as f:
        f.write(json.dumps(cmd) + "\n")


def set_silence(hours: int | None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if hours is None:
        SILENCE_FILE.unlink(missing_ok=True)
    else:
        SILENCE_FILE.write_text((now() + timedelta(hours=hours)).isoformat())
    send_command({"cmd": "silence"})


def open_url(url: str):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- icône

NEUTRAL = QColor("#3daee9")   # bleu Breeze


def render_icon(state: str, minutes: int | None = None, color: QColor | None = None) -> QIcon:
    """Icône « page d'éphéméride » : date du jour au repos, minutes restantes à l'approche d'un rdv."""
    t = now()
    dow = t.strftime("%a").upper().rstrip(".")[:3]
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128):
        if state == "countdown":
            pm = draw_page(size, color, "MIN", str(minutes), body_color=color)
        elif state == "start":
            pm = draw_page(size, RED, "RDV", "!", body_color=RED)
        elif state == "silence":
            pm = draw_page(size, GREY, "OFF", "z", body_color=GREY, dimmed=True)
        elif state == "error":
            pm = draw_page(size, GREY, dow, "?", body_color=GREY)
        else:
            pm = draw_page(size, color or NEUTRAL, dow, str(t.day))
        icon.addPixmap(pm)
    return icon


# ---------------------------------------------------------------- fenêtre agenda

class EventRow(QFrame):
    def __init__(self, e: dict, t: datetime, window: "AgendaWindow"):
        super().__init__()
        color = QColor(e["color"])
        past = e["end"] <= t
        ongoing = not e["allday"] and e["start"] <= t < e["end"]
        tint = QColor(color)
        tint.setAlpha(45 if ongoing else 0)
        self.setObjectName("row")
        self.setStyleSheet(
            f"#row {{ border-left: 5px solid {color.name()}; border-radius: 4px;"
            f" background: rgba({tint.red()},{tint.green()},{tint.blue()},{tint.alpha()}); }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)

        when = "Journée" if e["allday"] else f"{e['start']:%H:%M}\n{e['end']:%H:%M}"
        lt = QLabel(when)
        lt.setFixedWidth(52)
        f = lt.font()
        f.setBold(True)
        lt.setFont(f)
        lay.addWidget(lt)

        texts = QVBoxLayout()
        title = QLabel(e["summary"])
        title.setWordWrap(True)
        tf = title.font()
        tf.setBold(not past)
        tf.setPointSizeF(tf.pointSizeF() * 1.05)
        title.setFont(tf)
        texts.addWidget(title)
        details = [e["calendar"]]
        if e["location"]:
            details.insert(0, e["location"])
        if ongoing:
            details.append("en cours" + (" · vu" if e["acked"] else ""))
        elif not past and not e["allday"] and e["start"] > t and e["start"] - t < timedelta(hours=2):
            details.append(f"dans {fmt_delta(e['start'] - t)}")
        if e.get("snoozed_until"):
            details.append(f"rappel à {datetime.fromisoformat(e['snoozed_until']):%H:%M}")
        sub = QLabel(" · ".join(details))
        sub.setWordWrap(True)
        sub.setStyleSheet("color: palette(placeholder-text);")
        texts.addWidget(sub)
        lay.addLayout(texts, 1)

        if not past:
            if e["link"]:
                b = QPushButton("Teams" if "Teams" in e["link"][0] else "Lien")
                b.setToolTip(e["link"][1])
                b.clicked.connect(lambda _=False, e=e: window.join(e))
                lay.addWidget(b)
            if not e["allday"] and not e["acked"] and e["start"] - t < timedelta(minutes=30):
                b = QPushButton("J'y suis")
                b.clicked.connect(lambda _=False, e=e: window.ack(e))
                lay.addWidget(b)
        if past:
            for lab in (lt, title, sub):
                lab.setEnabled(False)


class AgendaWindow(QWidget):
    def __init__(self, model: Model, tray: "Tray"):
        super().__init__()
        self.model, self.tray = model, tray
        self.setWindowTitle("Kitot — Agenda")
        self.setWindowIcon(render_icon("idle"))
        self.resize(460, 560)
        outer = QVBoxLayout(self)
        self.header = QLabel()
        hf = self.header.font()
        hf.setPointSizeF(hf.pointSizeF() * 1.3)
        hf.setBold(True)
        self.header.setFont(hf)
        self.header.setWordWrap(True)
        outer.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll, 1)

        bar = QHBoxLayout()
        self.b_s1 = QPushButton(QIcon.fromTheme("notifications-disabled"), "Silence 1 h")
        self.b_s2 = QPushButton("Silence 2 h")
        self.b_resume = QPushButton(QIcon.fromTheme("notifications"), "Réactiver")
        self.b_refresh = QPushButton(QIcon.fromTheme("view-refresh"), "Actualiser")
        self.b_s1.clicked.connect(lambda: tray.silence(1))
        self.b_s2.clicked.connect(lambda: tray.silence(2))
        self.b_resume.clicked.connect(lambda: tray.silence(None))
        self.b_refresh.clicked.connect(tray.refresh)
        for b in (self.b_s1, self.b_s2, self.b_resume):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.b_refresh)
        outer.addLayout(bar)
        self.status = QLabel()
        self.status.setStyleSheet("color: palette(placeholder-text);")
        outer.addWidget(self.status)

    def join(self, e: dict):
        open_url(e["link"][1])
        send_command({"cmd": "ack", "ack_key": e["ack_key"]})

    def ack(self, e: dict):
        send_command({"cmd": "ack", "ack_key": e["ack_key"]})
        e["acked"] = True
        self.rebuild()

    def rebuild(self):
        t = now()
        m = self.model
        nxt = m.next_alert()
        if not m.loaded:
            self.header.setText("En attente du démon de rappels…")
        elif nxt and nxt["start"] > t:
            self.header.setText(f"Prochain rdv dans {fmt_delta(nxt['start'] - t)} : {nxt['summary']}")
        elif nxt:
            self.header.setText(f"Commencé il y a {fmt_delta(t - nxt['start'])} : {nxt['summary']}")
        else:
            self.header.setText("Plus de rdv à venir aujourd'hui" if not any(
                e["start"].date() == t.date() and e["end"] > t for e in m.events) else "Pas de rappel en attente")

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setSpacing(6)
        for day, label in ((t.date(), "Aujourd'hui"), (t.date() + timedelta(days=1), "Demain")):
            h = QLabel(f"{label} — {datetime.combine(day, datetime.min.time()):%A %d %B}")
            hf = h.font()
            hf.setBold(True)
            h.setFont(hf)
            h.setContentsMargins(0, 10, 0, 2)
            lay.addWidget(h)
            evs = [e for e in m.events if e["start"].astimezone().date() == day
                   or (e["allday"] and e["start"].date() <= day < e["end"].date())]
            if not evs:
                lay.addWidget(QLabel("Aucun rendez-vous"))
            for e in evs:
                lay.addWidget(EventRow(e, t, self))
        lay.addStretch(1)
        self.scroll.setWidget(body)

        until = m.silence_until()
        self.b_s1.setVisible(until is None)
        self.b_s2.setVisible(until is None)
        self.b_resume.setVisible(until is not None)
        parts = []
        if until:
            parts.append(f"Rappels en silence jusqu'à {until:%H:%M}")
        if m.error:
            parts.append(f"Serveur injoignable : {m.error[:80]}")
        if m.last_fetch:
            parts.append(f"Mis à jour à {m.last_fetch:%H:%M}")
        self.status.setText(" · ".join(parts))


# ---------------------------------------------------------------- icône de la boîte à miniatures

class Tray:
    def __init__(self, app: QApplication):
        self.app = app
        self.model = Model()
        self.icon = QSystemTrayIcon()
        self.window = AgendaWindow(self.model, self)
        self.menu = QMenu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.on_activated)

        self.watcher = QFileSystemWatcher([str(STATE_DIR)] if STATE_DIR.exists() else [])
        self.watcher.directoryChanged.connect(lambda _: self.reload())
        self.timer = QTimer()
        self.timer.timeout.connect(self.reload)
        self.timer.start(20_000)
        self.reload()
        self.icon.show()

    def reload(self):
        if not self.watcher.directories() and STATE_DIR.exists():
            self.watcher.addPath(str(STATE_DIR))
        self.model.load()
        self.update_icon()
        self.build_menu()
        if self.window.isVisible():
            self.window.rebuild()

    def update_icon(self):
        t = now()
        m = self.model
        until = m.silence_until()
        nxt = m.next_alert()
        state, minutes, color = "idle", None, None
        if nxt:
            delta = nxt["start"] - t
            if delta <= timedelta(0):
                state = "start"
            elif delta <= timedelta(minutes=30):
                minutes = max(1, int(delta.total_seconds() // 60))
                state, color = "countdown", (RED if delta <= timedelta(minutes=5) else ORANGE)
        if until:
            state = "silence"
        elif m.loaded and m.error:
            state = "error"
        self.icon.setIcon(render_icon(state, minutes, color))

        lines = []
        if not m.loaded:
            lines.append("En attente du démon de rappels")
        elif nxt and nxt["start"] > t:
            lines.append(f"{nxt['start']:%H:%M}  {nxt['summary']}  (dans {fmt_delta(nxt['start'] - t)})")
        elif nxt:
            lines.append(f"En cours depuis {fmt_delta(t - nxt['start'])} : {nxt['summary']}")
        else:
            lines.append("Aucun rdv imminent")
        rest = [e for e in m.events if e["start"].date() == t.date() and e["start"] > t and e is not nxt]
        if rest:
            lines.append(f"{len(rest)} autre(s) rdv aujourd'hui")
        if until:
            lines.append(f"Rappels en silence jusqu'à {until:%H:%M}")
        if m.loaded and m.error:
            lines.append("Serveur injoignable")
        self.icon.setToolTip("Kitot\n" + "\n".join(lines))

    def build_menu(self):
        menu = self.menu
        menu.clear()
        menu.addAction(QIcon.fromTheme("view-calendar-day"), "Afficher l'agenda", self.show_window)
        nxt = self.model.next_alert()
        if nxt:
            menu.addSeparator()
            hdr = QAction(f"{nxt['start']:%H:%M}  {nxt['summary'][:40]}", menu)
            hdr.setEnabled(False)
            menu.addAction(hdr)
            if nxt["link"]:
                menu.addAction(QIcon.fromTheme("call-start"), nxt["link"][0], lambda e=nxt: self.window.join(e))
            if nxt["start"] - now() < timedelta(minutes=30):
                menu.addAction(QIcon.fromTheme("dialog-ok"), "J'y suis", lambda e=nxt: self.window.ack(e))
        menu.addSeparator()
        if self.model.silence_until():
            menu.addAction(QIcon.fromTheme("notifications"), "Réactiver les rappels", lambda: self.silence(None))
        else:
            menu.addAction(QIcon.fromTheme("notifications-disabled"), "Silence 1 h", lambda: self.silence(1))
            menu.addAction("Silence 2 h", lambda: self.silence(2))
        menu.addAction(QIcon.fromTheme("view-refresh"), "Actualiser", self.refresh)
        menu.addSeparator()
        menu.addAction(QIcon.fromTheme("application-exit"), "Quitter l'icône", self.app.quit)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    def show_window(self):
        self.window.rebuild()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def silence(self, hours: int | None):
        set_silence(hours)
        QTimer.singleShot(200, self.reload)

    def refresh(self):
        send_command({"cmd": "refresh"})


def main():
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Kitot")
    app.setDesktopFileName(APP)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(render_icon("idle"))
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Aucune boîte à miniatures disponible.", file=sys.stderr)
        return 1
    tray = Tray(app)  # noqa: F841
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
