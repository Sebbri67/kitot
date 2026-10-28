#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installation de kitot pour l'utilisateur courant (aucun droit root nécessaire).
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"
CONF="$HOME/.config/kitot/config.ini"

echo "== Dépendances système"
missing=()
python3 -c "import PyQt6.QtWidgets, PyQt6.QtSvg" 2>/dev/null || missing+=(python3-pyqt6)
python3 -c "import venv, ensurepip" 2>/dev/null || missing+=(python3-venv)
command -v pw-play >/dev/null || command -v paplay >/dev/null || missing+=(pipewire-bin)
if ((${#missing[@]})); then
    echo "Paquets manquants : ${missing[*]}"
    echo "Installez-les avec : sudo apt install ${missing[*]}"
    exit 1
fi

echo "== Environnement Python"
[ -x "$APPDIR/.venv/bin/python" ] || python3 -m venv "$APPDIR/.venv"
"$APPDIR/.venv/bin/pip" install -q -r "$APPDIR/requirements.txt"

echo "== Services et entrée de bureau"
mkdir -p "$UNITS" "$APPS"
for f in kitot.service kitot-tray.service; do
    sed "s#@APPDIR@#$APPDIR#g" "$APPDIR/packaging/$f" > "$UNITS/$f"
done
sed "s#@APPDIR@#$APPDIR#g" "$APPDIR/packaging/kitot.desktop" > "$APPS/kitot.desktop"
systemctl --user daemon-reload

if [ ! -f "$CONF" ]; then
    mkdir -p "$(dirname "$CONF")"
    cp "$APPDIR/config.example.ini" "$CONF"
    echo
    echo "Configuration créée : $CONF"
    echo "1. Renseignez url et user dans ce fichier."
    echo "2. Enregistrez le mot de passe : $APPDIR/.venv/bin/python $APPDIR/rappels.py set-password"
    echo "3. Testez : $APPDIR/.venv/bin/python $APPDIR/rappels.py list"
    echo "4. Activez : systemctl --user enable --now kitot.service kitot-tray.service"
else
    systemctl --user enable --now kitot.service kitot-tray.service
    echo "kitot est installé et lancé."
fi
