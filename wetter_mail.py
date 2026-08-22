#!/usr/bin/env python3
"""
Wetter-Mail
-----------
Holt aktuelle Wetterdaten (inkl. Taupunkt), einen Modellvergleich
(GFS/ECMWF/AIFS/ICON) für 7 Tage mit Diagrammen, einen Taupunktverlauf sowie
einen Langfrist-Trend und verschickt alles per Gmail als HTML-Mail. Gedacht
für den Aufruf 3x täglich über GitHub Actions, Task Scheduler oder Cron.

Genutzte APIs (alle kostenlos, kein API-Key nötig):
- Open-Meteo Geocoding + Forecast API + Seasonal API: https://open-meteo.com
- Nominatim (OpenStreetMap) für Postleitzahlen-Suche: https://nominatim.org

Hinweis Langfrist-Trend: NOAA CFS wird von Open-Meteo nicht angeboten.
Stattdessen wird ECMWF EC46 (bis 46 Tage) / SEAS5 (bis 9 Monate) genutzt,
die vergleichbaren europäischen Langfrist-Modelle.
"""

import smtplib
import ssl
import sys
import tempfile
import os
import csv
import json
import math
import io
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import requests
from PIL import Image
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

# Telegram ist optional: leer lassen (oder Secrets nicht setzen), um nur per
# Mail zu versenden. Bot-Token über @BotFather erstellen, Chat-ID z.B. über
# @userinfobot herausfinden.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ============================================================


def geocode(ort: str):
    """
    Ortsname oder Postleitzahl -> (lat, lon, anzeigename).
    Erkennt automatisch, ob ORT eine Postleitzahl ist (4-5 Ziffern, z.B. für
    Deutschland/Österreich/Schweiz) und nutzt dann die PLZ-Suche über
    Nominatim (OpenStreetMap), da die Open-Meteo Geocoding API keine
    Postleitzahlen unterstützt. Bei normalen Ortsnamen wird weiterhin die
    Open-Meteo Geocoding API genutzt.
    """
    ort = ort.strip()
    if ort.isdigit() and 4 <= len(ort) <= 5:
        return geocode_plz(ort)
    return geocode_ortsname(ort)


def geocode_ortsname(ort: str):
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


def geocode_plz(plz: str):
    """Postleitzahl -> (lat, lon, anzeigename) über Nominatim (OpenStreetMap)."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "postalcode": plz,
        "format": "json",
        "addressdetails": 1,
        "limit": 1,
        "countrycodes": "de,at,ch",  # nur Deutschland, Österreich, Schweiz
    }
    headers = {"User-Agent": "WetterMail/1.0 (privates Automatisierungsskript)"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(
            f"Postleitzahl '{plz}' wurde nicht gefunden. Bitte ORT in der Konfiguration prüfen "
            "(funktioniert für Deutschland, Österreich und die Schweiz)."
        )
    treffer = data[0]
    adresse = treffer.get("address", {})
    ortsname = (
        adresse.get("city") or adresse.get("town") or adresse.get("village")
        or adresse.get("municipality") or adresse.get("county") or plz
    )
    land = adresse.get("country", "")
    name = f"{ortsname} ({plz}), {land}".strip(", ")
    return float(treffer["lat"]), float(treffer["lon"]), name


def hole_wetterdaten(lat: float, lon: float):
    """Aktuelle Werte + stündliche + tägliche Vorhersage (7 Tage) über Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "dew_point_2m,precipitation,weathercode,windspeed_10m,"
                   "windgusts_10m,winddirection_10m,surface_pressure",
        "hourly": "temperature_2m,precipitation,precipitation_probability,"
                  "dew_point_2m,surface_pressure,weathercode,cape",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "sunrise,sunset,weathercode,uv_index_max,sunshine_duration,"
                 "apparent_temperature_max,apparent_temperature_min",
        "forecast_days": 7,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


HIMMELSRICHTUNGEN = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
                      "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def windrichtung_text(grad: float) -> str:
    """Windrichtung in Grad -> Himmelsrichtung (z.B. 'NW')."""
    index = round(grad / 22.5) % 16
    return HIMMELSRICHTUNGEN[index]


def luftdruck_trend(hourly: dict) -> str:
    """
    Vergleicht den aktuellsten verfügbaren Luftdruckwert mit dem Wert von
    vor 3 Stunden und gibt einen kurzen Trend-Text zurück.
    """
    werte = hourly.get("surface_pressure")
    if not werte or len(werte) < 4:
        return ""
    # Index des letzten nicht-None Werts suchen (aktuellste Stunde)
    aktueller_index = None
    for i in range(len(werte) - 1, -1, -1):
        if werte[i] is not None:
            aktueller_index = i
            break
    if aktueller_index is None or aktueller_index < 3:
        return ""
    aktuell = werte[aktueller_index]
    vorher = werte[aktueller_index - 3]
    if aktuell is None or vorher is None:
        return ""
    diff = aktuell - vorher
    if diff > 1:
        pfeil = "steigend ↑"
    elif diff < -1:
        pfeil = "fallend ↓"
    else:
        pfeil = "gleichbleibend →"
    return f"{aktuell:.0f} hPa ({pfeil}, {diff:+.1f} hPa/3h)"


DWD_WARNSTUFEN = {
    1: "Wetterwarnung",
    2: "Markante Wetterwarnung",
    3: "Unwetterwarnung",
    4: "Extreme Unwetterwarnung",
}


def hole_dwd_warnungen(lat: float, lon: float):
    """
    Amtliche DWD-Unwetterwarnungen für den Landkreis/die Stadt am Standort.

    Ablauf: 1) Kreis-/Stadtname per Nominatim-Reverse-Geocoding ermitteln,
    2) passende DWD-Warncell-ID über die offizielle DWD-CSV-Liste finden,
    3) aktuelle Warnungen über die DWD-JSON-Schnittstelle abrufen und nach
    dieser ID filtern.

    Rein optionales Feature - bei jedem Fehler wird einfach eine leere
    Liste zurückgegeben (siehe try/except in main()), damit die restliche
    Mail davon nicht betroffen ist.
    """
    headers = {"User-Agent": "WetterMail/1.0 (privates Automatisierungsskript)"}

    # 1) Kreis/Stadt am Standort ermitteln
    rev = requests.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 8},
        headers=headers, timeout=15,
    )
    rev.raise_for_status()
    adresse = rev.json().get("address", {})
    gesuchte_namen = [
        adresse.get("county"), adresse.get("state_district"),
        adresse.get("city"), adresse.get("town"),
    ]
    gesuchte_namen = [n for n in gesuchte_namen if n]
    if not gesuchte_namen:
        return []

    def normalisiert(s: str) -> str:
        ersetzungen = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
        s = s.lower()
        for a, b in ersetzungen.items():
            s = s.replace(a, b)
        return s

    # 2) Warncell-ID über die DWD-CSV-Liste finden
    csv_url = ("https://www.dwd.de/DE/leistungen/opendata/help/warnungen/"
               "cap_warncellids_csv.csv?__blob=publicationFile&v=4")
    csv_r = requests.get(csv_url, headers=headers, timeout=20)
    csv_r.raise_for_status()
    zeilen = csv_r.content.decode("latin-1").splitlines()
    reader = csv.DictReader(zeilen, delimiter=";")
    # Spaltennamen können variieren - passende Spalten robust suchen
    felder = reader.fieldnames or []
    id_spalte = next((f for f in felder if "WARNCELLID" in f.upper()), None)
    name_spalte = next((f for f in felder if "NAME" in f.upper()), None)
    if not id_spalte or not name_spalte:
        return []

    gesuchte_normalisiert = [normalisiert(n) for n in gesuchte_namen]
    passende_id = None
    for row in reader:
        eintrag = normalisiert(row.get(name_spalte, ""))
        if any(g and (g in eintrag or eintrag in g) for g in gesuchte_normalisiert):
            passende_id = row.get(id_spalte, "").strip()
            break
    if not passende_id:
        return []

    # 3) Aktuelle Warnungen abrufen (JSONP, daher Präfix/Suffix entfernen)
    warn_r = requests.get(
        "https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json",
        headers=headers, timeout=15,
    )
    warn_r.raise_for_status()
    text = warn_r.text
    start = text.find("{")
    ende = text.rfind("}") + 1
    warn_daten = json.loads(text[start:ende])

    rohe_warnungen = warn_daten.get("warnings", {}).get(passende_id, [])
    ergebnis = []
    for w in rohe_warnungen:
        ergebnis.append({
            "headline": w.get("headline", ""),
            "beschreibung": w.get("description", ""),
            "level": w.get("level", 0),
            "region": w.get("regionName", ""),
        })
    return ergebnis


def baue_warnungen_html(warnungen: list) -> str:
    """HTML-Block für DWD-Unwetterwarnungen (leer, wenn keine aktiv sind)."""
    if not warnungen:
        return "<p style='color:#2e7d32;'><b>Keine amtlichen DWD-Warnungen aktiv.</b></p>"

    farben = {1: "#f9a825", 2: "#ef6c00", 3: "#c62828", 4: "#6a1b9a"}
    bloecke = []
    for w in warnungen:
        farbe = farben.get(w["level"], "#c62828")
        stufe = DWD_WARNSTUFEN.get(w["level"], "Warnung")
        bloecke.append(
            f"<div style='border-left:4px solid {farbe}; padding:6px 10px; margin:6px 0; "
            f"background:#fafafa;'><b style='color:{farbe};'>{stufe}: {w['headline']}</b>"
            f"<br><span style='font-size:13px;'>{w['beschreibung']}</span></div>"
        )
    return "".join(bloecke)


def hole_klimavergleich(lat: float, lon: float, heute_max: float, heute_min: float):
    """
    Vergleicht die heutige Vorhersage mit den tatsächlichen Werten der
    letzten 5 Jahre am selben Kalendertag (± 2 Tage Fenster) über die
    kostenlose Open-Meteo Archiv-API. Kein echtes 30-Jahres-Klimanormal,
    sondern eine grobe Einordnung "wärmer/kälter als in den Vorjahren".
    Optionales Feature - Fehler werden in main() abgefangen.
    """
    heute = datetime.now()
    alle_max, alle_min = [], []
    for jahre_zurueck in range(1, 6):
        ziel_jahr = heute.year - jahre_zurueck
        try:
            ziel_datum = heute.replace(year=ziel_jahr)
        except ValueError:
            continue  # 29. Februar in einem Jahr ohne Schaltjahr
        start = (ziel_datum - timedelta(days=2)).strftime("%Y-%m-%d")
        ende = (ziel_datum + timedelta(days=2)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start, "end_date": ende,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=20,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        alle_max += [v for v in daily.get("temperature_2m_max", []) if v is not None]
        alle_min += [v for v in daily.get("temperature_2m_min", []) if v is not None]

    if not alle_max or not alle_min:
        return None

    hist_max = sum(alle_max) / len(alle_max)
    hist_min = sum(alle_min) / len(alle_min)
    return {
        "hist_max": hist_max, "hist_min": hist_min,
        "anomalie_max": heute_max - hist_max, "anomalie_min": heute_min - hist_min,
        "jahre": len(range(1, 6)),
    }


def baue_klimavergleich_html(vergleich: dict) -> str:
    if not vergleich:
        return ""
    a_max, a_min = vergleich["anomalie_max"], vergleich["anomalie_min"]

    def text(anomalie):
        richtung = "wärmer" if anomalie >= 0 else "kälter"
        return f"{abs(anomalie):.1f}°C {richtung}"

    return (
        f"<p>Im Vergleich zum Schnitt der letzten {vergleich['jahre']} Jahre "
        f"(gleicher Kalendertag ± 2 Tage): Tagesmax {text(a_max)} "
        f"(Ø damals {vergleich['hist_max']:.0f}°C), Nachtmin {text(a_min)} "
        f"(Ø damals {vergleich['hist_min']:.0f}°C).</p>"
    )


# Modelle für den Vergleich: Name -> Open-Meteo Modell-String
MODELLE = {
    "GFS": "gfs_seamless",
    "ECMWF": "ecmwf_ifs025",
    "AIFS": "ecmwf_aifs025_single",
    "ICON": "icon_seamless",
}


def hole_modellvergleich(lat: float, lon: float):
    """
    7-Tage-Vorhersage (Max/Min-Temp, Niederschlag) für mehrere Wettermodelle
    gleichzeitig. Open-Meteo hängt bei mehreren Modellen den Modellnamen als
    Suffix an jede Variable an, z.B. temperature_2m_max_gfs_seamless.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "models": ",".join(MODELLE.values()),
        "forecast_days": 7,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def hole_langfristtrend(lat: float, lon: float):
    """
    Langfrist-Trend über die ECMWF Seasonal-API (EC46, bis 46 Tage).
    Liefert wöchentliche Durchschnittstemperatur + Niederschlagssumme.
    Läuft in eigenem try/except in main(), da diese API gelegentlich
    langsamer oder eingeschränkter ist als die normale Forecast-API.
    """
    url = "https://seasonal-api.open-meteo.com/v1/seasonal"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_mean,precipitation_sum",
        "forecast_days": 42,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, timeout=20)
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


WETTER_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "🌨️",
    80: "🌦️", 81: "🌧️", 82: "🌧️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


def wettercode_emoji(code) -> str:
    if code is None:
        return "❓"
    return WETTER_EMOJI.get(code, "❓")


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


def erstelle_cape_diagramm(daten: dict, pfad: str):
    """CAPE-Verlauf (Gewitterpotential) für die nächsten 48h, mit Einordnungslinien."""
    hourly = daten["hourly"]
    cape = hourly.get("cape")
    if not cape:
        raise ValueError("Keine CAPE-Daten in der Antwort enthalten.")

    zeiten = [datetime.fromisoformat(t) for t in hourly["time"][:48]]
    werte = cape[:48]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.fill_between(zeiten, werte, color="#8e24aa", alpha=0.3)
    ax.plot(zeiten, werte, color="#8e24aa", linewidth=1.5, label="CAPE (J/kg)")

    schwellen = [(1000, "#fbc02d", "gering/moderat"), (2500, "#fb8c00", "stark"),
                 (4000, "#c62828", "extrem")]
    for wert, farbe, label in schwellen:
        ax.axhline(wert, color=farbe, linestyle="--", linewidth=1, alpha=0.7)
        ax.text(zeiten[-1], wert, f" {wert} ({label})", color=farbe, fontsize=8, va="bottom")

    ax.set_title("CAPE - Gewitterpotential (nächste 48h)")
    ax.set_ylabel("CAPE (J/kg)")
    ax.set_xlabel("Uhrzeit")
    ax.set_ylim(bottom=0)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


def _latlon_zu_kachel(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def lade_regenradar(lat: float, lon: float, pfad: str, zoom: int = 8):
    """
    Regenradar-Kompositbild: OpenStreetMap-Kacheln als Basiskarte, darüber
    die aktuellste Niederschlagsradar-Kachel von RainViewer (kostenlos,
    kein API-Key). Zeigt ein 3x3-Kachelraster um den Standort.
    Optionales Feature - Fehler werden in main() abgefangen.
    """
    headers = {"User-Agent": "WetterMail/1.0 (privates Automatisierungsskript)"}

    # Aktuellsten Radar-Frame-Pfad von RainViewer holen
    meta = requests.get("https://api.rainviewer.com/public/weather-maps.json",
                         headers=headers, timeout=15)
    meta.raise_for_status()
    frames = meta.json().get("radar", {}).get("past", [])
    if not frames:
        raise ValueError("Keine aktuellen Radar-Frames von RainViewer verfügbar.")
    radar_pfad = frames[-1]["path"]

    tile_groesse = 256
    raster = 3
    mitte = raster // 2
    x0, y0 = _latlon_zu_kachel(lat, lon, zoom)

    gesamt = Image.new("RGB", (tile_groesse * raster, tile_groesse * raster), color="white")

    for dx in range(-mitte, mitte + 1):
        for dy in range(-mitte, mitte + 1):
            x, y = x0 + dx, y0 + dy
            # Basiskarte (OpenStreetMap)
            basis_url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
            r = requests.get(basis_url, headers=headers, timeout=15)
            r.raise_for_status()
            basis_kachel = Image.open(io.BytesIO(r.content)).convert("RGBA")

            # Niederschlagsradar-Kachel (Farbschema 2 = Universal Blue, mit Glättung)
            radar_url = f"https://tilecache.rainviewer.com{radar_pfad}/256/{zoom}/{x}/{y}/2/1_1.png"
            rr = requests.get(radar_url, headers=headers, timeout=15)
            if rr.status_code == 200 and rr.content:
                radar_kachel = Image.open(io.BytesIO(rr.content)).convert("RGBA")
                basis_kachel = Image.alpha_composite(basis_kachel, radar_kachel)

            gesamt.paste(basis_kachel.convert("RGB"),
                         ((dx + mitte) * tile_groesse, (dy + mitte) * tile_groesse))

    gesamt.save(pfad)


def baue_stundenverlauf_tabelle(daten: dict, stunden: int = 24, schritt: int = 2) -> str:
    """
    HTML-Tabelle: Stundenverlauf in 2h-Schritten (Wetter-Symbol, Temperatur,
    Taupunkt, Niederschlag), beginnend bei der aktuellsten verfügbaren Stunde.
    """
    hourly = daten["hourly"]
    zeiten_roh = hourly["time"]

    # Index der aktuellen/nächsten Stunde finden
    jetzt = datetime.now()
    start_index = 0
    for i, t in enumerate(zeiten_roh):
        if datetime.fromisoformat(t) >= jetzt:
            start_index = i
            break

    zeilen = ["<tr><th>Uhrzeit</th><th>Wetter</th><th>Temp.</th><th>Taupunkt</th><th>Niederschlag</th></tr>"]
    anzahl_schritte = stunden // schritt
    for n in range(anzahl_schritte):
        i = start_index + n * schritt
        if i >= len(zeiten_roh):
            break
        zeit = datetime.fromisoformat(zeiten_roh[i]).strftime("%a %H:%M")
        symbol = wettercode_emoji(hourly.get("weathercode", [None])[i] if i < len(hourly.get("weathercode", [])) else None)
        temp = hourly["temperature_2m"][i]
        taupunkt = hourly["dew_point_2m"][i]
        regen = hourly["precipitation"][i]
        zeilen.append(
            f"<tr><td>{zeit}</td><td style='font-size:18px;'>{symbol}</td>"
            f"<td>{temp:.0f}°C</td><td>{taupunkt:.0f}°C</td><td>{regen:.1f} mm</td></tr>"
        )

    return (
        "<table cellpadding='4' style='border-collapse:collapse; font-size:13px;' border='1'>"
        + "".join(zeilen) + "</table>"
    )


def erstelle_trenddiagramm(saison_daten: dict, pfad: str):
    """Wöchentlich gemittelter Temperatur- und Niederschlagstrend (ECMWF EC46)."""
    daily = saison_daten["daily"]
    zeiten = [datetime.fromisoformat(t) for t in daily["time"]]
    temp = daily["temperature_2m_mean"]
    regen = daily["precipitation_sum"]

    # Auf Wochenmittel / Wochensumme aggregieren (7er-Blöcke)
    wochen_labels, wochen_temp, wochen_regen = [], [], []
    for i in range(0, len(zeiten), 7):
        block_temp = [t for t in temp[i:i + 7] if t is not None]
        block_regen = [r for r in regen[i:i + 7] if r is not None]
        if not block_temp:
            continue
        wochen_labels.append(zeiten[i].strftime("%d.%m."))
        wochen_temp.append(sum(block_temp) / len(block_temp))
        wochen_regen.append(sum(block_regen))

    fig, ax1 = plt.subplots(figsize=(8, 4))
    ax1.set_title("Langfrist-Trend - ECMWF EC46 (wöchentlich gemittelt)")
    ax1.plot(wochen_labels, wochen_temp, color="#c0392b", linewidth=2, marker="o",
              label="Ø Temperatur (°C)")
    ax1.set_ylabel("Ø Temperatur (°C)", color="#c0392b")
    ax1.tick_params(axis="y", labelcolor="#c0392b")
    ax1.set_xlabel("Woche ab")
    fig.autofmt_xdate()

    ax2 = ax1.twinx()
    ax2.bar(wochen_labels, wochen_regen, color="#3a7bd5", alpha=0.4, label="Niederschlag (mm/Woche)")
    ax2.set_ylabel("Niederschlag (mm/Woche)", color="#3a7bd5")
    ax2.tick_params(axis="y", labelcolor="#3a7bd5")

    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


MODELL_FARBEN = {"GFS": "#2e7d32", "ECMWF": "#1565c0", "AIFS": "#6a1b9a", "ICON": "#e65100"}


def _max_temp_farbe(wert):
    """Hitze-Farbskala für Max-Temperaturen: ab 30°=gelb, 35°=rot, 40°=lila."""
    if wert is None:
        return None
    if wert >= 40:
        return "#8e24aa", "white"
    elif wert >= 35:
        return "#c62828", "white"
    elif wert >= 30:
        return "#f9a825", "#222"
    return None


def _min_temp_farbe(wert):
    """
    Farbskala für Min-Temperaturen (Nachtwerte): unter 10° = Grün,
    10-19° = Blau, 20-29° = Hellblau, ab 30° = Gelb.
    """
    if wert is None:
        return None
    if wert >= 30:
        return "#f9a825", "#222"    # Gelb
    elif wert >= 20:
        return "#4fc3f7", "#222"    # Hellblau
    elif wert >= 10:
        return "#1565c0", "white"   # Blau
    else:
        return "#2e7d32", "white"   # Grün


def baue_modellvergleich_tabelle(vergleich: dict) -> str:
    """
    HTML-Übersicht: 7-Tage-Vorhersage im Modellvergleich. Statt einer breiten
    10-Spalten-Tabelle (auf dem Handy unübersichtlich) gibt es pro Tag einen
    kompakten Block mit einer Zeile je Modell (farbig nach Anbieter) plus
    einer hervorgehobenen Mittelwert-Zeile. Temperaturen sind farblich nach
    Hitze/Kälte markiert.
    """
    daily = vergleich["daily"]
    tage = daily["time"]
    bloecke = []

    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%A, %d.%m.")
        zeilen_html = []
        niederschlag_werte, max_werte, min_werte = [], [], []

        for name, suffix in MODELLE.items():
            max_t = daily.get(f"temperature_2m_max_{suffix}", [None] * len(tage))[i]
            min_t = daily.get(f"temperature_2m_min_{suffix}", [None] * len(tage))[i]
            regen = daily.get(f"precipitation_sum_{suffix}", [None] * len(tage))[i]
            if regen is not None:
                niederschlag_werte.append(regen)
            if max_t is not None:
                max_werte.append(max_t)
            if min_t is not None:
                min_werte.append(min_t)

            max_txt = f"{max_t:.0f}°" if max_t is not None else "-"
            min_txt = f"{min_t:.0f}°" if min_t is not None else "-"
            max_farbe = _max_temp_farbe(max_t)
            min_farbe = _min_temp_farbe(min_t)
            max_style = f"background:{max_farbe[0]}; color:{max_farbe[1]}; font-weight:bold;" if max_farbe else ""
            min_style = f"background:{min_farbe[0]}; color:{min_farbe[1]}; font-weight:bold;" if min_farbe else ""
            modell_farbe = MODELL_FARBEN.get(name, "#555")

            zeilen_html.append(
                f"<tr>"
                f"<td style='background:{modell_farbe}; color:white; font-weight:bold; "
                f"padding:5px 8px;'>{name}</td>"
                f"<td style='padding:5px 8px; text-align:center; {max_style}'>{max_txt}</td>"
                f"<td style='padding:5px 8px; text-align:center; {min_style}'>{min_txt}</td>"
                f"</tr>"
            )

        mittel_max = sum(max_werte) / len(max_werte) if max_werte else None
        mittel_min = sum(min_werte) / len(min_werte) if min_werte else None
        mittel_max_txt = f"{mittel_max:.0f}°" if mittel_max is not None else "-"
        mittel_min_txt = f"{mittel_min:.0f}°" if mittel_min is not None else "-"
        mittel_max_farbe = _max_temp_farbe(mittel_max)
        mittel_min_farbe = _min_temp_farbe(mittel_min)
        mittel_max_style = (f"background:{mittel_max_farbe[0]}; color:{mittel_max_farbe[1]};"
                             if mittel_max_farbe else "")
        mittel_min_style = (f"background:{mittel_min_farbe[0]}; color:{mittel_min_farbe[1]};"
                             if mittel_min_farbe else "")
        regen_avg = f"{sum(niederschlag_werte)/len(niederschlag_werte):.1f} mm" if niederschlag_werte else "-"

        zeilen_html.append(
            f"<tr style='background:#eeeeee; font-weight:bold;'>"
            f"<td style='padding:5px 8px;'>Ø Mittel</td>"
            f"<td style='padding:5px 8px; text-align:center; {mittel_max_style}'>{mittel_max_txt}</td>"
            f"<td style='padding:5px 8px; text-align:center; {mittel_min_style}'>{mittel_min_txt}</td>"
            f"</tr>"
        )

        bloecke.append(
            f"<div style='margin-bottom:14px;'>"
            f"<div style='font-weight:bold; padding:4px 0;'>{datum} "
            f"<span style='font-weight:normal; color:#666; font-size:12px;'>"
            f"(Niederschlag Ø {regen_avg})</span></div>"
            f"<table cellpadding='0' cellspacing='0' style='border-collapse:collapse; width:100%; "
            f"max-width:360px; font-size:13px;' border='1'>"
            f"<tr><th style='padding:5px 8px;'>Modell</th><th style='padding:5px 8px;'>Max</th>"
            f"<th style='padding:5px 8px;'>Min (Nacht)</th></tr>"
            + "".join(zeilen_html) + "</table></div>"
        )

    return (
        "".join(bloecke)
        + "<p style='color:#888; font-size:11px;'>Max-Temperatur: Gelb ab 30°C, Rot ab 35°C, "
          "Lila ab 40°C. Min-Temperatur: Grün unter 10°C, Blau 10-19°C, "
          "Hellblau 20-29°C, Gelb ab 30°C. "
          "'Ø Mittel' = Durchschnitt über alle vier Modelle.</p>"
    )


def erstelle_modelltemperaturdiagramm(vergleich: dict, pfad: str):
    """
    Temperaturverlauf (Tages-Max) über 7 Tage, eine Linie je Modell, plus
    horizontale Schwellenlinien für Hitze-Referenzwerte.
    """
    daily = vergleich["daily"]
    tage = [datetime.fromisoformat(t) for t in daily["time"]]
    labels = [t.strftime("%a %d.%m.") for t in tage]

    farben = {"GFS": "#2e7d32", "ECMWF": "#1565c0", "AIFS": "#6a1b9a", "ICON": "#e65100"}

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for name, suffix in MODELLE.items():
        werte = daily.get(f"temperature_2m_max_{suffix}")
        if werte:
            ax.plot(labels, werte, marker="o", linewidth=2, label=name,
                    color=farben.get(name))

    # Schwellenlinien
    schwellen = [(20, "blue", "20°C"), (30, "gold", "30°C"),
                 (35, "red", "35°C"), (40, "purple", "40°C")]
    for wert, farbe, label in schwellen:
        ax.axhline(wert, color=farbe, linestyle="--", linewidth=1, alpha=0.7)
        ax.text(len(labels) - 1, wert, f" {label}", color=farbe, fontsize=9, va="bottom")

    ax.set_title("7-Tage-Temperaturverlauf im Modellvergleich (Tagesmaximum)")
    ax.set_ylabel("Temperatur (°C)")
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


def erstelle_taupunktdiagramm(daten: dict, pfad: str):
    """Taupunktverlauf über den kompletten abgerufenen Zeitraum (bis zu 7 Tage), mit Schwellenlinien."""
    hourly = daten["hourly"]
    zeiten = [datetime.fromisoformat(t) for t in hourly["time"]]
    taupunkt = hourly["dew_point_2m"]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(zeiten, taupunkt, color="#00838f", linewidth=1.5, label="Taupunkt (°C)")

    schwellen = [(16, "red", "16°C"), (20, "purple", "20°C")]
    for wert, farbe, label in schwellen:
        ax.axhline(wert, color=farbe, linestyle="--", linewidth=1, alpha=0.7)
        ax.text(zeiten[-1], wert, f" {label}", color=farbe, fontsize=9, va="bottom")

    ax.set_title("Taupunktverlauf")
    ax.set_ylabel("Taupunkt (°C)")
    ax.set_xlabel("Datum")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


def erstelle_gefuehlte_temp_diagramm(daten: dict, pfad: str):
    """Gefühlte Temperatur (Max/Min) im 7-Tage-Verlauf."""
    daily = daten["daily"]
    tage = [datetime.fromisoformat(t) for t in daily["time"]]
    labels = [t.strftime("%a %d.%m.") for t in tage]
    gef_max = daily.get("apparent_temperature_max")
    gef_min = daily.get("apparent_temperature_min")
    if not gef_max or not gef_min:
        raise ValueError("Keine Daten zur gefühlten Temperatur in der Antwort enthalten.")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(labels, gef_max, marker="o", linewidth=2, color="#d84315", label="Gefühlt Max")
    ax.plot(labels, gef_min, marker="o", linewidth=2, color="#1565c0", label="Gefühlt Min")
    ax.fill_between(labels, gef_min, gef_max, color="#ffab91", alpha=0.2)

    ax.set_title("Gefühlte Temperatur - 7-Tage-Verlauf")
    ax.set_ylabel("Gefühlte Temperatur (°C)")
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(pfad, dpi=120)
    plt.close(fig)


def baue_klartext(daten: dict, vergleich: dict, warnungen: list) -> str:
    """Regelbasierter Klartext-Bericht in einem Satz/Absatz (kein LLM, rein aus den Zahlen abgeleitet)."""
    cur = daten["current"]
    daily = daten["daily"]
    heute_max = daily["temperature_2m_max"][0]
    heute_min = daily["temperature_2m_min"][0]
    regen_heute = daily["precipitation_sum"][0] if daily.get("precipitation_sum") else 0

    teile = [f"{wettercode_text(cur['weathercode'])}, {heute_min:.0f}–{heute_max:.0f}°C."]

    if regen_heute and regen_heute > 0.5:
        teile.append(f"Mit ca. {regen_heute:.0f} mm Niederschlag ist heute zu rechnen.")
    else:
        teile.append("Kaum Niederschlag erwartet.")

    if heute_min < 0:
        teile.append("Nachts Frost möglich.")
    elif heute_min < 5:
        teile.append("Nachts kühl, im Bergland ggf. Bodenfrost.")

    if heute_max >= 30:
        teile.append("Deutliche Hitze am Tag - ausreichend trinken.")

    # Wochentrend: Vergleich Ø Max erste vs. zweite Hälfte der Woche
    max_werte = [v for v in daily.get("temperature_2m_max", []) if v is not None]
    if len(max_werte) >= 6:
        erste_haelfte = sum(max_werte[:3]) / 3
        zweite_haelfte = sum(max_werte[3:6]) / 3
        if zweite_haelfte - erste_haelfte > 2:
            teile.append("Im Wochenverlauf wird es spürbar wärmer.")
        elif erste_haelfte - zweite_haelfte > 2:
            teile.append("Im Wochenverlauf kühlt es spürbar ab.")

    if vergleich:
        richtung = "wärmer" if vergleich["anomalie_max"] >= 0 else "kälter"
        teile.append(f"Damit {abs(vergleich['anomalie_max']):.0f}°C {richtung} als in den Vorjahren üblich.")

    if warnungen:
        hoechste_stufe = max(w["level"] for w in warnungen)
        teile.append(f"Achtung: {DWD_WARNSTUFEN.get(hoechste_stufe, 'Wetterwarnung')} aktiv, Details unten.")

    return " ".join(teile)


def baue_wochenuebersicht_html(daten: dict, vergleich: dict) -> str:
    """
    Kompakte 7-Tage-Mini-Übersicht (Symbol + Min/Max) für den Anfang der Mail.
    Die Temperaturwerte sind der Mittelwert aus allen vier Modellen
    (GFS/ECMWF/AIFS/ICON) statt nur des einzelnen best_match-Modells (das
    bei Open-Meteo für Europa meist ICON ist und z.B. bei Hitze manchmal
    deutlich höher liegt als der Modell-Durchschnitt). Das Wetter-Symbol
    kommt weiterhin aus der best_match-Vorhersage, da die Modelle im
    Vergleich keinen Wettercode liefern.
    """
    daily = daten["daily"]
    tage = daily["time"]
    vergleich_daily = vergleich.get("daily", {})

    zellen = []
    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%a")
        symbol = wettercode_emoji(daily.get("weathercode", [None] * len(tage))[i])

        max_werte = [vergleich_daily[f"temperature_2m_max_{suffix}"][i]
                     for suffix in MODELLE.values()
                     if f"temperature_2m_max_{suffix}" in vergleich_daily
                     and i < len(vergleich_daily[f"temperature_2m_max_{suffix}"])
                     and vergleich_daily[f"temperature_2m_max_{suffix}"][i] is not None]
        min_werte = [vergleich_daily[f"temperature_2m_min_{suffix}"][i]
                     for suffix in MODELLE.values()
                     if f"temperature_2m_min_{suffix}" in vergleich_daily
                     and i < len(vergleich_daily[f"temperature_2m_min_{suffix}"])
                     and vergleich_daily[f"temperature_2m_min_{suffix}"][i] is not None]

        max_t = sum(max_werte) / len(max_werte) if max_werte else daily["temperature_2m_max"][i]
        min_t = sum(min_werte) / len(min_werte) if min_werte else daily["temperature_2m_min"][i]

        zellen.append(
            f"<td style='text-align:center; padding:6px 10px;'>"
            f"<div style='font-size:12px; color:#555;'>{datum}</div>"
            f"<div style='font-size:22px;'>{symbol}</div>"
            f"<div style='font-size:12px;'><b>{max_t:.0f}°</b>/{min_t:.0f}°</div></td>"
        )
    return (
        "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse; margin:8px 0;'>"
        "<tr>" + "".join(zellen) + "</tr></table>"
    )


VORHERSAGE_HISTORIE_DATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "letzte_vorhersage.json")


def lade_letzte_vorhersage():
    """Liest die beim letzten Lauf gespeicherte Vorhersage (falls vorhanden)."""
    try:
        with open(VORHERSAGE_HISTORIE_DATEI, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def speichere_vorhersage(daten: dict):
    """Speichert die aktuelle Vorhersage für den Vergleich beim nächsten Lauf."""
    daily = daten["daily"]
    inhalt = {
        "zeitstempel": datetime.now().isoformat(),
        "tage": {
            tag: {"max": daily["temperature_2m_max"][i], "min": daily["temperature_2m_min"][i]}
            for i, tag in enumerate(daily["time"])
        },
    }
    with open(VORHERSAGE_HISTORIE_DATEI, "w", encoding="utf-8") as f:
        json.dump(inhalt, f, ensure_ascii=False, indent=2)


def baue_vorhersage_vergleich_html(alt: dict, daten: dict) -> str:
    """
    Vergleicht die heutige Vorhersage (erster Tag) mit der beim letzten Lauf
    gespeicherten Vorhersage für denselben Kalendertag.
    """
    if not alt:
        return ""
    daily = daten["daily"]
    heute = daily["time"][0]
    alte_werte = alt.get("tage", {}).get(heute)
    if not alte_werte:
        return ""

    neu_max = daily["temperature_2m_max"][0]
    neu_min = daily["temperature_2m_min"][0]
    diff_max = neu_max - alte_werte["max"]
    diff_min = neu_min - alte_werte["min"]

    if abs(diff_max) < 0.5 and abs(diff_min) < 0.5:
        return ""  # keine nennenswerte Änderung - nichts anzeigen

    try:
        alter_zeitstempel = datetime.fromisoformat(alt["zeitstempel"]).strftime("%H:%M Uhr")
    except (KeyError, ValueError):
        alter_zeitstempel = "letztem Lauf"

    def richtungstext(diff):
        return f"{abs(diff):.1f}°C {'wärmer' if diff > 0 else 'kälter'}"

    teile = []
    if abs(diff_max) >= 0.5:
        teile.append(f"Tagesmax jetzt {richtungstext(diff_max)} ({alte_werte['max']:.0f}°C → {neu_max:.0f}°C)")
    if abs(diff_min) >= 0.5:
        teile.append(f"Nachtmin jetzt {richtungstext(diff_min)} ({alte_werte['min']:.0f}°C → {neu_min:.0f}°C)")

    return (
        f"<p style='background:#fff8e1; padding:8px 12px; border-left:4px solid #fbc02d;'>"
        f"<b>Änderung seit {alter_zeitstempel}:</b> {', '.join(teile)}.</p>"
    )


def baue_tiefstwerttabelle_html(daten: dict) -> str:
    """
    Tabelle mit dem gefühlten nächtlichen Tiefstwert pro Tag (7 Tage),
    farblich markiert: unter 20°C = blau, ab 20°C = gelb, ab 25°C = rot
    (Einordnung Richtung "Tropennacht" - Schwellen für unruhigen Schlaf).
    """
    daily = daten["daily"]
    tage = daily["time"]
    gef_min = daily.get("apparent_temperature_min")
    if not gef_min:
        return ""

    def farbe_fuer(wert):
        if wert >= 25:
            return "#c62828", "white"   # Rot
        elif wert >= 20:
            return "#f9a825", "#222"    # Gelb
        else:
            return "#1565c0", "white"   # Blau

    zeilen = ["<tr><th>Datum</th><th>Gefühlter Nachttiefstwert</th></tr>"]
    for i, tag in enumerate(tage):
        if i >= len(gef_min) or gef_min[i] is None:
            continue
        datum = datetime.fromisoformat(tag).strftime("%a %d.%m.")
        wert = gef_min[i]
        hg_farbe, text_farbe = farbe_fuer(wert)
        zeilen.append(
            f"<tr><td>{datum}</td>"
            f"<td style='background:{hg_farbe}; color:{text_farbe}; font-weight:bold; "
            f"text-align:center;'>{wert:.0f}°C</td></tr>"
        )

    return (
        "<h3>Gefühlter Nacht-Tiefstwert (7 Tage)</h3>"
        "<table cellpadding='6' style='border-collapse:collapse; font-size:13px;' border='1'>"
        + "".join(zeilen) + "</table>"
        "<p style='color:#888; font-size:11px;'>Blau = unter 20°C, Gelb = ab 20°C, "
        "Rot = ab 25°C (Tropennacht-Bereich, oft unruhiger Schlaf).</p>"
    )


def baue_html(ort_name: str, daten: dict, vergleich: dict, modellvergleich_html: str, hat_trend: bool,
              warnungen: list, klimavergleich: dict, hat_radar: bool, alte_vorhersage: dict) -> str:
    cur = daten["current"]
    daily = daten["daily"]
    heute_max = daily["temperature_2m_max"][0]
    heute_min = daily["temperature_2m_min"][0]
    sonnenaufgang = daily["sunrise"][0].split("T")[1]
    sonnenuntergang = daily["sunset"][0].split("T")[1]
    uv_index = daily.get("uv_index_max", [None])[0]
    sonnenschein_std = (daily.get("sunshine_duration", [0])[0] or 0) / 3600
    luftdruck = luftdruck_trend(daten["hourly"])
    windrichtung = windrichtung_text(cur["winddirection_10m"]) if cur.get("winddirection_10m") is not None else "-"

    klartext = baue_klartext(daten, klimavergleich, warnungen)
    warnungen_html = baue_warnungen_html(warnungen)
    klimavergleich_html = baue_klimavergleich_html(klimavergleich)
    stundenverlauf_html = baue_stundenverlauf_tabelle(daten)
    wochenuebersicht_html = baue_wochenuebersicht_html(daten, vergleich)
    tiefstwerttabelle_html = baue_tiefstwerttabelle_html(daten)
    vorhersage_vergleich_html = baue_vorhersage_vergleich_html(alte_vorhersage, daten)

    trend_block = ""
    if hat_trend:
        trend_block = """
      <h3 id="trend">Langfrist-Trend (ECMWF EC46, bis 46 Tage)</h3>
      <img src="cid:trend" width="600">
      <p style="color:#888; font-size:11px;">
        NOAA CFS ist über die kostenlose Open-Meteo-API nicht verfügbar. Diese Ansicht nutzt
        stattdessen ECMWF EC46, das vergleichbare europäische Langfrist-Modell. Nicht
        bias-korrigiert - als grobe Tendenz zu verstehen, nicht als Tagesvorhersage.
      </p>"""

    radar_block = ""
    if hat_radar:
        radar_block = """
      <h3 id="radar">Regenradar</h3>
      <img src="cid:radar" width="600">
      <p style="color:#888; font-size:11px;">Quelle: RainViewer. Zeigt die aktuellste verfügbare
      Niederschlagsradar-Aufnahme, überlagert auf einer OpenStreetMap-Basiskarte.</p>"""

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; color:#222;">
      <h2>Wetter für {ort_name}</h2>
      {wochenuebersicht_html}
      <p style="font-size:15px;">{klartext}</p>
      {vorhersage_vergleich_html}
      <h3>Amtliche Warnungen (DWD)</h3>
      {warnungen_html}
      <table cellpadding="4">
        <tr><td>Aktuelle Temperatur:</td><td><b>{cur['temperature_2m']} °C</b>
            (gefühlt {cur['apparent_temperature']} °C)</td></tr>
        <tr><td>Heute Min/Max:</td><td>{heute_min} °C / {heute_max} °C</td></tr>
        <tr><td>Taupunkt:</td><td>{cur['dew_point_2m']} °C</td></tr>
        <tr><td>Luftfeuchtigkeit:</td><td>{cur['relative_humidity_2m']} %</td></tr>
        <tr><td>Luftdruck:</td><td>{luftdruck or f"{cur.get('surface_pressure','-')} hPa"}</td></tr>
        <tr><td>Wind:</td><td>{cur['windspeed_10m']} km/h, Böen {cur.get('windgusts_10m','-')} km/h, aus {windrichtung}</td></tr>
        <tr><td>UV-Index (Tagesmax):</td><td>{uv_index if uv_index is not None else '-'}</td></tr>
        <tr><td>Sonnenscheindauer heute:</td><td>{sonnenschein_std:.1f} Std.</td></tr>
        <tr><td>Niederschlag aktuell:</td><td>{cur['precipitation']} mm</td></tr>
        <tr><td>Sonnenaufgang / -untergang:</td><td>{sonnenaufgang} / {sonnenuntergang}</td></tr>
      </table>
      {klimavergleich_html}
      {radar_block}
      <h3 id="stundenverlauf">Stundenverlauf (2h-Schritte)</h3>
      {stundenverlauf_html}
      <h3 id="verlauf24h">Verlauf nächste 24h</h3>
      <img src="cid:diagramm" width="600">
      <h3 id="modellvergleich">7-Tage-Modellvergleich (GFS / ECMWF / AIFS / ICON)</h3>
      {modellvergleich_html}
      <h3 id="modelltemp">7-Tage-Temperaturverlauf im Modellvergleich</h3>
      <img src="cid:modelltemp" width="600">
      <h3 id="gefuehlt">Gefühlte Temperatur - 7-Tage-Verlauf</h3>
      <img src="cid:gefuehlt" width="600">
      <h3 id="taupunkt">Taupunktverlauf</h3>
      <img src="cid:taupunkt" width="600">
      <h3 id="cape">CAPE - Gewitterpotential (48h)</h3>
      <img src="cid:cape" width="600">
      {trend_block}
      {tiefstwerttabelle_html}
      <p style="color:#888; font-size:12px;">
        Automatisch erstellt am {datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')}.
        Datenquelle: Open-Meteo, DWD.

      </p>
    </body>
    </html>
    """


def _chunke_telegram_text(bloecke: list, limit: int = 3500) -> list:
    """
    Fügt Text-Blöcke zu Nachrichten unter der Telegram-Zeichengrenze
    zusammen. Blöcke werden möglichst nicht mitten durchgeschnitten - nur
    wenn ein einzelner Block selbst zu lang ist, wird er zeilenweise geteilt.
    """
    nachrichten = []
    aktuell = ""
    for block in bloecke:
        if not block:
            continue
        kandidat = f"{aktuell}\n\n{block}" if aktuell else block
        if len(kandidat) <= limit:
            aktuell = kandidat
            continue
        if aktuell:
            nachrichten.append(aktuell)
            aktuell = ""
        if len(block) <= limit:
            aktuell = block
            continue
        teil = ""
        for zeile in block.split("\n"):
            kandidat2 = f"{teil}\n{zeile}" if teil else zeile
            if len(kandidat2) <= limit:
                teil = kandidat2
            else:
                if teil:
                    nachrichten.append(teil)
                teil = zeile
        aktuell = teil
    if aktuell:
        nachrichten.append(aktuell)
    return nachrichten


def baue_vorhersage_vergleich_text(alt: dict, daten: dict) -> str:
    """Textvariante von baue_vorhersage_vergleich_html (für Telegram)."""
    if not alt:
        return ""
    daily = daten["daily"]
    heute = daily["time"][0]
    alte_werte = alt.get("tage", {}).get(heute)
    if not alte_werte:
        return ""
    neu_max = daily["temperature_2m_max"][0]
    neu_min = daily["temperature_2m_min"][0]
    diff_max = neu_max - alte_werte["max"]
    diff_min = neu_min - alte_werte["min"]
    if abs(diff_max) < 0.5 and abs(diff_min) < 0.5:
        return ""
    try:
        alter_zeitstempel = datetime.fromisoformat(alt["zeitstempel"]).strftime("%H:%M Uhr")
    except (KeyError, ValueError):
        alter_zeitstempel = "letztem Lauf"

    def richtungstext(diff):
        return f"{abs(diff):.1f}°C {'wärmer' if diff > 0 else 'kälter'}"

    teile = []
    if abs(diff_max) >= 0.5:
        teile.append(f"Tagesmax jetzt {richtungstext(diff_max)} ({alte_werte['max']:.0f}°C -> {neu_max:.0f}°C)")
    if abs(diff_min) >= 0.5:
        teile.append(f"Nachtmin jetzt {richtungstext(diff_min)} ({alte_werte['min']:.0f}°C -> {neu_min:.0f}°C)")
    return f"Änderung seit {alter_zeitstempel}: " + ", ".join(teile)


def baue_telegram_stundenverlauf_text(daten: dict, stunden: int = 24, schritt: int = 2) -> str:
    """Textvariante der Stundenverlauf-Tabelle (für Telegram)."""
    hourly = daten["hourly"]
    zeiten_roh = hourly["time"]
    jetzt = datetime.now()
    start_index = 0
    for i, t in enumerate(zeiten_roh):
        if datetime.fromisoformat(t) >= jetzt:
            start_index = i
            break

    zeilen = ["Stundenverlauf (2h-Schritte):"]
    for n in range(stunden // schritt):
        i = start_index + n * schritt
        if i >= len(zeiten_roh):
            break
        zeit = datetime.fromisoformat(zeiten_roh[i]).strftime("%a %H:%M")
        wc_liste = hourly.get("weathercode", [])
        symbol = wettercode_emoji(wc_liste[i] if i < len(wc_liste) else None)
        temp = hourly["temperature_2m"][i]
        taupunkt = hourly["dew_point_2m"][i]
        regen = hourly["precipitation"][i]
        zeilen.append(f"{zeit}  {symbol}  {temp:.0f}°C  Taupunkt {taupunkt:.0f}°C  Regen {regen:.1f}mm")
    return "\n".join(zeilen)


def baue_telegram_modellvergleich_text(vergleich: dict) -> str:
    """Textvariante der Modellvergleich-Tabelle (für Telegram)."""
    daily = vergleich["daily"]
    tage = daily["time"]
    tagesbloecke = []
    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%a %d.%m.")
        zeilen = [f"{datum}:"]
        max_werte, min_werte, regen_werte = [], [], []
        for name, suffix in MODELLE.items():
            max_t = daily.get(f"temperature_2m_max_{suffix}", [None] * len(tage))[i]
            min_t = daily.get(f"temperature_2m_min_{suffix}", [None] * len(tage))[i]
            regen = daily.get(f"precipitation_sum_{suffix}", [None] * len(tage))[i]
            if max_t is not None:
                max_werte.append(max_t)
            if min_t is not None:
                min_werte.append(min_t)
            if regen is not None:
                regen_werte.append(regen)
            werte_txt = f"{max_t:.0f}°/{min_t:.0f}°" if max_t is not None and min_t is not None else "-"
            zeilen.append(f"  {name}: {werte_txt}")
        if max_werte and min_werte:
            mittel_max = sum(max_werte) / len(max_werte)
            mittel_min = sum(min_werte) / len(min_werte)
            zeilen.append(f"  Ø Mittel: {mittel_max:.0f}°/{mittel_min:.0f}°")
        if regen_werte:
            zeilen.append(f"  Niederschlag Ø: {sum(regen_werte)/len(regen_werte):.1f}mm")
        tagesbloecke.append("\n".join(zeilen))
    return "7-Tage-Modellvergleich (GFS/ECMWF/AIFS/ICON):\n\n" + "\n\n".join(tagesbloecke)


def baue_telegram_tiefstwert_text(daten: dict) -> str:
    """Textvariante der Tiefstwert-Tabelle (für Telegram)."""
    daily = daten["daily"]
    tage = daily["time"]
    gef_min = daily.get("apparent_temperature_min")
    if not gef_min:
        return ""
    zeilen = ["Gefühlter Nacht-Tiefstwert (7 Tage):"]
    for i, tag in enumerate(tage):
        if i >= len(gef_min) or gef_min[i] is None:
            continue
        datum = datetime.fromisoformat(tag).strftime("%a %d.%m.")
        wert = gef_min[i]
        symbol = "🔴" if wert >= 25 else "🟡" if wert >= 20 else "🔵"
        zeilen.append(f"{datum}: {symbol} {wert:.0f}°C")
    return "\n".join(zeilen)


def sende_telegram(ort_name: str, daten: dict, vergleich: dict, warnungen: list,
                    klimavergleich: dict, alte_vorhersage: dict, betreff_praefix: str,
                    bilder: list):
    """
    Verschickt den kompletten Inhalt der Wetter-Mail zusätzlich per
    Telegram-Bot: alle Textabschnitte (in mehreren Nachrichten, da Telegram
    eine Nachricht auf ~4096 Zeichen begrenzt) sowie alle Diagramme als
    Fotos mit Bildunterschrift. Komplett optional - wird stillschweigend
    übersprungen, wenn kein Bot-Token/Chat-ID gesetzt ist. Kein parse_mode
    (Markdown), damit Sonderzeichen/Emojis nie zu
    "can't parse entities"-Fehlern führen.

    bilder: Liste aus (dateipfad, bildunterschrift) - Einträge mit
    dateipfad=None oder nicht existierender Datei werden übersprungen.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    cur = daten["current"]
    daily = daten["daily"]
    tage = daily["time"]
    heute_max = daily["temperature_2m_max"][0]
    heute_min = daily["temperature_2m_min"][0]
    uv_index = daily.get("uv_index_max", [None])[0]
    sonnenschein_std = (daily.get("sunshine_duration", [0])[0] or 0) / 3600
    luftdruck = luftdruck_trend(daten["hourly"])
    windrichtung = windrichtung_text(cur["winddirection_10m"]) if cur.get("winddirection_10m") is not None else "-"

    luftdruck_text = luftdruck or f"{cur.get('surface_pressure', '-')} hPa"

    # --- Block 1: Kopf mit Klartext + aktuelle Werte ---
    kopf = [f"{betreff_praefix}Wetter {ort_name}".strip()]
    kopf.append(baue_klartext(daten, klimavergleich, warnungen))
    kopf.append("")
    kopf.append(f"Aktuell: {cur['temperature_2m']}°C (gefühlt {cur['apparent_temperature']}°C)")
    kopf.append(f"Heute Min/Max: {heute_min}°C / {heute_max}°C")
    kopf.append(f"Taupunkt: {cur['dew_point_2m']}°C")
    kopf.append(f"Luftfeuchtigkeit: {cur['relative_humidity_2m']}%")
    kopf.append(f"Luftdruck: {luftdruck_text}")
    kopf.append(f"Wind: {cur['windspeed_10m']} km/h, Böen {cur.get('windgusts_10m', '-')} km/h, aus {windrichtung}")
    kopf.append(f"UV-Index: {uv_index if uv_index is not None else '-'}")
    kopf.append(f"Sonnenschein heute: {sonnenschein_std:.1f} Std.")
    kopf.append(f"Sonnenauf-/untergang: {daily['sunrise'][0].split('T')[1]} / {daily['sunset'][0].split('T')[1]}")
    block_kopf = "\n".join(kopf)

    # --- Block 2: 7-Tage-Mini-Übersicht ---
    tage_zeile = ["7-Tage-Übersicht:"]
    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%a")
        symbol = wettercode_emoji(daily.get("weathercode", [None] * len(tage))[i])
        tage_zeile.append(f"{datum} {symbol} {daily['temperature_2m_max'][i]:.0f}°/{daily['temperature_2m_min'][i]:.0f}°")
    block_wochenuebersicht = " | ".join(tage_zeile)

    # --- Block 3: Vorhersage-Vergleich (nur bei Änderung) ---
    block_vergleich = baue_vorhersage_vergleich_text(alte_vorhersage, daten)

    # --- Block 4: DWD-Warnungen ---
    if warnungen:
        warnzeilen = ["⚠️ Amtliche DWD-Warnungen:"]
        for w_ in warnungen:
            stufe = DWD_WARNSTUFEN.get(w_["level"], "Warnung")
            warnzeilen.append(f"- {stufe}: {w_['headline']}")
            warnzeilen.append(f"  {w_['beschreibung']}")
        block_warnungen = "\n".join(warnzeilen)
    else:
        block_warnungen = "Keine amtlichen DWD-Warnungen aktiv."

    # --- Block 5: Klimavergleich ---
    block_klima = ""
    if klimavergleich:
        a_max, a_min = klimavergleich["anomalie_max"], klimavergleich["anomalie_min"]
        r_max = "wärmer" if a_max >= 0 else "kälter"
        r_min = "wärmer" if a_min >= 0 else "kälter"
        block_klima = (
            f"Vergleich zu den letzten {klimavergleich['jahre']} Jahren: "
            f"Tagesmax {abs(a_max):.1f}°C {r_max}, Nachtmin {abs(a_min):.1f}°C {r_min}"
        )

    # --- Block 6-8: Stundenverlauf, Modellvergleich, Tiefstwerte ---
    block_stundenverlauf = baue_telegram_stundenverlauf_text(daten)
    block_modellvergleich = baue_telegram_modellvergleich_text(vergleich)
    block_tiefstwerte = baue_telegram_tiefstwert_text(daten)

    alle_bloecke = [
        block_kopf, block_wochenuebersicht, block_vergleich, block_warnungen,
        block_klima, block_stundenverlauf, block_modellvergleich, block_tiefstwerte,
    ]

    basis_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    for text in _chunke_telegram_text(alle_bloecke):
        r = requests.post(f"{basis_url}/sendMessage",
                           data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        r.raise_for_status()

    for pfad, bildunterschrift in bilder:
        if not pfad or not os.path.exists(pfad):
            continue
        with open(pfad, "rb") as f:
            r2 = requests.post(f"{basis_url}/sendPhoto",
                                data={"chat_id": TELEGRAM_CHAT_ID, "caption": bildunterschrift},
                                files={"photo": f}, timeout=30)
            r2.raise_for_status()


def sende_mail(ort_name: str, html: str, diagramm_pfad: str, modelltemp_pfad: str,
                taupunkt_pfad: str, cape_pfad: str, gefuehlt_pfad: str,
                trend_pfad: str = None, radar_pfad: str = None, betreff_praefix: str = ""):
    msg = MIMEMultipart("related")
    zeitstempel = datetime.now().strftime('%d.%m.%Y %H:%M')
    msg["Subject"] = f"{betreff_praefix}Wetter-Update {ort_name} - {zeitstempel}"
    msg["From"] = GMAIL_ADRESSE
    msg["To"] = EMPFAENGER
    msg.attach(MIMEText(html, "html", "utf-8"))

    bilder = [
        (diagramm_pfad, "diagramm"),
        (modelltemp_pfad, "modelltemp"),
        (taupunkt_pfad, "taupunkt"),
        (cape_pfad, "cape"),
        (gefuehlt_pfad, "gefuehlt"),
    ]
    for pfad, cid in bilder:
        with open(pfad, "rb") as f:
            bild = MIMEImage(f.read())
            bild.add_header("Content-ID", f"<{cid}>")
            msg.attach(bild)

    optionale_bilder = [(trend_pfad, "trend"), (radar_pfad, "radar")]
    for pfad, cid in optionale_bilder:
        if pfad and os.path.exists(pfad):
            with open(pfad, "rb") as f:
                bild = MIMEImage(f.read())
                bild.add_header("Content-ID", f"<{cid}>")
                msg.attach(bild)

    kontext = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=kontext)
        server.login(GMAIL_ADRESSE, GMAIL_APP_PASSWORT)
        server.sendmail(GMAIL_ADRESSE, EMPFAENGER, msg.as_string())


def berechne_betreff_praefix(daily: dict, warnungen: list) -> str:
    """
    Auffälliger Betreff-Prefix bei aktiven DWD-Warnungen oder Extremwerten.
    Hitze/Frost wird bewusst nur für heute + morgen geprüft (nicht die ganze
    7-Tage-Woche), da die Mail mehrmals täglich verschickt wird und der
    Betreff so nah am aktuellen/nächsten Tag bleiben soll.
    """
    if warnungen:
        hoechste_stufe = max(w["level"] for w in warnungen)
        if hoechste_stufe >= 3:
            return "🔴 UNWETTERWARNUNG - "
        return "🟠 Wetterwarnung - "

    max_werte = [v for v in daily.get("temperature_2m_max", [])[:2] if v is not None]
    min_werte = [v for v in daily.get("temperature_2m_min", [])[:2] if v is not None]
    if max_werte and max(max_werte) >= 35:
        return "🌡️ Extreme Hitze - "
    if min_werte and min(min_werte) < 0:
        return "❄️ Frost - "
    return ""


def main():
    try:
        lat, lon, ort_name = geocode(ORT)
        daten = hole_wetterdaten(lat, lon)
        vergleich = hole_modellvergleich(lat, lon)
        modellvergleich_html = baue_modellvergleich_tabelle(vergleich)

        tmp = tempfile.gettempdir()
        diagramm_pfad = os.path.join(tmp, "wetter_diagramm.png")
        modelltemp_pfad = os.path.join(tmp, "wetter_modelltemp.png")
        taupunkt_pfad = os.path.join(tmp, "wetter_taupunkt.png")
        cape_pfad = os.path.join(tmp, "wetter_cape.png")
        gefuehlt_pfad = os.path.join(tmp, "wetter_gefuehlt.png")
        trend_pfad = os.path.join(tmp, "wetter_trend.png")
        radar_pfad = os.path.join(tmp, "wetter_radar.png")

        erstelle_diagramm(daten, diagramm_pfad)
        erstelle_modelltemperaturdiagramm(vergleich, modelltemp_pfad)
        erstelle_taupunktdiagramm(daten, taupunkt_pfad)
        erstelle_cape_diagramm(daten, cape_pfad)
        erstelle_gefuehlte_temp_diagramm(daten, gefuehlt_pfad)

        # Die folgenden Zusatz-Features sind optional: schlägt eine dieser
        # externen APIs mal fehl, soll die restliche Mail trotzdem
        # verschickt werden.
        hat_trend = False
        try:
            saison_daten = hole_langfristtrend(lat, lon)
            erstelle_trenddiagramm(saison_daten, trend_pfad)
            hat_trend = True
        except Exception as e:
            print(f"Hinweis: Langfrist-Trend konnte nicht geladen werden: {e}", file=sys.stderr)

        hat_radar = False
        try:
            lade_regenradar(lat, lon, radar_pfad)
            hat_radar = True
        except Exception as e:
            print(f"Hinweis: Regenradar konnte nicht geladen werden: {e}", file=sys.stderr)

        warnungen = []
        try:
            warnungen = hole_dwd_warnungen(lat, lon)
        except Exception as e:
            print(f"Hinweis: DWD-Warnungen konnten nicht geladen werden: {e}", file=sys.stderr)

        klimavergleich = None
        try:
            klimavergleich = hole_klimavergleich(
                lat, lon,
                daten["daily"]["temperature_2m_max"][0],
                daten["daily"]["temperature_2m_min"][0],
            )
        except Exception as e:
            print(f"Hinweis: Klimavergleich konnte nicht geladen werden: {e}", file=sys.stderr)

        alte_vorhersage = None
        try:
            alte_vorhersage = lade_letzte_vorhersage()
        except Exception as e:
            print(f"Hinweis: Vorhersage-Historie konnte nicht geladen werden: {e}", file=sys.stderr)

        betreff_praefix = berechne_betreff_praefix(daten["daily"], warnungen)
        html = baue_html(ort_name, daten, vergleich, modellvergleich_html, hat_trend, warnungen,
                          klimavergleich, hat_radar, alte_vorhersage)
        sende_mail(ort_name, html, diagramm_pfad, modelltemp_pfad, taupunkt_pfad, cape_pfad,
                   gefuehlt_pfad, trend_pfad if hat_trend else None,
                   radar_pfad if hat_radar else None, betreff_praefix)
        print(f"Wetter-Mail für {ort_name} erfolgreich versendet.")

        try:
            telegram_bilder = [
                (diagramm_pfad, "Verlauf nächste 24h"),
                (modelltemp_pfad, "7-Tage-Temperaturverlauf (Modellvergleich)"),
                (gefuehlt_pfad, "Gefühlte Temperatur - 7-Tage-Verlauf"),
                (taupunkt_pfad, "Taupunktverlauf"),
                (cape_pfad, "CAPE - Gewitterpotential (48h)"),
            ]
            if hat_radar:
                telegram_bilder.append((radar_pfad, "Regenradar"))
            if hat_trend:
                telegram_bilder.append((trend_pfad, "Langfrist-Trend (ECMWF EC46)"))
            sende_telegram(ort_name, daten, vergleich, warnungen, klimavergleich,
                            alte_vorhersage, betreff_praefix, telegram_bilder)
        except Exception as e:
            print(f"Hinweis: Telegram-Nachricht konnte nicht gesendet werden: {e}", file=sys.stderr)

        try:
            speichere_vorhersage(daten)
        except Exception as e:
            print(f"Hinweis: Vorhersage-Historie konnte nicht gespeichert werden: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Fehler beim Versenden der Wetter-Mail: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
