# kitot

> Parce qu'il vaut mieux être prévenu *tôt*. Rappels de rendez-vous CalDAV pour KDE Plasma.

kitot surveille vos agendas CalDAV et vous prévient avant chaque rendez-vous, avec des
notifications Plasma interactives et une icône compte à rebours près de l'horloge.

Il est né d'un besoin simple : les invitations reçues d'Outlook ou de Teams arrivent souvent
**sans alarme** dans l'agenda, et on finit par rater des réunions. kitot impose donc sa propre
politique de rappel, quel que soit le contenu de l'agenda.

## Fonctionnalités

- **Tous les agendas du compte**, découverts automatiquement à partir de l'URL « home » CalDAV.
  Chaque agenda garde la couleur définie sur le serveur.
- **Rappels en cascade** : 30 min avant, 5 min avant, puis à l'heure du rdv. Le dernier reste
  affiché jusqu'à ce qu'on le ferme et joue une alarme. Les autres jouent un son discret.
- **Boutons sur les rappels** :
  - « Rappeler dans 5 min » ;
  - « Rejoindre Teams », ou « Ouvrir le lien », quand le rdv contient un lien de réunion ;
  - « Silence 1 h » et « Silence 2 h » ;
  - « J'y suis », au moment du rdv, qui arrête tous les rappels de ce rdv.
- **Alarmes de l'agenda** (VALARM) respectées en plus de la cascade.
- **Récapitulatifs** : les rdv du jour le matin, ceux du lendemain en fin de journée.
- **Rdv déplacé ou annulé** : notification.
- **Rattrapage** : après une mise en veille ou un silence, le rappel manqué part tant que le rdv
  n'est pas terminé.
- **Mode « Ne pas déranger »** de Plasma respecté : aucun son.
- **Icône dans la boîte à miniatures** : une page d'éphéméride à la date du jour, qui affiche les
  minutes restantes à l'approche d'un rdv (orange, puis rouge). Un clic ouvre la liste des rdv
  d'aujourd'hui et de demain, avec les boutons Teams, « J'y suis », silence et actualisation.

## Prérequis

- KDE Plasma 6 (testé sur Kubuntu 26.04, Plasma 6.6, Wayland)
- Python 3.11 ou plus récent, avec les paquets système `python3-venv` et `python3-pyqt6`
- PipeWire ou PulseAudio pour les sons (`pw-play` ou `paplay`)
- Un portefeuille compatible Secret Service pour le mot de passe : KWallet convient
- Un serveur CalDAV. Testé avec Oracle Communications Calendar Server.

## Installation

```bash
git clone https://github.com/Sebbri67/kitot.git
cd kitot
./install.sh
```

Le script crée l'environnement Python, installe les services systemd utilisateur et copie la
configuration d'exemple dans `~/.config/kitot/config.ini`. Ensuite :

```bash
nano ~/.config/kitot/config.ini                 # renseigner url et user
.venv/bin/python rappels.py set-password        # mot de passe stocké dans le portefeuille
.venv/bin/python rappels.py list                # vérifier : rdv des 24 prochaines heures
systemctl --user enable --now kitot.service kitot-tray.service
```

Le démon et l'icône démarrent ensuite à chaque ouverture de session.

## Configuration

Toutes les clés sont décrites dans [`config.example.ini`](config.example.ini). Les principales :

| clé                 | défaut | rôle                                                        |
|---------------------|--------|-------------------------------------------------------------|
| url                 |        | URL CalDAV « home » du compte                               |
| user                |        | identifiant de connexion (uid ou adresse mail selon le serveur) |
| calendars           | (vide) | agendas à surveiller, séparés par des virgules (vide = tous) |
| before_minutes      | 30,5,0 | cascade de rappels, en minutes avant le rdv (0 = à l'heure) |
| critical_last       | true   | dernier rappel en urgence critique (reste affiché)          |
| sound_last          | true   | alarme sur le rappel à l'heure du rdv                       |
| sound_reminders     | true   | son discret sur les autres rappels et récapitulatifs        |
| use_calendar_alarms | true   | respecter aussi les alarmes définies dans l'agenda          |
| snooze_minutes      | 5      | durée du bouton « Rappeler dans X min »                     |
| digest_hour         | 8      | heure du récapitulatif du jour (0 = désactivé)              |
| eve_digest_hour     | 17     | heure du récapitulatif du lendemain (0 = désactivé)         |
| notify_changes      | true   | signaler les rdv déplacés ou annulés                        |
| interval_seconds    | 300    | fréquence de relevé du serveur                              |
| window_hours        | 48     | fenêtre surveillée (au moins la plus longue alarme)         |
| allday_hour         | 8      | heure de rappel des événements « journée entière »          |

Après une modification : `systemctl --user restart kitot.service`.

## Utilisation en ligne de commande

```bash
.venv/bin/python rappels.py calendars    # agendas découverts
.venv/bin/python rappels.py list         # rdv à venir, alarmes et liens détectés
.venv/bin/python rappels.py digest       # récapitulatif du jour, tout de suite
.venv/bin/python rappels.py pause 2h     # silence (1h par défaut, ou 45m…)
.venv/bin/python rappels.py resume       # fin du silence
journalctl --user -u kitot -n 30         # journal du démon
```

## Fonctionnement

- `rappels.py` est le démon. Il relève les agendas, envoie les notifications par D-Bus et joue
  les sons. Il publie son état dans `~/.local/state/kitot/agenda.json`.
- `tray.py` est l'icône de la boîte à miniatures. Elle lit cet état et renvoie ses commandes au
  démon par le fichier `~/.local/state/kitot/commands`. Elle tourne avec le Python du système,
  pour profiter de PyQt6.
- `icons.py` dessine les icônes « page d'éphéméride ».

Plasma ne joue pas les sons des notifications envoyées avec des boutons. kitot les joue donc
lui-même, sur la sortie audio par défaut.

## Licence

[GPL-3.0-or-later](LICENSE)
