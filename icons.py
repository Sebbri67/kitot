#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Icônes « page d'éphéméride » : bandeau de couleur + grand chiffre.

Utilisé par :
  - le démon, pour l'icône des notifications (lancé en ligne de commande avec le Python du système) :
        icons.py COULEUR JOUR_SEMAINE NUMERO FICHIER.png
  - tray.py, qui importe draw_page() pour l'icône de la boîte à miniatures.
"""

import os
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap

PAGE_BG = QColor("#fcfcfc")
TEXT = QColor("#232629")


def _font(px: float, bold: bool = True) -> QFont:
    f = QFont("Noto Sans")
    f.setPixelSize(max(1, int(px)))
    f.setBold(bold)
    return f


def draw_page(size: int, color: str | QColor, header: str, body: str,
              body_color: str | QColor | None = None, dimmed: bool = False) -> QPixmap:
    """Page d'éphéméride carrée : bandeau `color` portant `header`, `body` en grand dessous."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    if dimmed:
        p.setOpacity(0.55)
    m = size * 0.06                       # marge
    box = QRectF(m, m, size - 2 * m, size - 2 * m)
    band = QRectF(box.x(), box.y(), box.width(), box.height() * 0.32)
    rest = QRectF(box.x(), band.bottom(), box.width(), box.bottom() - band.bottom())
    path = QPainterPath()
    path.addRoundedRect(box, size * 0.15, size * 0.15)
    p.setClipPath(path)
    p.fillRect(box, PAGE_BG)
    p.fillRect(band, QColor(color))
    p.setClipping(False)
    p.setPen(QPen(QColor(0, 0, 0, 45), max(1.0, size / 80)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    if header and size >= 48:             # en petite taille, le bandeau seul suffit
        p.setPen(QColor("white"))
        p.setFont(_font(band.height() * 0.58))
        p.drawText(band, Qt.AlignmentFlag.AlignCenter, header)
    p.setPen(QColor(body_color) if body_color else TEXT)
    p.setFont(_font(rest.height() * (0.80 if len(body) <= 2 else 0.62)))
    p.drawText(rest.adjusted(0, -rest.height() * 0.04, 0, 0), Qt.AlignmentFlag.AlignCenter, body)
    p.end()
    return pm


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication
    color, header, body, out = sys.argv[1:5]
    _app = QGuiApplication(sys.argv[:1])
    if not draw_page(128, color, header, body).save(out, "PNG"):
        raise SystemExit(f"écriture impossible : {out}")


if __name__ == "__main__":
    main()
