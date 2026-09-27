"""Generate ``strings.json`` and the translation catalogues from one table.

Written as a generator rather than three hand-maintained JSON files because
§10.5 requires a test asserting that every key present in one language exists
in the others, with the same placeholders. Hand-editing three files keeps that
test permanently red for uninteresting reasons; generating them from a single
table makes the test a real check on the *table* instead.

``strings.json`` is the English source of truth (§10.1), ``translations/en.json``
is its copy, and ``translations/fr.json`` carries the French. Nothing
user-visible is hard-coded anywhere in ``custom_components/`` -- that is what
the accompanying test verifies.

``services.yaml`` and ``icons.json`` come from here too, for the same reason:
one table, and a test that asserts each file is its output.

Run: ``python scripts/build_translations.py``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "carnet_scolaire"


# ---------------------------------------------------------------------------
# Entities. `(key, english, french)` per platform.
#
# Names are what a user reads in a picker, so they are nouns describing the
# fact, not the mechanism: "Next lesson", not "Timetable tier next item".
# ---------------------------------------------------------------------------

SENSORS: list[tuple[str, str, str]] = [
    ("next_lesson", "Next lesson", "Prochain cours"),
    ("end_of_lessons", "End of lessons", "Fin des cours"),
    ("morning_end", "End of morning", "Fin de matinée"),
    ("next_cancellation", "Next cancellation", "Prochaine annulation"),
    ("next_wake_up", "Wake-up time", "Prochain réveil"),
    ("next_test", "Next test", "Prochain contrôle"),
    ("lessons_today", "Lessons today", "Cours du jour"),
    ("homework_todo", "Homework to do", "Devoirs à faire"),
    ("homework_tomorrow", "Homework for tomorrow", "Devoirs pour demain"),
    ("latest_grade", "Latest grade", "Dernière note"),
    ("overall_average", "Overall average", "Moyenne générale"),
    ("class_average", "Class average", "Moyenne de la classe"),
    ("next_punishment", "Next detention", "Prochaine punition"),
    ("unjustified_absences", "Unjustified absences", "Absences non justifiées"),
    ("unread_information", "Unread news", "Actualités non lues"),
    ("unread_messages", "Unread messages", "Messages non lus"),
    ("current_period", "Current period", "Période en cours"),
    ("timetable_tomorrow", "Timetable tomorrow", "Emploi du temps de demain"),
    ("timetable_week", "Timetable this week", "Emploi du temps de la semaine"),
    ("homework", "Homework", "Devoirs"),
    ("grades", "Grades", "Notes"),
    ("averages", "Subject averages", "Moyennes par matière"),
    ("report_card", "Report card", "Bulletin"),
    ("absences", "Absences", "Absences"),
    ("delays", "Late arrivals", "Retards"),
    ("punishments", "Punishments", "Punitions"),
    ("evaluations", "Skill assessments", "Évaluations"),
    ("information", "News", "Actualités"),
    ("discussions", "Discussions", "Discussions"),
    ("menu_today", "Menu today", "Menu du jour"),
    ("menu_tomorrow", "Menu tomorrow", "Menu de demain"),
    ("teaching_staff", "Teaching staff", "Équipe pédagogique"),
    ("class_name", "Class", "Classe"),
    ("periods", "Periods", "Périodes"),
]

# One set per closed period. The period's own label is a placeholder because it
# comes from PRONOTE and is not ours to translate (§10.2, §10.6).
HISTORY_SENSORS: list[tuple[str, str, str]] = [
    ("grades_period", "Grades ({period})", "Notes ({period})"),
    (
        "averages_period",
        "Subject averages ({period})",
        "Moyennes par matière ({period})",
    ),
    (
        "overall_average_period",
        "Overall average ({period})",
        "Moyenne générale ({period})",
    ),
    ("report_card_period", "Report card ({period})", "Bulletin ({period})"),
    ("absences_period", "Absences ({period})", "Absences ({period})"),
    ("delays_period", "Late arrivals ({period})", "Retards ({period})"),
    ("punishments_period", "Punishments ({period})", "Punitions ({period})"),
    (
        "evaluations_period",
        "Skill assessments ({period})",
        "Évaluations ({period})",
    ),
]

DIAGNOSTIC_SENSORS: list[tuple[str, str, str]] = [
    ("calls_today", "Requests today", "Appels du jour"),
    ("remaining_budget", "Remaining budget", "Budget restant"),
    ("last_collection", "Last collection", "Dernière collecte"),
    ("next_collection", "Next collection", "Prochaine collecte"),
    ("session_age", "Session age", "Âge de la session"),
    ("session_lifetime", "Measured session lifetime", "Durée de vie de la session"),
    ("logins_today", "Logins today", "Connexions du jour"),
    ("limiter_state", "Rate limiter state", "État du limiteur"),
]

# The closed value set of `sensor.<account>_etat_limiteur`. An automation
# compares the raw state; only the label is translated (§10.3).
LIMITER_STATES: list[tuple[str, str, str]] = [
    ("nominal", "Nominal", "Nominal"),
    ("throttled", "Throttled", "Bridé"),
    ("backoff", "Backing off", "Temporisation"),
    ("quiet_hours", "Quiet hours", "Heures calmes"),
    ("credentials_hold", "Login attempts paused", "Connexions suspendues"),
    ("bootstrap_failed", "Cannot reach the session page", "Page de session illisible"),
]

BINARY_SENSORS: list[tuple[str, str, str]] = [
    ("school_day", "School day", "Jour de classe"),
    ("in_class", "In class", "En cours"),
    ("lessons_canceled", "Lessons cancelled", "Cours annulés"),
    ("outing_today", "Educational outing", "Sortie pédagogique"),
    ("test_today", "Test today", "Contrôle prévu"),
    ("holidays", "Holidays", "Vacances"),
    ("homework_overdue", "Homework overdue", "Devoirs en retard"),
    ("absence_in_progress", "Absence in progress", "Absence en cours"),
    ("punishment_upcoming", "Detention scheduled", "Punition à venir"),
    ("throttled", "Collections throttled", "Collectes bridées"),
]

CALENDARS: list[tuple[str, str, str]] = [
    ("timetable", "Timetable", "Emploi du temps"),
    ("homework", "Homework", "Devoirs"),
    ("punishments", "Detentions", "Punitions"),
]

TODOS: list[tuple[str, str, str]] = [("homework", "Homework", "Devoirs")]

BUTTONS: list[tuple[str, str, str]] = [
    ("refresh", "Refresh", "Rafraîchir"),
    ("refresh_marks", "Refresh grades", "Rafraîchir les notes"),
]

EVENTS: list[tuple[str, str, str]] = [
    ("new_grade", "New grade", "Nouvelle note"),
    ("new_homework", "New homework", "Nouveau devoir"),
    ("lesson_changed", "Lesson changed", "Cours modifié"),
    ("new_information", "New news item", "Nouvelle actualité"),
    ("new_absence", "New absence", "Nouvelle absence"),
    ("new_delay", "New late arrival", "Nouveau retard"),
    ("new_punishment", "New punishment", "Nouvelle punition"),
    ("new_message", "New message", "Nouveau message"),
    ("new_evaluation", "New skill assessment", "Nouvelle évaluation"),
]

IMAGES: list[tuple[str, str, str]] = [("photo", "Photo", "Photo")]

# ---------------------------------------------------------------------------
# Tier labels, reused by the options page and the refresh service.
# ---------------------------------------------------------------------------

TIERS: list[tuple[str, str, str]] = [
    ("timetable", "Timetable", "Emploi du temps"),
    ("homework", "Homework", "Devoirs"),
    ("news", "News", "Actualités"),
    ("discussions", "Discussions", "Discussions"),
    ("marks", "Grades", "Notes"),
    ("attendance", "Attendance", "Vie scolaire"),
    ("evaluations", "Skill assessments", "Évaluations"),
    ("menus", "Canteen menus", "Menus"),
    ("static", "Rarely-changing data", "Données stables"),
    ("history", "Closed periods", "Périodes closes"),
]

# ---------------------------------------------------------------------------
# Config and options flow
# ---------------------------------------------------------------------------

CONFIG_EN: dict[str, Any] = {
    "step": {
        "user": {
            "title": "Carnet scolaire",
            "description": (
                "![Carnet scolaire]({logo})\n\n"
                "Which platform does your child's school use? Choose the one "
                "you log in to for the timetable, homework and grades."
            ),
            "menu_options": {
                "pronote": "PRONOTE",
                "ecoledirecte": "EcoleDirecte",
            },
        },
        "pronote": {
            "title": "PRONOTE",
            "description": (
                "Choose how to connect. The QR code from the PRONOTE mobile "
                "app is the most reliable method: it enrols this Home "
                "Assistant as a device and avoids storing your password."
            ),
            "menu_options": {
                "qr_code": "QR code from the mobile app (recommended)",
                "credentials": "Username and password",
                "ent": "Federated login (ENT)",
            },
        },
        "reconfigure": {
            "title": "Change the PRONOTE connection",
            "description": (
                "This entry connects to {url}. Choose how it should connect "
                "from now on -- the same method to correct the address, the "
                "username or the portal, or another one. The next form asks "
                "for the password or the QR code again: neither is shown "
                "back.\n\nThe new details are tried once before anything is "
                "saved, and must open this same account. The children you "
                "follow, the options and the history are kept."
            ),
            "menu_options": {
                "qr_code": "QR code from the mobile app (recommended)",
                "credentials": "Username and password",
                "ent": "Federated login (ENT)",
            },
        },
        "qr_code": {
            "title": "Enrol with a QR code",
            "description": (
                "In the PRONOTE mobile app, open the account menu and generate "
                "a QR code, choosing a four-digit code when asked.\n\n"
                "The app draws the QR code as a picture, and the field below "
                "wants the JSON it encodes. So scan it with a QR code reader "
                "— your phone's camera or any scanner app — and share or copy "
                "the text it gives you, then paste that text below with the "
                "same four-digit code.\n\n"
                "A QR code is single-use **and expires within minutes**, so "
                "the order matters: open this form first, generate the QR code "
                "second, and submit straight away. Generate a new one if this "
                "attempt fails -- the old one is spent either way."
            ),
            "data": {
                "qr_payload": "QR code content (JSON)",
                "qr_pin": "Four-digit code",
                "device_name": "Device name",
                "account_pin": "Two-factor PIN (if your account uses one)",
            },
            "data_description": {
                "qr_payload": (
                    "The text a QR code reader returns when it scans the code "
                    "the app draws: a JSON object with login, jeton and url. "
                    "Paste it whole, not a photo or a link to the picture."
                ),
                "device_name": (
                    "The name PRONOTE shows for this Home Assistant in the "
                    "list of devices connected to the account -- only there. "
                    "It does not name the device in Home Assistant, which "
                    "takes the pupil's name from PRONOTE."
                ),
                "qr_pin": (
                    "The one you chose in the app while generating the QR "
                    "code. Not the account PIN below -- both are four digits, "
                    "and they are different codes."
                ),
                "account_pin": (
                    "The two-factor PIN set on the PRONOTE account itself, if "
                    "it has one. Also four digits, and not the code above. "
                    "Used for this login only and never stored. You will be "
                    "asked for it again if PRONOTE requires it."
                ),
            },
        },
        "credentials": {
            "title": "Username and password",
            "description": (
                "Enter the address of your establishment's PRONOTE space, "
                "ending in eleve.html or parent.html."
            ),
            "data": {
                "pronote_url": "PRONOTE address",
                "username": "Username",
                "password": "Password",
                "account_pin": "Two-factor PIN (if your account uses one)",
            },
            "data_description": {
                "pronote_url": (
                    "The address of the establishment's PRONOTE page as your "
                    "browser shows it, ending in parent.html for a parent "
                    "account or eleve.html for a pupil's. Anything after .html "
                    "is dropped before it is stored."
                ),
                "username": (
                    "The username typed on the PRONOTE login page itself -- not "
                    "the one of a regional portal (ENT)."
                ),
                "password": (
                    "The PRONOTE password of that account. It is stored, since "
                    "this method has no other way to log in again; the QR code "
                    "method avoids that."
                ),
                "account_pin": (
                    "Used for this login only and never stored. You will be "
                    "asked for it again if PRONOTE requires it."
                ),
            },
        },
        "ent": {
            "title": "Federated login",
            "description": (
                "Select your regional education portal. Your credentials are "
                "the ones for that portal, not for PRONOTE itself."
            ),
            "data": {
                "pronote_url": "PRONOTE address",
                "username": "Username",
                "password": "Password",
                "ent": "Portal",
            },
            "data_description": {
                "pronote_url": (
                    "The address of the establishment's PRONOTE page, ending in "
                    "parent.html or eleve.html: the page the portal opens once "
                    "you are logged in, not the portal's own address."
                ),
                "username": (
                    "The username typed on the regional portal (ENT), not a "
                    "PRONOTE one."
                ),
                "password": (
                    "The password of that portal account. It is stored, since a "
                    "federated login goes through the portal again every time."
                ),
                "ent": (
                    "The portal your establishment signs you in through. Pick it "
                    "from the list rather than typing a name: an unknown name is "
                    "refused."
                ),
            },
        },
        "ecoledirecte": {
            "title": "Connect to EcoleDirecte",
            "description": (
                "Enter the credentials used to sign in to EcoleDirecte. The "
                "password is stored because EcoleDirecte does not issue a "
                "durable device token."
            ),
            "data": {
                "username": "Username",
                "password": "Password",
            },
            "data_description": {
                "username": (
                    "The username typed on the EcoleDirecte login page, for a "
                    "parent or a pupil account."
                ),
                "password": (
                    "The EcoleDirecte password of that account. It is stored, "
                    "since EcoleDirecte issues no durable device token."
                ),
            },
        },
        "ecoledirecte_qcm": {
            "title": "EcoleDirecte security question",
            "description": (
                "EcoleDirecte asks: {question}\n\n"
                "Select the remembered answer. No account is created until "
                "the login succeeds."
            ),
            "data": {"choice": "Answer"},
            "data_description": {
                "choice": (
                    "The answer EcoleDirecte expects for this account, as its "
                    "own site asks it on a new device. It is remembered, so "
                    "the question is not asked at every login."
                ),
            },
        },
        "children": {
            "title": "Which children to follow",
            "description": (
                "Each child you follow multiplies the number of daily requests "
                "to the server, so select only the ones you want."
            ),
            "data": {"children": "Children"},
            "data_description": {
                "children": (
                    "Each ticked child gets its own device and entities. Untick "
                    "a child you do not need: every child adds the same number "
                    "of requests a day."
                ),
            },
        },
        "reauth_confirm": {
            "title": "Reconnect to PRONOTE",
            "description": (
                "The connection to {url} was refused. Enter a corrected "
                "password, or the two-factor PIN PRONOTE is asking for. The "
                "PIN is used once and never stored."
            ),
            "data": {
                "password": "Password",
                "account_pin": "Two-factor PIN",
            },
            "data_description": {
                "password": (
                    "Leave empty to keep the stored password, for instance "
                    "when PRONOTE is only asking for the two-factor PIN."
                ),
                "account_pin": (
                    "The two-factor PIN set on the PRONOTE account, when "
                    "PRONOTE asks for it. Used for this login only and never "
                    "stored."
                ),
            },
        },
        "reauth_qr": {
            "title": "Reconnect with a new QR code",
            "description": (
                "The connection to {url} was refused. This account was "
                "enrolled from a QR code, so it has no password to correct: "
                "PRONOTE issues a new access token at every login, and once "
                "the stored one is refused the only way back in is a new QR "
                "code.\n\nIn the PRONOTE mobile app, generate a fresh QR "
                "code, choosing a four-digit code when asked, then paste its "
                "content below. Nothing else about this account changes -- the "
                "children you follow, your settings and your history are all "
                "kept."
            ),
            "data": {
                "qr_payload": "QR code content (JSON)",
                "qr_pin": "Four-digit code",
                "device_name": "Device name",
                "account_pin": "Two-factor PIN (if your account uses one)",
            },
            "data_description": {
                "qr_payload": (
                    "The text a QR code reader returns when it scans the code "
                    "the app draws: a JSON object with login, jeton and url. "
                    "Paste it whole, not a photo or a link to the picture."
                ),
                "device_name": (
                    "The name PRONOTE shows for this Home Assistant in the "
                    "list of devices connected to the account -- only there. "
                    "It does not name the device in Home Assistant, which "
                    "takes the pupil's name from PRONOTE."
                ),
                "qr_pin": (
                    "The one you chose in the app while generating the QR "
                    "code. Not the account PIN below -- both are four digits, "
                    "and they are different codes."
                ),
                "account_pin": (
                    "The two-factor PIN set on the PRONOTE account itself, if "
                    "it has one. Also four digits, and not the code above. "
                    "Used for this login only and never stored. You will be "
                    "asked for it again if PRONOTE requires it."
                ),
            },
        },
    },
    "error": {
        "invalid_auth": (
            "PRONOTE refused these credentials. Repeatedly retrying a wrong "
            "password is what gets an address blocked, so check them before "
            "trying again."
        ),
        "mfa_required": (
            "PRONOTE is asking for the two-factor PIN of this account. Enter it above."
        ),
        "bootstrap_failed": (
            "The address answered, but did not return a PRONOTE session page. "
            "Check the address, and that the space is open right now."
        ),
        "ent_required": (
            "This establishment does not allow direct login to PRONOTE: the "
            "address leads to its ENT portal's own login page. Nothing was "
            "sent and your password was not refused. Go back to the login "
            "methods and choose “Federated login (ENT)”, with the "
            "portal's username and password -- or the app's QR code, if the "
            "establishment has enabled it."
        ),
        "invalid_qr": (
            "The QR code or its four-digit code was refused. A QR code can "
            "only be used once, so generate a new one in the app."
        ),
        "qr_refused": (
            "PRONOTE would not honour this QR code. Its four-digit code was "
            "right -- a wrong one is reported separately, and is caught before "
            "anything is sent -- so what was refused is the QR code itself. "
            "Almost always that means it has already been used, or was "
            "generated more than a few minutes ago: PRONOTE issues them for "
            "single use and expires them quickly. Generate a new one and use "
            "it straight away, with the form already open. If a QR code you "
            "generated seconds earlier is refused too, the account may not be "
            "allowed to enrol a new device -- check with the establishment "
            "rather than retrying, because repeatedly retrying a refused login "
            "is what gets an address blocked."
        ),
        "invalid_qr_payload": (
            "That does not look like the content of a PRONOTE QR code. It "
            "should be a JSON object with login, jeton and url -- the first "
            "two written in hexadecimal. Paste it whole, exactly as the app "
            "gives it."
        ),
        "unknown_ent": (
            "No ENT provider by that name is installed. Pick one from the list "
            "rather than typing it in: an unrecognised name does not fall back "
            "to anything sensible, it sends this portal's username and "
            "password to the PRONOTE server instead."
        ),
        "invalid_qcm": "Select one of the proposed answers.",
        "cannot_connect": "Could not reach the server.",
        "ed_invalid_auth": (
            "EcoleDirecte refused these credentials. Check them before trying again."
        ),
        "rate_limited": (
            "Too many login attempts in the last hour, so this one was not "
            "sent. Wait a little before trying again: repeatedly retrying a "
            "login is what gets an address blocked."
        ),
        "unknown": "Unexpected error. Check the Home Assistant log.",
    },
    "abort": {
        "already_configured": "This account is already configured.",
        "reauth_successful": "Reconnected.",
        "reconfigure_successful": (
            "Connection updated. The entry is reloading with it; its devices, "
            "entities, history and options are unchanged."
        ),
        "reconfigure_wrong_account": (
            "These details sign in to a different account from the one this "
            "entry follows, so nothing was changed: applying them would have "
            "pointed this entry at somebody else's child while keeping this "
            "child's name and history. Use the details of this entry's own "
            "account, or add the other account separately."
        ),
        "wrong_account": (
            "That QR code belongs to a different PRONOTE account, so it was "
            "not applied: reconnecting this one with it would have pointed it "
            "at somebody else's child while keeping this child's name and "
            "history. Generate a QR code from the account this entry follows, "
            "or add the other account separately."
        ),
    },
}

CONFIG_FR: dict[str, Any] = {
    "step": {
        "user": {
            "title": "Carnet scolaire",
            "description": (
                "![Carnet scolaire]({logo})\n\n"
                "Quelle plateforme l'établissement de votre enfant "
                "utilise-t-il ? Choisissez celle sur laquelle vous consultez "
                "l'emploi du temps, les devoirs et les notes."
            ),
            "menu_options": {
                "pronote": "PRONOTE",
                "ecoledirecte": "EcoleDirecte",
            },
        },
        "pronote": {
            "title": "PRONOTE",
            "description": (
                "Choisissez le mode de connexion. Le QR code de l'application "
                "mobile PRONOTE est le plus fiable : il enrôle ce Home "
                "Assistant comme appareil et évite de conserver votre mot de "
                "passe."
            ),
            "menu_options": {
                "qr_code": "QR code de l'application mobile (recommandé)",
                "credentials": "Identifiant et mot de passe",
                "ent": "Connexion par l'ENT",
            },
        },
        "reconfigure": {
            "title": "Modifier la connexion PRONOTE",
            "description": (
                "Cette entrée se connecte à {url}. Choisissez comment elle "
                "doit se connecter désormais : le même mode pour corriger "
                "l'adresse, l'identifiant ou le portail, ou un autre. Le "
                "formulaire suivant redemande le mot de passe ou le QR code : "
                "aucun des deux n'est réaffiché.\n\nLes nouvelles informations "
                "sont essayées une fois avant tout enregistrement, et doivent "
                "ouvrir ce même compte. Les enfants suivis, les options et "
                "l'historique sont conservés."
            ),
            "menu_options": {
                "qr_code": "QR code de l'application mobile (recommandé)",
                "credentials": "Identifiant et mot de passe",
                "ent": "Connexion par l'ENT",
            },
        },
        "qr_code": {
            "title": "Enrôler avec un QR code",
            "description": (
                "Dans l'application mobile PRONOTE, ouvrez le menu du compte "
                "et générez un QR code, en choisissant un code à quatre "
                "chiffres lorsqu'il vous est demandé.\n\n"
                "L'application affiche le QR code sous forme d'image, alors "
                "que le champ ci-dessous attend le JSON qu'elle encode. "
                "Utilisez donc un lecteur de QR code — l'appareil photo de "
                "votre téléphone ou n'importe quelle application de scan — "
                "pour le lire, puis partagez ou copiez le texte obtenu et "
                "collez-le ci-dessous avec le même code à quatre "
                "chiffres.\n\n"
                "Un QR code est à usage unique **et périme en quelques "
                "minutes** : l'ordre compte donc. Ouvrez d'abord ce "
                "formulaire, générez le QR code ensuite, et validez "
                "immédiatement. Regénérez-en un si cette tentative échoue — "
                "l'ancien est dépensé de toute façon."
            ),
            "data": {
                "qr_payload": "Contenu du QR code (JSON)",
                "qr_pin": "Code à quatre chiffres",
                "device_name": "Nom de l'appareil",
                "account_pin": "Code PIN à deux facteurs (si votre compte en a un)",
            },
            "data_description": {
                "qr_payload": (
                    "Le texte que renvoie un lecteur de QR code en scannant le "
                    "code affiché par l'application : un objet JSON avec login, "
                    "jeton et url. Collez-le en entier, pas une photo ni un lien "
                    "vers l'image."
                ),
                "device_name": (
                    "Le nom sous lequel PRONOTE affiche ce Home Assistant dans "
                    "la liste des appareils connectés au compte, et seulement "
                    "là. Il ne nomme pas l'appareil dans Home Assistant, qui "
                    "prend le nom de l'élève fourni par PRONOTE."
                ),
                "qr_pin": (
                    "Celui que vous avez choisi dans l'application en générant "
                    "le QR code. Pas le code PIN du compte ci-dessous : les "
                    "deux font quatre chiffres, et ce sont deux codes "
                    "différents."
                ),
                "account_pin": (
                    "Le code PIN à deux facteurs défini sur le compte PRONOTE "
                    "lui-même, s'il en a un. Quatre chiffres aussi, et pas "
                    "celui du dessus. Utilisé pour cette connexion uniquement "
                    "et jamais conservé. Il vous sera redemandé si PRONOTE "
                    "l'exige."
                ),
            },
        },
        "credentials": {
            "title": "Identifiant et mot de passe",
            "description": (
                "Saisissez l'adresse de l'espace PRONOTE de l'établissement, "
                "terminée par eleve.html ou parent.html."
            ),
            "data": {
                "pronote_url": "Adresse PRONOTE",
                "username": "Identifiant",
                "password": "Mot de passe",
                "account_pin": "Code PIN à deux facteurs (si votre compte en a un)",
            },
            "data_description": {
                "pronote_url": (
                    "L'adresse de la page PRONOTE de l'établissement telle que "
                    "votre navigateur l'affiche, terminée par parent.html pour "
                    "un compte parent ou eleve.html pour un compte élève. Tout "
                    "ce qui suit .html est retiré avant d'être conservé."
                ),
                "username": (
                    "L'identifiant saisi sur la page de connexion de PRONOTE "
                    "elle-même, et non celui d'un portail académique (ENT)."
                ),
                "password": (
                    "Le mot de passe PRONOTE de ce compte. Il est conservé, car "
                    "ce mode n'a pas d'autre moyen de se reconnecter ; le QR "
                    "code évite cela."
                ),
                "account_pin": (
                    "Utilisé pour cette connexion uniquement et jamais "
                    "conservé. Il vous sera redemandé si PRONOTE l'exige."
                ),
            },
        },
        "ent": {
            "title": "Connexion par l'ENT",
            "description": (
                "Sélectionnez votre portail académique. Les identifiants sont "
                "ceux du portail, pas ceux de PRONOTE."
            ),
            "data": {
                "pronote_url": "Adresse PRONOTE",
                "username": "Identifiant",
                "password": "Mot de passe",
                "ent": "Portail",
            },
            "data_description": {
                "pronote_url": (
                    "L'adresse de la page PRONOTE de l'établissement, terminée "
                    "par parent.html ou eleve.html : la page que le portail "
                    "ouvre une fois connecté, pas l'adresse du portail."
                ),
                "username": (
                    "L'identifiant saisi sur le portail académique (ENT), pas "
                    "un identifiant PRONOTE."
                ),
                "password": (
                    "Le mot de passe de ce compte de portail. Il est conservé, "
                    "car une connexion fédérée repasse par le portail à chaque "
                    "fois."
                ),
                "ent": (
                    "Le portail par lequel votre établissement vous connecte. "
                    "Choisissez-le dans la liste plutôt que de saisir un nom : "
                    "un nom inconnu est refusé."
                ),
            },
        },
        "ecoledirecte": {
            "title": "Se connecter à EcoleDirecte",
            "description": (
                "Saisissez les identifiants utilisés pour EcoleDirecte. Le mot "
                "de passe est conservé, car EcoleDirecte ne fournit pas de "
                "jeton d'appareil durable."
            ),
            "data": {
                "username": "Identifiant",
                "password": "Mot de passe",
            },
            "data_description": {
                "username": (
                    "L'identifiant saisi sur la page de connexion "
                    "d'EcoleDirecte, pour un compte parent ou élève."
                ),
                "password": (
                    "Le mot de passe EcoleDirecte de ce compte. Il est "
                    "conservé, car EcoleDirecte ne délivre pas de jeton "
                    "d'appareil durable."
                ),
            },
        },
        "ecoledirecte_qcm": {
            "title": "Question de sécurité EcoleDirecte",
            "description": (
                "EcoleDirecte demande : {question}\n\n"
                "Choisissez la réponse mémorisée. Aucun compte n'est créé "
                "avant la réussite de la connexion."
            ),
            "data": {"choice": "Réponse"},
            "data_description": {
                "choice": (
                    "La réponse qu'EcoleDirecte attend pour ce compte, comme son "
                    "propre site la demande sur un nouvel appareil. Elle est "
                    "mémorisée : la question n'est pas reposée à chaque "
                    "connexion."
                ),
            },
        },
        "children": {
            "title": "Enfants à suivre",
            "description": (
                "Chaque enfant suivi multiplie le nombre d'appels quotidiens "
                "au serveur : ne sélectionnez que ceux qui vous intéressent."
            ),
            "data": {"children": "Enfants"},
            "data_description": {
                "children": (
                    "Chaque enfant coché a son propre appareil et ses propres "
                    "entités. Décochez un enfant inutile : chacun ajoute le "
                    "même nombre d'appels par jour."
                ),
            },
        },
        "reauth_confirm": {
            "title": "Se reconnecter à PRONOTE",
            "description": (
                "La connexion à {url} a été refusée. Saisissez un mot de passe "
                "corrigé, ou le code PIN à deux facteurs demandé par PRONOTE. "
                "Ce code est utilisé une fois et jamais conservé."
            ),
            "data": {
                "password": "Mot de passe",
                "account_pin": "Code PIN à deux facteurs",
            },
            "data_description": {
                "password": (
                    "Laissez vide pour garder le mot de passe conservé, par "
                    "exemple quand PRONOTE ne demande que le code PIN à deux "
                    "facteurs."
                ),
                "account_pin": (
                    "Le code PIN à deux facteurs défini sur le compte PRONOTE, "
                    "quand PRONOTE le demande. Utilisé pour cette connexion "
                    "uniquement et jamais conservé."
                ),
            },
        },
        "reauth_qr": {
            "title": "Se reconnecter avec un nouveau QR code",
            "description": (
                "La connexion à {url} a été refusée. Ce compte a été enrôlé "
                "par QR code : il n'a donc pas de mot de passe à corriger. "
                "PRONOTE délivre un nouveau jeton d'accès à chaque connexion, "
                "et lorsque celui qui est conservé est refusé, seul un nouveau "
                "QR code permet de revenir.\n\nDans l'application mobile "
                "PRONOTE, générez un nouveau QR code en choisissant un code à "
                "quatre chiffres, puis collez son contenu ci-dessous. Rien "
                "d'autre ne change : les enfants suivis, vos réglages et votre "
                "historique sont conservés."
            ),
            "data": {
                "qr_payload": "Contenu du QR code (JSON)",
                "qr_pin": "Code à quatre chiffres",
                "device_name": "Nom de l'appareil",
                "account_pin": "Code PIN à deux facteurs (si votre compte en a un)",
            },
            "data_description": {
                "qr_payload": (
                    "Le texte que renvoie un lecteur de QR code en scannant le "
                    "code affiché par l'application : un objet JSON avec login, "
                    "jeton et url. Collez-le en entier, pas une photo ni un lien "
                    "vers l'image."
                ),
                "device_name": (
                    "Le nom sous lequel PRONOTE affiche ce Home Assistant dans "
                    "la liste des appareils connectés au compte, et seulement "
                    "là. Il ne nomme pas l'appareil dans Home Assistant, qui "
                    "prend le nom de l'élève fourni par PRONOTE."
                ),
                "qr_pin": (
                    "Celui que vous avez choisi dans l'application en générant "
                    "le QR code. Pas le code PIN du compte ci-dessous : les "
                    "deux font quatre chiffres, et ce sont deux codes "
                    "différents."
                ),
                "account_pin": (
                    "Le code PIN à deux facteurs défini sur le compte PRONOTE "
                    "lui-même, s'il en a un. Quatre chiffres aussi, et pas "
                    "celui du dessus. Utilisé pour cette connexion uniquement "
                    "et jamais conservé. Il vous sera redemandé si PRONOTE "
                    "l'exige."
                ),
            },
        },
    },
    "error": {
        "invalid_auth": (
            "PRONOTE a refusé ces identifiants. Réessayer en boucle un mot de "
            "passe erroné est précisément ce qui fait bloquer une adresse : "
            "vérifiez-les avant de recommencer."
        ),
        "mfa_required": (
            "PRONOTE demande le code PIN à deux facteurs de ce compte. "
            "Saisissez-le ci-dessus."
        ),
        "bootstrap_failed": (
            "L'adresse a répondu, mais sans page de session PRONOTE. Vérifiez "
            "l'adresse, et que l'espace est bien ouvert en ce moment."
        ),
        "ent_required": (
            "Cet établissement n'autorise pas la connexion directe à PRONOTE : "
            "l'adresse mène à la page de connexion de son ENT. Rien n'a été "
            "envoyé et votre mot de passe n'a pas été refusé. Revenez au choix "
            "du mode de connexion et prenez « Connexion par l'ENT », avec "
            "l'identifiant et le mot de passe du portail — ou le QR code de "
            "l'application, si l'établissement l'a activé."
        ),
        "invalid_qr": (
            "Le QR code ou son code à quatre chiffres a été refusé. Un QR code "
            "n'est utilisable qu'une fois : générez-en un nouveau dans "
            "l'application."
        ),
        "qr_refused": (
            "PRONOTE n'a pas honoré ce QR code. Son code à quatre chiffres "
            "était juste — une erreur sur celui-là est signalée à part, et "
            "détectée avant tout envoi — donc ce qui a été refusé, c'est le QR "
            "code lui-même. Presque toujours, cela veut dire qu'il a déjà "
            "servi, ou qu'il a été généré il y a plus de quelques minutes : "
            "PRONOTE les délivre à usage unique et les périme vite. "
            "Générez-en un nouveau et utilisez-le immédiatement, le "
            "formulaire déjà ouvert. Si un QR code généré quelques secondes "
            "plus tôt est refusé lui aussi, c'est peut-être que le compte "
            "n'est pas autorisé à enrôler un nouvel appareil : renseignez-vous "
            "auprès de l'établissement plutôt que de réessayer, car réessayer "
            "en boucle une connexion refusée est précisément ce qui fait "
            "bloquer une adresse."
        ),
        "invalid_qr_payload": (
            "Cela ne ressemble pas au contenu d'un QR code PRONOTE. Il doit "
            "s'agir d'un objet JSON avec login, jeton et url — les deux "
            "premiers en hexadécimal. Collez-le en entier, exactement tel que "
            "l'application le donne."
        ),
        "unknown_ent": (
            "Aucun fournisseur ENT de ce nom n'est installé. Choisissez-en un "
            "dans la liste plutôt que de le saisir : un nom non reconnu ne "
            "retombe pas sur quelque chose de raisonnable, il envoie "
            "l'identifiant et le mot de passe de ce portail au serveur "
            "PRONOTE."
        ),
        "invalid_qcm": "Choisissez l'une des réponses proposées.",
        "cannot_connect": "Impossible de joindre le serveur.",
        "ed_invalid_auth": (
            "EcoleDirecte a refusé ces identifiants. Vérifiez-les avant de recommencer."
        ),
        "rate_limited": (
            "Trop de tentatives de connexion dans la dernière heure : celle-ci "
            "n'a pas été envoyée. Attendez un peu avant de réessayer, car "
            "réessayer une connexion en boucle est précisément ce qui fait "
            "bloquer une adresse."
        ),
        "unknown": "Erreur inattendue. Consultez le journal de Home Assistant.",
    },
    "abort": {
        "already_configured": "Ce compte est déjà configuré.",
        "reauth_successful": "Reconnexion réussie.",
        "reconfigure_successful": (
            "Connexion mise à jour. L'entrée se recharge avec elle ; ses "
            "appareils, entités, historique et options sont inchangés."
        ),
        "reconfigure_wrong_account": (
            "Ces informations ouvrent un autre compte que celui que suit cette "
            "entrée : rien n'a été modifié. Les appliquer aurait fait pointer "
            "cette entrée vers l'enfant de quelqu'un d'autre en conservant le "
            "nom et l'historique de celui-ci. Utilisez les informations du "
            "compte de cette entrée, ou ajoutez l'autre compte séparément."
        ),
        "wrong_account": (
            "Ce QR code appartient à un autre compte PRONOTE : il n'a pas été "
            "appliqué. Reconnecter cette entrée avec lui l'aurait fait pointer "
            "vers l'enfant de quelqu'un d'autre en conservant le nom et "
            "l'historique de celui-ci. Générez un QR code depuis le compte "
            "que suit cette entrée, ou ajoutez l'autre compte séparément."
        ),
    },
}

# `(option key, english label, french label, english help, french help)`.
# Every tunable of annexe B §7 appears here, and every one carries a help line:
# a range without an explanation gets set at random.
GENERAL_OPTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "master_tick",
        "Heartbeat interval",
        "Intervalle du battement",
        (
            "How often the integration checks which tiers are due. Not how often "
            "it calls PRONOTE."
        ),
        (
            "Fréquence à laquelle l'intégration regarde quels paliers sont dus. "
            "Ce n'est pas la fréquence des appels à PRONOTE."
        ),
    ),
    (
        "homework_horizon",
        "Homework horizon",
        "Horizon des devoirs",
        (
            "How many days of homework to display. This does not change the "
            "number of requests: PRONOTE returns a week range either way."
        ),
        (
            "Nombre de jours de devoirs affichés. Cela ne change pas le nombre "
            "d'appels : PRONOTE renvoie une plage de semaines dans tous les cas."
        ),
    ),
    (
        "wake_margin",
        "Wake-up margin",
        "Marge de réveil",
        (
            "Minutes subtracted from the first lesson of the day to produce the "
            "wake-up sensor."
        ),
        (
            "Minutes retirées au premier cours de la journée pour calculer le "
            "capteur de réveil."
        ),
    ),
    (
        "stale_after",
        "Mark data stale after",
        "Marquer périmé après",
        (
            "Multiples of a tier's interval after which its entities are marked "
            "stale. They keep their last value rather than going unavailable, "
            "because an entity that flickers unavailable fires automations "
            "spuriously."
        ),
        (
            "Multiples de l'intervalle d'un palier au-delà desquels ses entités "
            "sont marquées périmées. Elles conservent leur dernière valeur au "
            "lieu de devenir indisponibles : une entité qui clignote en "
            "indisponible déclenche des automatisations à tort."
        ),
    ),
    (
        "establishment_timezone",
        "Establishment timezone",
        "Fuseau de l'établissement",
        (
            "PRONOTE returns local times with no timezone. This is the timezone "
            "they are interpreted in. Leave it empty to follow Home Assistant's "
            "own timezone, which is what you want unless the establishment is in "
            "a different one from you. Written as Region/City, for example "
            "Europe/Paris."
        ),
        (
            "PRONOTE renvoie des heures locales sans fuseau. C'est le fuseau dans "
            "lequel elles sont interprétées. Laissez la case vide pour suivre le "
            "fuseau de Home Assistant, ce qui est le bon choix sauf si "
            "l'établissement est dans un autre fuseau que vous. S'écrit "
            "Région/Ville, par exemple Europe/Paris."
        ),
    ),
    (
        "session_strategy",
        "Session strategy",
        "Stratégie de session",
        (
            "Whether to keep one session alive between collections (lazy) or open "
            "a new one for each batch. Lazy starts cautious and switches by "
            "itself if the server turns out to expire sessions quickly, so it is "
            "never worse than the alternative."
        ),
        (
            "Conserver une session entre les collectes (lazy) ou en ouvrir une "
            "par lot. Le mode lazy démarre prudemment et basculera de lui-même si "
            "le serveur expire vite les sessions : il n'est jamais moins bon que "
            "l'autre."
        ),
    ),
    (
        "write_operations_enabled",
        "Allow writing to PRONOTE",
        "Autoriser l'écriture dans PRONOTE",
        (
            "On for a new installation. When on, ticking a homework item, marking "
            "news read and sending messages are possible, and the establishment "
            "sees those actions."
        ),
        (
            "Activé pour une nouvelle installation. Activé, cocher un devoir, "
            "marquer une actualité comme lue et envoyer des messages sont "
            "possibles, et l'établissement voit ces actions."
        ),
    ),
]

RATE_LIMIT_OPTIONS: list[tuple[str, str, str, str, str]] = [
    (
        "min_request_interval",
        "Minimum interval between requests",
        "Intervalle minimum entre appels",
        "Spacing applied to every request, so a batch never arrives as a burst.",
        "Espacement appliqué à chaque appel, pour qu'un lot n'arrive jamais en rafale.",
    ),
    (
        "max_requests_per_hour",
        "Requests per hour",
        "Appels par heure",
        "The sustained rate the token bucket refills at.",
        "Le débit soutenu auquel le seau à jetons se remplit.",
    ),
    (
        "burst_size",
        "Burst size",
        "Taille de la rafale",
        "How many requests may go out back to back before the hourly rate applies.",
        (
            "Nombre d'appels pouvant partir d'affilée avant que le débit horaire "
            "s'applique."
        ),
    ),
    (
        "max_requests_per_day",
        "Requests per day",
        "Appels par jour",
        (
            "Hard ceiling. Low-priority tiers stop being served well before it is "
            "reached, so the cap protects the account rather than surprising you."
        ),
        (
            "Plafond absolu. Les paliers de faible priorité cessent d'être servis "
            "bien avant qu'il soit atteint : le plafond protège le compte au lieu "
            "de vous surprendre."
        ),
    ),
    (
        "max_wait",
        "Maximum wait before postponing",
        "Attente maximale avant report",
        (
            "If a tier would have to wait longer than this for its budget, it is "
            "postponed instead. Its entities keep their values."
        ),
        (
            "Si un palier devait attendre plus longtemps que cela pour son budget, "
            "il est reporté. Ses entités conservent leurs valeurs."
        ),
    ),
    (
        "max_logins_per_day",
        "Logins per day",
        "Connexions par jour",
        "A login costs about five requests, so this is counted separately.",
        "Une connexion coûte environ cinq appels : elle est donc comptée à part.",
    ),
    (
        "max_failed_logins_per_hour",
        "Failed logins before pausing",
        "Échecs de connexion avant pause",
        (
            "The single most important setting here. PRONOTE sanctions repeated "
            "failed logins by blocking the address, and that sanction is "
            "undocumented and costly. Keep this low."
        ),
        (
            "Le réglage le plus important de cette page. PRONOTE sanctionne les "
            "échecs de connexion répétés en bloquant l'adresse, et cette sanction "
            "n'est pas documentée et coûte cher. Gardez une valeur basse."
        ),
    ),
    (
        "credentials_hold",
        "Pause after too many failures",
        "Pause après trop d'échecs",
        (
            "How long to stop trying once the failed-login limit is reached. A "
            "manual reconnection clears it immediately."
        ),
        (
            "Durée pendant laquelle on arrête d'essayer une fois la limite "
            "atteinte. Une reconnexion manuelle la lève immédiatement."
        ),
    ),
    (
        "bootstrap_hold",
        "Pause when the session page is unreadable",
        "Pause si la page de session est illisible",
        (
            "Applied when the address answers without a PRONOTE session page. The "
            "cause is not knowable from the response, so no cause is claimed."
        ),
        (
            "Appliquée quand l'adresse répond sans page de session PRONOTE. La "
            "cause n'est pas déductible de la réponse : aucune cause n'est donc "
            "affirmée."
        ),
    ),
    (
        "backoff_base",
        "Backoff base",
        "Base de la temporisation",
        (
            "First wait after a server error. It doubles at each consecutive "
            "failure, with jitter."
        ),
        (
            "Première attente après une erreur serveur. Elle double à chaque échec "
            "consécutif, avec une part d'aléatoire."
        ),
    ),
    (
        "backoff_max",
        "Backoff maximum",
        "Temporisation maximale",
        "Ceiling on that doubling.",
        "Plafond de ce doublement.",
    ),
    (
        "connect_timeout",
        "Connection timeout",
        "Délai de connexion",
        (
            "The upstream library sets no timeout at all, so this is applied by "
            "this integration."
        ),
        (
            "La bibliothèque amont ne fixe aucun délai : celui-ci est appliqué par "
            "cette intégration."
        ),
    ),
    (
        "read_timeout",
        "Read timeout",
        "Délai de lecture",
        "How long to wait for a response before abandoning a request.",
        "Durée d'attente d'une réponse avant d'abandonner un appel.",
    ),
    (
        "quiet_hours_enabled",
        "Quiet hours",
        "Heures calmes",
        (
            "Stop collecting overnight. Roughly a third of the daily budget, spent "
            "on data nobody reads while asleep."
        ),
        (
            "Arrêter de collecter la nuit. C'est environ un tiers du budget "
            "quotidien, dépensé sur des données que personne ne lit en dormant."
        ),
    ),
    (
        "quiet_start",
        "Quiet hours start",
        "Début des heures calmes",
        (
            "When scheduled collection pauses each evening. A button press, a "
            "service call and the refill after a restart still go through."
        ),
        (
            "Heure à laquelle la collecte programmée s'arrête le soir. Un appui "
            "sur un bouton, un appel de service et le remplissage après un "
            "redémarrage passent quand même."
        ),
    ),
    (
        "quiet_end",
        "Quiet hours end",
        "Fin des heures calmes",
        "When scheduled collection resumes in the morning.",
        "Heure à laquelle la collecte programmée reprend le matin.",
    ),
]

OPTIONS_INIT_DESCRIPTION_EN = (
    "Currently about **{estimate} requests a day** for {students} child(ren). "
    "Measured session lifetime: {measured} min."
)
OPTIONS_INIT_DESCRIPTION_FR = (
    "Actuellement environ **{estimate} appels par jour** pour {students} "
    "enfant(s). Durée de vie mesurée de la session : {measured} min."
)

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

SERVICES: list[dict[str, Any]] = [
    {
        "key": "refresh",
        "name_en": "Refresh",
        "name_fr": "Rafraîchir",
        "description_en": (
            "Asks the scheduler for a priority pass. Does not bypass the rate "
            "limiter, so pressing it repeatedly costs one batch, not several."
        ),
        "description_fr": (
            "Demande un passage prioritaire à l'ordonnanceur. Ne contourne pas "
            "le limiteur : l'appeler plusieurs fois coûte un lot, pas "
            "plusieurs."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            (
                "tiers",
                "Tiers",
                "Paliers",
                "Which categories to refresh. All of them if left empty.",
                "Catégories à rafraîchir. Toutes si le champ est vide.",
            ),
        ],
    },
    {
        "key": "get_ical_url",
        "name_en": "Get the iCal URL",
        "name_fr": "Obtenir l'URL iCal",
        "description_en": (
            "Returns the timetable subscription URL in the response. It is "
            "never stored in a state or an attribute: anyone holding this URL "
            "can read the child's timetable without a password."
        ),
        "description_fr": (
            "Renvoie l'URL d'abonnement à l'emploi du temps dans la réponse. "
            "Elle n'est jamais stockée dans un état ni un attribut : quiconque "
            "détient cette URL lit l'emploi du temps de l'enfant sans mot de "
            "passe."
        ),
        "fields": [("device_id", "Child", "Enfant", "", "")],
    },
    {
        "key": "get_identity",
        "name_en": "Get the identity",
        "name_fr": "Obtenir l'identité",
        "description_en": (
            "Returns the personal details and legal guardians in the response. "
            "Never stored in a state."
        ),
        "description_fr": (
            "Renvoie l'état civil et les responsables légaux dans la réponse. "
            "Jamais stocké dans un état."
        ),
        "fields": [("device_id", "Child", "Enfant", "", "")],
    },
    {
        "key": "mark_homework_done",
        "name_en": "Tick a homework item",
        "name_fr": "Cocher un devoir",
        "description_en": (
            "Marks one homework item done or not done. The establishment sees "
            "this. Requires writing to be enabled in the options."
        ),
        "description_fr": (
            "Marque un devoir comme fait ou non fait. L'établissement le voit. "
            "Nécessite l'activation de l'écriture dans les options."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            (
                "homework_id",
                "Homework identifier",
                "Identifiant du devoir",
                "Taken from the id field of the homework sensor's items.",
                "Repris du champ id des items du capteur de devoirs.",
            ),
            ("done", "Done", "Fait", "", ""),
        ],
    },
    {
        "key": "mark_information_read",
        "name_en": "Mark a news item read",
        "name_fr": "Marquer une actualité comme lue",
        "description_en": (
            "Marks one news item as read. Requires writing to be enabled."
        ),
        "description_fr": (
            "Marque une actualité comme lue. Nécessite l'activation de l'écriture."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            (
                "information_id",
                "News identifier",
                "Identifiant de l'actualité",
                "Taken from the id field of the news sensor's items.",
                "Repris du champ id des items du capteur d'actualités.",
            ),
        ],
    },
    {
        "key": "send_message",
        "name_en": "Send a message",
        "name_fr": "Envoyer un message",
        "description_en": (
            "Replies to an existing discussion, or starts a new one with named "
            "recipients. Give either a discussion identifier or a list of "
            "recipients, not both. Requires writing to be enabled."
        ),
        "description_fr": (
            "Répond à une discussion existante, ou en ouvre une nouvelle avec "
            "des destinataires nommés. Fournissez soit un identifiant de "
            "discussion, soit une liste de destinataires, pas les deux. "
            "Nécessite l'activation de l'écriture."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            ("message", "Message", "Message", "", ""),
            (
                "discussion_id",
                "Discussion identifier",
                "Identifiant de discussion",
                "To reply to an existing thread.",
                "Pour répondre à un fil existant.",
            ),
            (
                "subject",
                "Subject",
                "Objet",
                "Required when starting a new discussion.",
                "Obligatoire pour ouvrir une nouvelle discussion.",
            ),
            (
                "recipients",
                "Recipients",
                "Destinataires",
                (
                    "Names as PRONOTE publishes them. An unmatched name is "
                    "refused rather than silently dropped."
                ),
                (
                    "Noms tels que PRONOTE les publie. Un nom non trouvé est "
                    "refusé plutôt qu'ignoré silencieusement."
                ),
            ),
        ],
    },
    {
        "key": "generate_timetable_pdf",
        "name_en": "Generate a timetable PDF",
        "name_fr": "Générer un PDF de l'emploi du temps",
        "description_en": (
            "Asks PRONOTE to render a PDF and returns its URL in the response."
        ),
        "description_fr": (
            "Demande à PRONOTE de produire un PDF et renvoie son URL dans la réponse."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            ("day", "Day", "Jour", "Defaults to today.", "Aujourd'hui par défaut."),
            ("orientation", "Orientation", "Orientation", "", ""),
        ],
    },
    {
        "key": "get_rate_limit_status",
        "name_en": "Get the rate limiter status",
        "name_fr": "Obtenir l'état du limiteur",
        "description_en": (
            "Returns the full budget and scheduler state in the response. "
            "Costs no request: it reads counters already in memory."
        ),
        "description_fr": (
            "Renvoie l'état complet du budget et de l'ordonnanceur dans la "
            "réponse. Ne coûte aucun appel : il lit des compteurs déjà en "
            "mémoire."
        ),
        "fields": [("device_id", "Account or child", "Compte ou enfant", "", "")],
    },
    {
        "key": "get_attachment_url",
        "name_en": "Get an attachment's address",
        "name_fr": "Obtenir l'adresse d'une pièce jointe",
        "description_en": (
            "Returns, in the response, a short-lived address that opens one "
            "homework file. Costs no request: the document is fetched only "
            "when the address is opened."
        ),
        "description_fr": (
            "Renvoie dans la réponse une adresse de courte durée qui ouvre un "
            "fichier de devoir. Ne coûte aucun appel : le document n'est "
            "téléchargé qu'à l'ouverture de l'adresse."
        ),
        "fields": [
            ("device_id", "Child", "Enfant", "", ""),
            (
                "key",
                "Attachment key",
                "Clé de la pièce jointe",
                "The key of an entry in the attachment_refs attribute.",
                "La clé d'une entrée de l'attribut attachment_refs.",
            ),
        ],
    },
]

# ---------------------------------------------------------------------------
# Exceptions, issues, device automation
# ---------------------------------------------------------------------------

EXCEPTIONS: list[tuple[str, str, str]] = [
    (
        "writes_disabled",
        (
            "Writing to PRONOTE is disabled. Turn on “Allow writing to "
            "PRONOTE” in the integration options first."
        ),
        (
            "L'écriture dans PRONOTE est désactivée. Activez d'abord "
            "« Autoriser l'écriture dans PRONOTE » dans les options de "
            "l'intégration."
        ),
    ),
    (
        "service_deferred",
        (
            "Postponed by the rate limiter ({reason}). Try again in about "
            "{seconds} seconds."
        ),
        (
            "Reporté par le limiteur ({reason}). Réessayez dans environ {seconds} "
            "secondes."
        ),
    ),
    (
        "unknown_device",
        "No such device: {device_id}.",
        "Appareil inconnu : {device_id}.",
    ),
    (
        "device_not_pronote",
        "Device {device_id} does not belong to a PRONOTE account.",
        "L'appareil {device_id} n'appartient pas à un compte PRONOTE.",
    ),
    (
        "student_required",
        (
            "This account follows several children ({children}). Target the "
            "child's device rather than the account."
        ),
        (
            "Ce compte suit plusieurs enfants ({children}). Ciblez l'appareil de "
            "l'enfant plutôt que celui du compte."
        ),
    ),
    (
        "attachment_not_collected",
        "Homework has not been collected for this child yet. Try again in a moment.",
        (
            "Les devoirs de cet enfant n'ont pas encore été collectés. Réessayez "
            "dans un instant."
        ),
    ),
    (
        "attachment_unknown",
        (
            "No homework file with this key is known for this child. Refresh the "
            "dashboard: the homework may have changed."
        ),
        (
            "Aucun fichier de devoir ne correspond à cette clé pour cet enfant. "
            "Rafraîchissez le tableau de bord : le devoir a peut-être changé."
        ),
    ),
    (
        "subject_required",
        "A subject is required to start a new discussion.",
        "Un objet est obligatoire pour ouvrir une nouvelle discussion.",
    ),
    (
        "discussion_not_found",
        "No discussion with identifier {discussion_id} is visible to this account.",
        (
            "Aucune discussion avec l'identifiant {discussion_id} n'est visible "
            "depuis ce compte."
        ),
    ),
    (
        "discussion_closed",
        "Discussion {discussion_id} is closed and cannot be replied to.",
        "La discussion {discussion_id} est close : impossible d'y répondre.",
    ),
    (
        "recipient_not_found",
        "Unknown recipients: {missing}. Reachable recipients are: {available}.",
        (
            "Destinataires inconnus : {missing}. Les destinataires joignables "
            "sont : {available}."
        ),
    ),
    (
        "todo_item_unknown",
        "That to-do item has no PRONOTE identifier, so it cannot be updated.",
        (
            "Cet élément de liste n'a pas d'identifiant PRONOTE : il ne peut pas "
            "être mis à jour."
        ),
    ),
    (
        "homework_tick_parent_account",
        (
            "PRONOTE does not record a homework tick sent from a parent "
            "account: only the student's own account can tick homework. "
            "Nothing was sent."
        ),
        (
            "PRONOTE n'enregistre pas la coche d'un devoir envoyée depuis un "
            "compte parent : seul le compte de l'élève peut cocher un devoir. "
            "Rien n'a été envoyé."
        ),
    ),
    (
        "write_not_applied",
        (
            "PRONOTE accepted the request but did not record it: the item "
            "read back unchanged. Try again later, or make the change in "
            "PRONOTE itself."
        ),
        (
            "PRONOTE a accepté la demande sans l'enregistrer : l'élément a été "
            "relu inchangé. Réessayez plus tard, ou faites la modification "
            "dans PRONOTE."
        ),
    ),
    (
        "item_not_found",
        (
            "That item is no longer on PRONOTE as it was last read: it was "
            "withdrawn or changed since. Nothing was sent. Refresh, then try "
            "again."
        ),
        (
            "Cet élément n'est plus sur PRONOTE tel qu'il a été lu : il a été "
            "retiré ou modifié depuis. Rien n'a été envoyé. Actualisez, puis "
            "réessayez."
        ),
    ),
    # The failures `service_errors.async_run_gesture` translates for every
    # service and for the to-do checkbox. Fixed sentences on purpose: the
    # exceptions behind them can quote a URL carrying session parameters, so
    # their text never reaches the user -- the only placeholders are the
    # limiter's own reason codes.
    (
        "service_login_refused",
        (
            "PRONOTE was not contacted: the rate limiter is holding back "
            "logins ({reason}). Nothing was sent. Try again later; retrying "
            "sooner makes it last longer."
        ),
        (
            "PRONOTE n'a pas été contacté : le limiteur retient les connexions "
            "({reason}). Rien n'a été envoyé. Réessayez plus tard ; réessayer "
            "plus tôt prolonge l'attente."
        ),
    ),
    (
        "service_reauth_required",
        (
            "PRONOTE refused the account's credentials, or asked for the "
            "two-factor PIN. Re-authenticate the integration from its page in "
            "Settings, then try again."
        ),
        (
            "PRONOTE a refusé les identifiants du compte, ou demande le code "
            "PIN de double authentification. Réauthentifiez l'intégration "
            "depuis sa page dans Paramètres, puis réessayez."
        ),
    ),
    (
        "service_unreachable",
        (
            "PRONOTE could not be reached, or did not answer in time. The "
            "request may not have been received. Try again in a few minutes."
        ),
        (
            "PRONOTE est injoignable, ou n'a pas répondu à temps. La demande "
            "n'a peut-être pas été reçue. Réessayez dans quelques minutes."
        ),
    ),
    (
        "service_refused",
        (
            "PRONOTE refused the request. The integration is backing off; try "
            "again later."
        ),
        (
            "PRONOTE a refusé la demande. L'intégration espace ses appels ; "
            "réessayez plus tard."
        ),
    ),
    (
        "service_unreadable",
        (
            "PRONOTE answered, but the answer could not be read. This usually "
            "means the establishment's PRONOTE changed; please report it with "
            "the integration's diagnostics."
        ),
        (
            "PRONOTE a répondu, mais sa réponse est illisible. Cela signifie "
            "généralement que le PRONOTE de l'établissement a changé ; "
            "signalez-le en joignant les diagnostics de l'intégration."
        ),
    ),
    (
        "service_internal_error",
        (
            "The request could not be completed because of a fault in this "
            "integration. The Home Assistant log says more; please report it."
        ),
        (
            "La demande n'a pas pu aboutir à cause d'un défaut de cette "
            "intégration. Le journal de Home Assistant en dit plus ; "
            "signalez-le."
        ),
    ),
    (
        "service_not_supported",
        "The {source} source does not support the {service} action.",
        "La source {source} ne prend pas en charge l'action {service}.",
    ),
    (
        "homework_tick_not_supported",
        "The {source} source does not support ticking homework.",
        "La source {source} ne permet pas de cocher un devoir.",
    ),
    (
        "todo_item_owned_by_pronote",
        (
            "Only the tick can be changed. The subject, the wording and the due "
            "date of a homework item belong to the teacher, in PRONOTE."
        ),
        (
            "Seule la coche peut changer. La matière, l'énoncé et l'échéance "
            "d'un devoir appartiennent au professeur, dans PRONOTE."
        ),
    ),
    # Set-up failures (`__init__.py`). Home Assistant shows these on the
    # integration card, and they replace `str(error)`, which could quote a
    # server page or an address carrying session parameters. The only
    # placeholder is `failures.describe_failure`: class names and a fixed
    # category, useful in a bug report, and never anything the server sent.
    (
        "setup_auth_failed",
        (
            "The school platform refused the saved login, or asks for a "
            "confirmation [{error_type}]. Re-authenticate the integration."
        ),
        (
            "La plateforme scolaire a refusé la connexion enregistrée, ou "
            "demande une confirmation [{error_type}]. Réauthentifiez "
            "l'intégration."
        ),
    ),
    (
        "setup_unreachable",
        (
            "The school platform's login page could not be read "
            "[{error_type}]. The integration will try again."
        ),
        (
            "La page de connexion de la plateforme scolaire est illisible "
            "[{error_type}]. L'intégration réessaiera."
        ),
    ),
    (
        "setup_login_postponed",
        (
            "The login was refused or postponed [{error_type}]. The "
            "integration will try again."
        ),
        (
            "La connexion a été refusée ou différée [{error_type}]. "
            "L'intégration réessaiera."
        ),
    ),
    (
        "setup_unreadable",
        (
            "The school platform answered with data this integration cannot "
            "read [{error_type}]. The integration will try again."
        ),
        (
            "La plateforme scolaire a répondu par des données que cette "
            "intégration ne sait pas lire [{error_type}]. L'intégration "
            "réessaiera."
        ),
    ),
    (
        "setup_server_refused",
        (
            "The school platform refused the request [{error_type}]. The "
            "integration will try again."
        ),
        (
            "La plateforme scolaire a refusé la requête [{error_type}]. "
            "L'intégration réessaiera."
        ),
    ),
    (
        "setup_network_error",
        (
            "The school platform could not be reached [{error_type}]. The "
            "integration will try again."
        ),
        (
            "La plateforme scolaire est injoignable [{error_type}]. "
            "L'intégration réessaiera."
        ),
    ),
    # Device automations (`device_condition.py`, `device_trigger.py`).
    (
        "condition_entity_missing",
        (
            "There is no {key} entity on device {device_id}: this "
            "establishment does not publish the data this condition reads."
        ),
        (
            "L'appareil {device_id} n'a pas d'entité {key} : cet "
            "établissement ne publie pas la donnée que lit cette condition."
        ),
    ),
    (
        "trigger_device_not_followed",
        (
            "Device {device_id} is not a child this account currently "
            "follows, so this trigger can never fire. Select the device again "
            "in the automation, or delete the stale one."
        ),
        (
            "L'appareil {device_id} n'est pas un enfant que ce compte suit "
            "actuellement : ce déclencheur ne peut donc jamais se produire. "
            "Sélectionnez à nouveau l'appareil dans l'automatisation, ou "
            "supprimez l'ancien."
        ),
    ),
]

ISSUES: list[tuple[str, str, str, str, str]] = [
    (
        # Opened by `async_open_unreadable_issue`, on a live path
        # (`__init__.py`), and it had no entry here -- so Home Assistant
        # rendered the raw key as the title of a repair the user cannot
        # otherwise diagnose. A separate issue from `bootstrap_failed` because
        # the remedy differs: there is nothing for the user to correct.
        "account_unreadable",
        "PRONOTE answered something this integration cannot read",
        "PRONOTE a répondu quelque chose d'illisible",
        (
            "The address {url} and the credentials are both fine: PRONOTE "
            "answered, and the answer is outside what the pinned pronotepy "
            "version can decode. This usually means the establishment's PRONOTE "
            "changed something. There is nothing to correct on your side -- "
            "please report it, ideally with the integration's diagnostics "
            "attached."
        ),
        (
            "L'adresse {url} et les identifiants sont corrects : PRONOTE a "
            "répondu, mais sa réponse sort de ce que la version épinglée de "
            "pronotepy sait décoder. Cela signifie généralement que le PRONOTE de "
            "l'établissement a changé. Il n'y a rien à corriger de votre côté : "
            "merci de le signaler, si possible avec le diagnostic de "
            "l'intégration."
        ),
    ),
    (
        "invalid_credentials",
        "PRONOTE credentials refused",
        "Identifiants PRONOTE refusés",
        (
            "PRONOTE refused the credentials {attempts} times, so attempts have "
            "been paused until {until}. Repeatedly retrying a wrong password is "
            "what gets an address blocked. Reconfigure the integration to enter "
            "corrected credentials."
        ),
        (
            "PRONOTE a refusé les identifiants {attempts} fois : les tentatives "
            "sont suspendues jusqu'à {until}. Réessayer en boucle un mot de passe "
            "erroné est précisément ce qui fait bloquer une adresse. "
            "Reconfigurez l'intégration pour saisir des identifiants corrigés."
        ),
    ),
    (
        "bootstrap_failed",
        "Cannot read the PRONOTE session page",
        "Page de session PRONOTE illisible",
        (
            "{url} answered, but the response contained no PRONOTE session block. "
            "This can mean the address is wrong, the space is closed for "
            "maintenance, the establishment changed its URL, or the server is "
            "returning an error page. The response does not say which, so no "
            "cause is claimed here. Retrying automatically until {until}."
        ),
        (
            "{url} a répondu, mais la réponse ne contenait aucun bloc de session "
            "PRONOTE. Cela peut signifier que l'adresse est erronée, que l'espace "
            "est fermé pour maintenance, que l'établissement a changé son URL, ou "
            "que le serveur renvoie une page d'erreur. La réponse ne permet pas de "
            "trancher : aucune cause n'est donc affirmée ici. Nouvelle tentative "
            "automatique jusqu'à {until}."
        ),
    ),
    (
        "daily_cap_near",
        "Approaching the daily request limit",
        "Plafond d'appels quotidien bientôt atteint",
        (
            "{calls} of {cap} requests used today. Low-priority categories are "
            "already being postponed. Lengthening the intervals of the categories "
            "you care least about in the options is the usual fix."
        ),
        (
            "{calls} appels sur {cap} utilisés aujourd'hui. Les catégories de "
            "faible priorité sont déjà reportées. Allonger les intervalles des "
            "catégories qui vous importent le moins dans les options est le "
            "réglage habituel."
        ),
    ),
    (
        "mfa_required",
        "PRONOTE is asking for the two-factor PIN",
        "PRONOTE demande le code PIN à deux facteurs",
        (
            "This integration deliberately never stores the two-factor PIN, so it "
            "has to be entered again. Reconfigure the integration to supply it."
        ),
        (
            "Cette intégration ne conserve délibérément jamais le code PIN à deux "
            "facteurs : il doit donc être saisi à nouveau. Reconfigurez "
            "l'intégration pour le fournir."
        ),
    ),
]

TRIGGER_TYPES: list[tuple[str, str, str]] = [
    ("grade_added", "A grade was added", "Une note est arrivée"),
    ("homework_added", "Homework was added", "Un devoir a été ajouté"),
    ("lesson_canceled", "A lesson was cancelled", "Un cours a été annulé"),
    (
        "lesson_restored",
        "A cancelled lesson is back on",
        "Un cours annulé est rétabli",
    ),
    ("lesson_moved", "A lesson was moved", "Un cours a été déplacé"),
    ("room_changed", "A room changed", "Une salle a changé"),
    ("teacher_changed", "A teacher changed", "Un professeur a changé"),
    (
        "lesson_status_changed",
        "A lesson's status changed",
        "Le statut d'un cours a changé",
    ),
    ("information_added", "A news item was posted", "Une actualité a été publiée"),
    ("absence_added", "An absence was recorded", "Une absence a été enregistrée"),
    ("delay_added", "A late arrival was recorded", "Un retard a été enregistré"),
    ("punishment_added", "A punishment was given", "Une punition a été donnée"),
    ("message_received", "A message was received", "Un message a été reçu"),
    (
        "evaluation_added",
        "A skill assessment was added",
        "Une évaluation a été ajoutée",
    ),
]

CONDITION_TYPES: list[tuple[str, str, str]] = [
    ("is_school_day", "It is a school day", "C'est un jour de classe"),
    ("is_not_school_day", "It is not a school day", "Ce n'est pas un jour de classe"),
    ("is_in_class", "The child is in class", "L'enfant est en cours"),
    ("is_not_in_class", "The child is not in class", "L'enfant n'est pas en cours"),
    ("is_test_today", "A test is scheduled today", "Un contrôle est prévu aujourd'hui"),
    ("is_homework_overdue", "Homework is overdue", "Des devoirs sont en retard"),
    (
        "is_absence_in_progress",
        "An absence is in progress",
        "Une absence est en cours",
    ),
    (
        "is_punishment_upcoming",
        "A detention is scheduled",
        "Une punition est programmée",
    ),
    ("is_holidays", "It is the holidays", "C'est les vacances"),
    ("is_not_holidays", "It is not the holidays", "Ce ne sont pas les vacances"),
]

ACTION_TYPES: list[tuple[str, str, str]] = [
    ("refresh", "Refresh everything", "Tout rafraîchir"),
    ("refresh_marks", "Refresh the grades", "Rafraîchir les notes"),
    ("mark_homework_done", "Tick a homework item", "Cocher un devoir"),
    (
        "mark_information_read",
        "Mark a news item read",
        "Marquer une actualité comme lue",
    ),
]


#: Which event types each ``event`` entity may report -- the same mapping
#: ``event.py`` declares in its entity descriptions. Duplicated here because
#: this script deliberately imports nothing from the integration (it has to run
#: without Home Assistant installed), and kept honest by
#: ``test_every_event_type_an_entity_can_report_is_translated``, which compares
#: it against the entities that were actually created.
EVENT_TYPES_BY_ENTITY: dict[str, tuple[str, ...]] = {
    "new_grade": ("grade_added",),
    "new_homework": ("homework_added",),
    "lesson_changed": (
        "lesson_canceled",
        "lesson_restored",
        "lesson_moved",
        "room_changed",
        "teacher_changed",
        "lesson_status_changed",
    ),
    "new_information": ("information_added",),
    "new_absence": ("absence_added",),
    "new_delay": ("delay_added",),
    "new_punishment": ("punishment_added",),
    "new_message": ("message_received",),
    "new_evaluation": ("evaluation_added",),
}

# ---------------------------------------------------------------------------
# Icons (`icons.json`), per platform and translation key
#
# Keyed exactly like the entity tables above, and `_icons` refuses to build a
# file in which one of them has no icon: an entity added to a table without an
# icon fails the generator, and the test comparing `icons.json` to this output,
# rather than shipping with whatever its device class happens to draw.
#
# A value is either a bare icon (the `default`) or a mapping. For a binary
# sensor the default is the `on` picture and `state.off` the other one; the
# limiter's ENUM sensor has one icon per state. hassfest rejects a state icon
# identical to its default, so the tables never repeat it.
# ---------------------------------------------------------------------------

ENTITY_ICONS: dict[str, dict[str, str | dict[str, Any]]] = {
    "sensor": {
        "next_lesson": "mdi:book-clock",
        "end_of_lessons": "mdi:bell-ring-outline",
        "morning_end": "mdi:clock-end",
        "next_cancellation": "mdi:calendar-remove",
        "next_wake_up": "mdi:alarm",
        "next_test": "mdi:clipboard-text-clock",
        "lessons_today": "mdi:book-open-page-variant",
        "homework_todo": "mdi:notebook-edit",
        "homework_tomorrow": "mdi:bag-personal",
        "latest_grade": "mdi:star-box",
        "overall_average": "mdi:chart-line",
        "class_average": "mdi:account-group",
        "next_punishment": "mdi:gavel",
        "unjustified_absences": "mdi:account-question",
        "unread_information": "mdi:newspaper-variant",
        "unread_messages": "mdi:email-alert",
        "current_period": "mdi:calendar-range",
        "timetable_tomorrow": "mdi:calendar-arrow-right",
        "timetable_week": "mdi:calendar-week",
        "homework": "mdi:notebook",
        "grades": "mdi:format-list-numbered",
        "averages": "mdi:chart-bar",
        "report_card": "mdi:file-certificate",
        "absences": "mdi:account-off",
        "delays": "mdi:clock-alert",
        "punishments": "mdi:alert-octagon",
        "evaluations": "mdi:check-decagram",
        "information": "mdi:newspaper",
        "discussions": "mdi:forum",
        "menu_today": "mdi:silverware-fork-knife",
        "menu_tomorrow": "mdi:food",
        "teaching_staff": "mdi:human-male-board",
        "class_name": "mdi:google-classroom",
        "periods": "mdi:calendar-multiple",
        # A closed period reads like its current-period sibling.
        "grades_period": "mdi:format-list-numbered",
        "averages_period": "mdi:chart-bar",
        "overall_average_period": "mdi:chart-line",
        "report_card_period": "mdi:file-certificate",
        "absences_period": "mdi:account-off",
        "delays_period": "mdi:clock-alert",
        "punishments_period": "mdi:alert-octagon",
        "evaluations_period": "mdi:check-decagram",
        "calls_today": "mdi:counter",
        "remaining_budget": "mdi:gauge",
        "last_collection": "mdi:cloud-download",
        "next_collection": "mdi:timer-sand",
        "session_age": "mdi:timer-outline",
        "session_lifetime": "mdi:history",
        "logins_today": "mdi:login",
        "limiter_state": {
            "default": "mdi:speedometer",
            "state": {
                "nominal": "mdi:check-circle",
                "throttled": "mdi:speedometer-slow",
                "backoff": "mdi:pause-circle",
                "quiet_hours": "mdi:weather-night",
                "credentials_hold": "mdi:account-lock",
                "bootstrap_failed": "mdi:web-off",
            },
        },
    },
    "binary_sensor": {
        "school_day": {"default": "mdi:school", "state": {"off": "mdi:home"}},
        "in_class": {
            "default": "mdi:account-school",
            "state": {"off": "mdi:account-school-outline"},
        },
        "lessons_canceled": {
            "default": "mdi:calendar-remove",
            "state": {"off": "mdi:calendar-check"},
        },
        "outing_today": {
            "default": "mdi:bus-school",
            "state": {"off": "mdi:school-outline"},
        },
        "test_today": {
            "default": "mdi:clipboard-text",
            "state": {"off": "mdi:clipboard-outline"},
        },
        "holidays": {"default": "mdi:beach", "state": {"off": "mdi:school"}},
        "homework_overdue": {
            "default": "mdi:book-alert",
            "state": {"off": "mdi:book-check"},
        },
        "absence_in_progress": {
            "default": "mdi:account-alert",
            "state": {"off": "mdi:account-check"},
        },
        "punishment_upcoming": {
            "default": "mdi:gavel",
            "state": {"off": "mdi:check"},
        },
        "throttled": {
            "default": "mdi:speedometer-slow",
            "state": {"off": "mdi:speedometer"},
        },
    },
    "calendar": {
        "timetable": "mdi:calendar-clock",
        "homework": "mdi:calendar-edit",
        "punishments": "mdi:calendar-alert",
    },
    "todo": {"homework": "mdi:clipboard-check-outline"},
    "button": {"refresh": "mdi:refresh", "refresh_marks": "mdi:sync"},
    "event": {
        "new_grade": "mdi:star-plus",
        "new_homework": "mdi:notebook-plus",
        # An event entity's state is the type of its last event, so the icon
        # can say which aspect of the lesson changed.
        "lesson_changed": {
            "default": "mdi:calendar-sync",
            "state_attributes": {
                "event_type": {
                    "state": {
                        "lesson_canceled": "mdi:calendar-remove",
                        "lesson_restored": "mdi:calendar-check",
                        "lesson_moved": "mdi:calendar-arrow-right",
                        "room_changed": "mdi:door",
                        "teacher_changed": "mdi:human-male-board",
                        "lesson_status_changed": "mdi:information-outline",
                    }
                }
            },
        },
        "new_information": "mdi:newspaper-plus",
        "new_absence": "mdi:account-alert",
        "new_delay": "mdi:clock-alert",
        "new_punishment": "mdi:gavel",
        "new_message": "mdi:email-plus",
        "new_evaluation": "mdi:check-decagram",
    },
    "image": {"photo": "mdi:account-box"},
}

SERVICE_ICONS: dict[str, str] = {
    "refresh": "mdi:refresh",
    "get_ical_url": "mdi:calendar-export",
    "get_identity": "mdi:card-account-details",
    "mark_homework_done": "mdi:notebook-check",
    "mark_information_read": "mdi:email-open",
    "send_message": "mdi:send",
    "generate_timetable_pdf": "mdi:file-pdf-box",
    "get_rate_limit_status": "mdi:speedometer",
    "get_attachment_url": "mdi:paperclip",
}


def _entities(index: int) -> dict[str, Any]:
    """The ``entity`` block for one language (1 = English, 2 = French)."""
    sensor: dict[str, Any] = {}
    for key, en, fr in [*SENSORS, *HISTORY_SENSORS, *DIAGNOSTIC_SENSORS]:
        sensor[key] = {"name": (en, fr)[index - 1]}
    sensor["limiter_state"]["state"] = {
        key: (en, fr)[index - 1] for key, en, fr in LIMITER_STATES
    }

    def block(rows: list[tuple[str, str, str]]) -> dict[str, Any]:
        return {key: {"name": (en, fr)[index - 1]} for key, en, fr in rows}

    # An `event` entity's *state* is the type of the last event it fired, and
    # Home Assistant displays it. Read from
    # `entity.event.<key>.state_attributes.event_type.state.<value>`; without
    # these, a dashboard showed `lesson_canceled` in snake case while the very
    # same string sat translated two blocks away under
    # `device_automation.trigger_type`. Both come from `TRIGGER_TYPES` here, so
    # the two readings of one event can no longer disagree.
    labels = {key: (en, fr)[index - 1] for key, en, fr in TRIGGER_TYPES}
    events: dict[str, Any] = block(EVENTS)
    for key, types in EVENT_TYPES_BY_ENTITY.items():
        events[key]["state_attributes"] = {
            "event_type": {
                "name": ("Event type", "Type d'évènement")[index - 1],
                "state": {value: labels[value] for value in types},
            }
        }

    return {
        "sensor": sensor,
        "binary_sensor": block(BINARY_SENSORS),
        "calendar": block(CALENDARS),
        "todo": block(TODOS),
        "button": block(BUTTONS),
        "event": events,
        "image": block(IMAGES),
    }


def _options(index: int) -> dict[str, Any]:
    """The ``options`` block for one language."""
    pick = index - 1
    description = (OPTIONS_INIT_DESCRIPTION_EN, OPTIONS_INIT_DESCRIPTION_FR)[pick]

    general_data = {row[0]: row[1 + pick] for row in GENERAL_OPTIONS}
    general_help = {row[0]: row[3 + pick] for row in GENERAL_OPTIONS if row[3 + pick]}
    limit_data = {row[0]: row[1 + pick] for row in RATE_LIMIT_OPTIONS}
    limit_help = {row[0]: row[3 + pick] for row in RATE_LIMIT_OPTIONS if row[3 + pick]}

    tier_data: dict[str, str] = {}
    tier_help: dict[str, str] = {}
    interval_help, enabled_help = (
        (
            (
                "Minutes between two collections of this category. Shorter means "
                "fresher data and more requests a day."
            ),
            (
                "Off stops collecting this category altogether and saves its "
                "requests; its entities stay unavailable."
            ),
        ),
        (
            (
                "Minutes entre deux collectes de cette catégorie. Plus court, "
                "c'est une donnée plus fraîche et plus d'appels par jour."
            ),
            (
                "Désactivé, la catégorie n'est plus collectée du tout et ses "
                "appels sont économisés ; ses entités restent indisponibles."
            ),
        ),
    )[pick]
    for key, en, fr in TIERS:
        tier_help[f"interval_{key}"] = interval_help
        tier_help[f"enabled_{key}"] = enabled_help
        label = (en, fr)[pick]
        tier_data[f"interval_{key}"] = (
            f"{label} - interval" if pick == 0 else f"{label} - intervalle"
        )
        tier_data[f"enabled_{key}"] = (
            f"{label} - enabled" if pick == 0 else f"{label} - activé"
        )

    titles = (
        {
            "init": "PRONOTE options",
            "general": "General",
            "tiers": "Collection intervals",
            "rate_limit": "Rate limiting",
        },
        {
            "init": "Options PRONOTE",
            "general": "Général",
            "tiers": "Intervalles de collecte",
            "rate_limit": "Limitation de débit",
        },
    )[pick]

    menu_labels = (
        {
            "general": "General",
            "tiers": "Collection intervals",
            "rate_limit": "Rate limiting",
        },
        {
            "general": "Général",
            "tiers": "Intervalles de collecte",
            "rate_limit": "Limitation de débit",
        },
    )[pick]

    tiers_description = (
        "One interval per category. The estimate above updates when you save.",
        (
            "Un intervalle par catégorie. L'estimation ci-dessus se met à jour à "
            "l'enregistrement."
        ),
    )[pick]

    limit_description = (
        (
            "These defaults were chosen to stay well inside what a PRONOTE server "
            "tolerates. Raising them raises the risk of a sanction, which is borne "
            "by the account, not by the integration."
        ),
        (
            "Ces valeurs par défaut ont été choisies pour rester largement dans ce "
            "qu'un serveur PRONOTE tolère. Les augmenter augmente le risque de "
            "sanction, et cette sanction pèse sur le compte, pas sur "
            "l'intégration."
        ),
    )[pick]

    timezone_error = (
        (
            "Not a timezone this system knows. It is written Region/City -- "
            "Europe/Paris, not Paris -- and it has to match the zone database "
            "exactly. Leave the box empty to follow Home Assistant's own "
            "timezone."
        ),
        (
            "Ce n'est pas un fuseau connu de ce système. Il s'écrit Région/Ville "
            "-- Europe/Paris, et non Paris -- et doit correspondre exactement à "
            "la base des fuseaux. Laissez la case vide pour suivre le fuseau de "
            "Home Assistant."
        ),
    )[pick]

    return {
        # A field error rather than a `base` one: there is exactly one box to
        # correct, and the other six fields on the page are fine.
        "error": {"invalid_timezone": timezone_error},
        "step": {
            "init": {
                "title": titles["init"],
                "description": description,
                "menu_options": menu_labels,
            },
            "general": {
                "title": titles["general"],
                "description": description,
                "data": general_data,
                "data_description": general_help,
            },
            "tiers": {
                "title": titles["tiers"],
                "description": f"{description}\n\n{tiers_description}",
                "data": tier_data,
                "data_description": tier_help,
            },
            "rate_limit": {
                "title": titles["rate_limit"],
                "description": f"{description}\n\n{limit_description}",
                "data": limit_data,
                "data_description": limit_help,
            },
        },
    }


def _services(index: int) -> dict[str, Any]:
    """The ``services`` block for one language."""
    pick = index - 1
    block: dict[str, Any] = {}
    for service in SERVICES:
        fields = {}
        for key, en, fr, help_en, help_fr in service["fields"]:
            entry: dict[str, str] = {"name": (en, fr)[pick]}
            help_text = (help_en, help_fr)[pick]
            entry["description"] = help_text or (en, fr)[pick]
            fields[key] = entry
        block[str(service["key"])] = {
            "name": service[f"name_{'en' if pick == 0 else 'fr'}"],
            "description": service[f"description_{'en' if pick == 0 else 'fr'}"],
            "fields": fields,
        }
    return block


def _catalogue(index: int) -> dict[str, Any]:
    """A complete catalogue for one language."""
    pick = index - 1
    return {
        "config": (CONFIG_EN, CONFIG_FR)[pick],
        "options": _options(index),
        "entity": _entities(index),
        "services": _services(index),
        "exceptions": {key: {"message": (en, fr)[pick]} for key, en, fr in EXCEPTIONS},
        "issues": {
            key: {
                "title": (title_en, title_fr)[pick],
                "description": (body_en, body_fr)[pick],
            }
            for key, title_en, title_fr, body_en, body_fr in ISSUES
        },
        "device_automation": {
            "trigger_type": {key: (en, fr)[pick] for key, en, fr in TRIGGER_TYPES},
            "condition_type": {key: (en, fr)[pick] for key, en, fr in CONDITION_TYPES},
            "action_type": {key: (en, fr)[pick] for key, en, fr in ACTION_TYPES},
        },
        "selector": {
            # Referenced by `services.yaml` through `translation_key: tier`, so
            # the refresh service's tier picker reads in the user's language
            # while the automation still sends the raw tier name (§10.3).
            "tier": {"options": {key: (en, fr)[pick] for key, en, fr in TIERS}},
            "session_strategy": {
                "options": (
                    {
                        "lazy": "Keep the session alive (recommended)",
                        "per_batch": "One session per batch",
                    },
                    {
                        "lazy": "Conserver la session (recommandé)",
                        "per_batch": "Une session par lot",
                    },
                )[pick]
            },
            # Referenced by `services.yaml` through `translation_key:
            # orientation`. Without it the PDF service offered "portrait" and
            # "landscape" as raw values, in English, in both catalogues.
            "orientation": {
                "options": (
                    {"portrait": "Portrait", "landscape": "Landscape"},
                    {"portrait": "Portrait", "landscape": "Paysage"},
                )[pick]
            },
        },
    }


def _icons() -> dict[str, Any]:
    """``icons.json``: one icon per entity translation key and per service.

    Raises rather than builds when a table and ``ENTITY_ICONS`` disagree, so a
    new entity cannot ship without an icon and a removed one cannot leave a
    stale entry behind.
    """
    entity: dict[str, Any] = {}
    for platform, names in _entities(1).items():
        icons = ENTITY_ICONS.get(platform, {})
        if set(icons) != set(names):
            missing = sorted(set(names) - set(icons))
            stale = sorted(set(icons) - set(names))
            message = f"{platform}: no icon for {missing}, stale icon for {stale}"
            raise ValueError(message)
        entity[platform] = {
            key: (value if isinstance(value, dict) else {"default": value})
            for key, value in icons.items()
        }
    services = [str(service["key"]) for service in SERVICES]
    if set(SERVICE_ICONS) != set(services):
        disagreement = sorted(set(SERVICE_ICONS) ^ set(services))
        message = f"service icons disagree with SERVICES: {disagreement}"
        raise ValueError(message)
    return {
        "entity": entity,
        "services": {key: {"service": SERVICE_ICONS[key]} for key in services},
    }


def _services_yaml() -> str:
    """``services.yaml``: structure only, no user-visible text.

    Names and descriptions live in ``strings.json`` so they are translatable.
    A description written here would be untranslatable and would silently win
    over the catalogue (§10.5).
    """
    lines = [
        "# Generated by scripts/build_translations.py -- do not edit by hand.",
        "#",
        "# Structure only. Every user-visible string lives in strings.json and",
        "# translations/, because a name written here cannot be translated.",
        "",
    ]
    selectors = {
        "device_id": "      device:\n        integration: carnet_scolaire",
        "tiers": (
            "      select:\n"
            "        multiple: true\n"
            "        translation_key: tier\n"
            "        options:\n"
            + "\n".join(f'          - "{key}"' for key, _en, _fr in TIERS)
        ),
        "homework_id": "      text:",
        "key": "      text:",
        "information_id": "      text:",
        "discussion_id": "      text:",
        "subject": "      text:",
        "message": "      text:\n        multiline: true",
        "recipients": "      object:",
        "done": "      boolean:",
        "day": "      date:",
        "orientation": (
            "      select:\n"
            "        translation_key: orientation\n"
            "        options:\n"
            '          - "portrait"\n'
            '          - "landscape"'
        ),
    }
    required = {
        "device_id",
        "homework_id",
        "key",
        "information_id",
        "message",
    }

    for service in SERVICES:
        lines.append(f"{service['key']}:")
        lines.append("  fields:")
        for key, *_rest in service["fields"]:
            lines.append(f"    {key}:")
            if key in required:
                lines.append("      required: true")
            lines.append("      selector:")
            # Re-indented by two. The table below is written at the depth of
            # `selector:` itself so it stays readable, and emitting it verbatim
            # made every selector body a *sibling* of `selector:` rather than
            # its child: `selector: null`, eighteen times. Home Assistant
            # validates `services.yaml` against its own schema at load time
            # (`helpers/service.py:498`), logs one line and returns `{}` -- so
            # all eight services appeared in the UI with **no fields at all**,
            # and `hassfest` failed on the same file. This is the whole defect,
            # and it is two spaces.
            lines.extend(
                f"  {line}" if line else line for line in selectors[key].splitlines()
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Write the three catalogues, ``services.yaml`` and ``icons.json``."""
    english = _catalogue(1)
    french = _catalogue(2)

    translations = COMPONENT / "translations"
    translations.mkdir(exist_ok=True)

    for path, payload in (
        (COMPONENT / "strings.json", english),
        (translations / "en.json", english),
        (translations / "fr.json", french),
    ):
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path.relative_to(ROOT)}")

    services_path = COMPONENT / "services.yaml"
    services_path.write_text(_services_yaml(), encoding="utf-8")
    print(f"wrote {services_path.relative_to(ROOT)}")

    icons_path = COMPONENT / "icons.json"
    icons_path.write_text(
        json.dumps(_icons(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {icons_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
