#!/usr/bin/env python3
"""
Wetter-Mail
-----------
Holt aktuelle Wetterdaten + eine Wetterkarte für einen festen Ort und
verschickt sie per Gmail als HTML-Mail. Gedacht für den Aufruf 2x täglich
über Task Scheduler (Windows) oder Cron (Linux/Mac).

Genutzte APIs (beide kostenlos, kein API-Key nötig):
- Open-Meteo Geocoding + Forecast API: https://open-meteo.com
- OpenStreetMap Static-Map-Dienst: https://staticmap.openstreetmap.de
"""

import smtplib
import ssl
import sys
import tempfile
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import requests
import matplotlib
matplotlib.use("Agg")  # kein Display nötig, wichtig für Cron/Task Scheduler
import matplotlib.pyplot as plt

# ============================================================
# KONFIGURATION
# ============================================================
# Läuft das Skript lokal (PC), hier direkt die Werte eintragen.
# Läuft es über GitHub Actions, kommen die Werte automatisch aus den
# GitHub Secrets (Umgebungsvariablen) - dann müssen die Zeilen unten
# NICHT verändert werden, siehe README Abschnitt "GitHub Actions".

ORT = os.environ.get("WETTER_ORT", "Berlin")  # <-- Stadtname, z.B. "München"

GMAIL_ADRESSE = os.environ.get("GMAIL_ADRESSE", "deine.adresse@gmail.com")
GMAIL_APP_PASSWORT = os.environ.get("GMAIL_APP_PASSWORT", "xxxx xxxx xxxx xxxx")
EMPFAENGER = os.environ.get("WETTER_EMPFAENGER", "empfaenger@example.com")

# ============================================================


def geocode(ort: str):
    """Ortsname -> (lat, lon, anzeigename) über Open-Meteo Geocoding API."""
    url = "https://geocoding-api.open-meteo.com/v1/search"
    r = requests.get(url, params={"name": ort, "count": 1, "language": "de"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        raise ValueError(f"Ort '{ort}' wurde nicht gefunden. Bitte ORT in der Konfiguration prüfen.")
    treffer = data["results"][0]
    name = f"{treffer['name']}, {treffer.get('country', '')}".strip(", ")
    return treffer["latitude"], treffer["longitude"], name


def hole_wetterdaten(lat: float, lon: float):
    """Aktuelle Werte + stündliche Vorhersage (48h) + Tageswerte über Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "precipitation,weathercode,windspeed_10m",
        "hourly": "temperature_2m,precipitation,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "sunrise,sunset,weathercode",
        "forecast_days": 3,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


WETTERCODES = {
    0: "Klarer Himmel", 1: "Überwiegend klar", 2: "Teilweise bewölkt", 3: "Bedeckt",
    45: "Nebel", 48: "Reifnebel",
    51: "Leichter Nieselregen", 53: "Nieselregen", 55: "Starker Nieselregen",
    61: "Leichter Regen", 63: "Regen", 65: "Starker Regen",
    71: "Leichter Schneefall", 73: "Schneefall", 75: "Starker Schneefall",
    80: "Leichte Regenschauer", 81: "Regenschauer", 82: "Heftige Regenschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Schweres Gewitter mit Hagel",
}


def wettercode_text(code: int) -> str:
    return WETTERCODES.get(code, f"Code {code}")


def erstelle_diagramm(daten: dict, pfad: str):
    """Temperatur- und Niederschlagsdiagramm für die nächsten 24h."""
    hourly = daten["hourly"]
    zeiten = [datetime.fromisoformat(t) for t in hourly["time"][:24]]
    temp = hourly["temperature_2m"][:24]
    regen = hourly["precipitation"][:24]

    fig, ax1 = plt.subplots(figsize=(8, 4))
    ax1.set_title("Temperatur & Niederschlag - nächste 24 Stunden")
    ax1.plot(zeiten, temp, color="#e07b00", linewidth=2, label="Temperatur (°C)")
    ax1.set_ylabel("Temperatur (°C)", color="#e07b00")
    ax1.tick_params(axis="y", labelcolor="#e07b00")
    ax1.set_xlabel("Uhrzeit")
    fig.autofmt_xdate()

    ax2 = ax1.twinx()
    ax2.bar(zeiten, regen, width=0.03, color="#3a7bd5", alpha=0.5, label="Niederschlag (mm)")
    ax2.set_ylabel("Niederschlag (mm)", color="#3a7bd5")
    ax2.tick_params(axis="y", labelcolor="#3a7bd5")

    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


def lade_karte(lat: float, lon: float, pfad: str):
    """Statische OSM-Karte mit Marker am Standort (kein API-Key nötig)."""
    url = "https://staticmap.openstreetmap.de/staticmap.php"
    params = {
        "center": f"{lat},{lon}",
        "zoom": 10,
        "size": "600x400",
        "markers": f"{lat},{lon},red-pushpin",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    with open(pfad, "wb") as f:
        f.write(r.content)


def baue_html(ort_name: str, daten: dict) -> str:
    cur = daten["current"]
    daily = daten["daily"]
    heute_max = daily["temperature_2m_max"][0]
    heute_min = daily["temperature_2m_min"][0]
    sonnenaufgang = daily["sunrise"][0].split("T")[1]
    sonnenuntergang = daily["sunset"][0].split("T")[1]

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; color:#222;">
      <h2>Wetter für {ort_name}</h2>
      <p><b>{wettercode_text(cur['weathercode'])}</b></p>
      <table cellpadding="4">
        <tr><td>Aktuelle Temperatur:</td><td><b>{cur['temperature_2m']} °C</b>
            (gefühlt {cur['apparent_temperature']} °C)</td></tr>
        <tr><td>Heute Min/Max:</td><td>{heute_min} °C / {heute_max} °C</td></tr>
        <tr><td>Luftfeuchtigkeit:</td><td>{cur['relative_humidity_2m']} %</td></tr>
        <tr><td>Wind:</td><td>{cur['windspeed_10m']} km/h</td></tr>
        <tr><td>Niederschlag aktuell:</td><td>{cur['precipitation']} mm</td></tr>
        <tr><td>Sonnenaufgang / -untergang:</td><td>{sonnenaufgang} / {sonnenuntergang}</td></tr>
      </table>
      <h3>Karte</h3>
      <img src="cid:karte" width="600"><br><br>
      <h3>Verlauf nächste 24h</h3>
      <img src="cid:diagramm" width="600">
      <p style="color:#888; font-size:12px;">
        Automatisch erstellt am {datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')}.
        Datenquelle: Open-Meteo.
      </p>
    </body>
    </html>
    """


def sende_mail(ort_name: str, html: str, karte_pfad: str, diagramm_pfad: str):
    msg = MIMEMultipart("related")
    msg["Subject"] = f"Wetter-Update {ort_name} - {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    msg["From"] = GMAIL_ADRESSE
    msg["To"] = EMPFAENGER
    msg.attach(MIMEText(html, "html", "utf-8"))

    with open(karte_pfad, "rb") as f:
        bild = MIMEImage(f.read())
        bild.add_header("Content-ID", "<karte>")
        msg.attach(bild)

    with open(diagramm_pfad, "rb") as f:
        bild = MIMEImage(f.read())
        bild.add_header("Content-ID", "<diagramm>")
        msg.attach(bild)

    kontext = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=kontext)
        server.login(GMAIL_ADRESSE, GMAIL_APP_PASSWORT)
        server.sendmail(GMAIL_ADRESSE, EMPFAENGER, msg.as_string())


def main():
    try:
        lat, lon, ort_name = geocode(ORT)
        daten = hole_wetterdaten(lat, lon)

        tmp = tempfile.gettempdir()
        karte_pfad = os.path.join(tmp, "wetter_karte.png")
        diagramm_pfad = os.path.join(tmp, "wetter_diagramm.png")
        lade_karte(lat, lon, karte_pfad)
        erstelle_diagramm(daten, diagramm_pfad)

        html = baue_html(ort_name, daten)
        sende_mail(ort_name, html, karte_pfad, diagramm_pfad)
        print(f"Wetter-Mail für {ort_name} erfolgreich versendet.")
    except Exception as e:
        print(f"Fehler beim Versenden der Wetter-Mail: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
