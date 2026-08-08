#!/usr/bin/env python3
"""
Warnung-Check
-------------
Prüft in kurzen Abständen (siehe .github/workflows/warnung-check.yml) auf
NEUE amtliche DWD-Unwetterwarnungen und verschickt bei einer neuen Warnung
sofort eine kurze Alarm-Mail per Gmail - unabhängig von der normalen
3x-täglichen Wetter-Mail (wetter_mail.py).

Merkt sich die zuletzt gesehenen Warnungen in gesehene_warnungen.json, damit
jede Warnung nur einmal meldet wird (nicht bei jedem Check erneut), aber bei
erneutem Auftreten nach vorherigem Abklingen wieder als neu gilt.
"""

import json
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText

from wetter_mail import (
    geocode, hole_dwd_warnungen, DWD_WARNSTUFEN,
    ORT, GMAIL_ADRESSE, GMAIL_APP_PASSWORT, EMPFAENGER,
)

GESEHENE_WARNUNGEN_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gesehene_warnungen.json"
)


def warnung_id(w: dict) -> str:
    """Eindeutiger Schlüssel pro Warnung (Stufe + Überschrift + Region)."""
    return f"{w['level']}|{w['headline']}|{w['region']}"


def lade_gesehene() -> set:
    try:
        with open(GESEHENE_WARNUNGEN_DATEI, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def speichere_gesehene(ids: set):
    with open(GESEHENE_WARNUNGEN_DATEI, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def sende_alarm_mail(neue_warnungen: list, ort_name: str):
    hoechste_stufe = max(w["level"] for w in neue_warnungen)
    symbol = "🔴" if hoechste_stufe >= 3 else "🟠"
    betreff = f"{symbol} Neue Unwetterwarnung für {ort_name}"

    abschnitte = []
    for w in neue_warnungen:
        stufe = DWD_WARNSTUFEN.get(w["level"], "Warnung")
        abschnitte.append(f"{stufe}: {w['headline']}\n\n{w['beschreibung']}")
    text = "\n\n---\n\n".join(abschnitte) + "\n\n(Automatische Alarm-Mail, Quelle: DWD)"

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = betreff
    msg["From"] = GMAIL_ADRESSE
    msg["To"] = EMPFAENGER

    kontext = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=kontext)
        server.login(GMAIL_ADRESSE, GMAIL_APP_PASSWORT)
        server.sendmail(GMAIL_ADRESSE, EMPFAENGER, msg.as_string())


def main():
    try:
        lat, lon, ort_name = geocode(ORT)
        aktuelle_warnungen = hole_dwd_warnungen(lat, lon)
    except Exception as e:
        print(f"Fehler beim Prüfen auf Warnungen: {e}", file=sys.stderr)
        sys.exit(1)

    aktuelle_ids = {warnung_id(w) for w in aktuelle_warnungen}
    gesehene_ids = lade_gesehene()
    neue_ids = aktuelle_ids - gesehene_ids

    if neue_ids:
        neue_warnungen = [w for w in aktuelle_warnungen if warnung_id(w) in neue_ids]
        try:
            sende_alarm_mail(neue_warnungen, ort_name)
            print(f"Alarm-Mail für {len(neue_warnungen)} neue Warnung(en) versendet.")
        except Exception as e:
            print(f"Fehler beim Versenden der Alarm-Mail: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Keine neuen Warnungen.")

    # Immer aktualisieren: abgeklungene Warnungen fallen aus der Liste raus,
    # damit sie bei erneutem Auftreten wieder als "neu" erkannt werden.
    speichere_gesehene(aktuelle_ids)


if __name__ == "__main__":
    main()
