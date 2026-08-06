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
from datetime import datetime, timedelta
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
                  "dew_point_2m,surface_pressure",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "sunrise,sunset,weathercode,uv_index_max,sunshine_duration",
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


def baue_modellvergleich_tabelle(vergleich: dict) -> str:
    """HTML-Tabelle: 7-Tage-Vorhersage im Modellvergleich (Max/Min-Temp, Niederschlag)."""
    daily = vergleich["daily"]
    tage = daily["time"]

    zeilen = []
    kopf = "<tr><th>Datum</th>"
    for name in MODELLE:
        kopf += f"<th colspan='2'>{name}</th>"
    kopf += "<th colspan='2'>Mittel aller Modelle</th><th>Niederschlag Ø</th></tr>"
    zeilen.append(kopf)
    zeilen.append(
        "<tr><td></td>" + "<th>Max</th><th>Min (Nacht)</th>" * len(MODELLE)
        + "<th>Max</th><th>Min (Nacht)</th><td></td></tr>"
    )

    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%a %d.%m.")
        zeile = f"<tr><td><b>{datum}</b></td>"
        niederschlag_werte, max_werte, min_werte = [], [], []
        for suffix in MODELLE.values():
            max_key = f"temperature_2m_max_{suffix}"
            min_key = f"temperature_2m_min_{suffix}"
            regen_key = f"precipitation_sum_{suffix}"
            max_t = daily.get(max_key, [None] * len(tage))[i]
            min_t = daily.get(min_key, [None] * len(tage))[i]
            regen = daily.get(regen_key, [None] * len(tage))[i]
            if regen is not None:
                niederschlag_werte.append(regen)
            if max_t is not None:
                max_werte.append(max_t)
            if min_t is not None:
                min_werte.append(min_t)
            max_txt = f"{max_t:.0f}°" if max_t is not None else "-"
            min_txt = f"{min_t:.0f}°" if min_t is not None else "-"
            # Kälteste Nächte (< 5°C) hervorheben
            min_style = ' style="color:#1a5fb4; font-weight:bold;"' if (min_t is not None and min_t < 5) else ""
            zeile += f"<td>{max_txt}</td><td{min_style}>{min_txt}</td>"

        mittel_max = f"{sum(max_werte)/len(max_werte):.0f}°" if max_werte else "-"
        mittel_min = f"{sum(min_werte)/len(min_werte):.0f}°" if min_werte else "-"
        zeile += f"<td><b>{mittel_max}</b></td><td><b>{mittel_min}</b></td>"

        regen_avg = f"{sum(niederschlag_werte)/len(niederschlag_werte):.1f} mm" if niederschlag_werte else "-"
        zeile += f"<td>{regen_avg}</td></tr>"
        zeilen.append(zeile)

    return (
        "<table cellpadding='4' style='border-collapse:collapse; font-size:13px;' border='1'>"
        + "".join(zeilen) + "</table>"
        + "<p style='color:#888; font-size:11px;'>Min-Werte unter 5°C sind hervorgehoben "
          "(relevant für Nachtfrost-Risiko). 'Mittel aller Modelle' und Niederschlag Ø = "
          "Durchschnitt über alle vier Modelle.</p>"
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


def baue_html(ort_name: str, daten: dict, modellvergleich_html: str, hat_trend: bool,
              warnungen: list, klimavergleich: dict) -> str:
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

    trend_block = ""
    if hat_trend:
        trend_block = """
      <h3>Langfrist-Trend (ECMWF EC46, bis 46 Tage)</h3>
      <img src="cid:trend" width="600">
      <p style="color:#888; font-size:11px;">
        NOAA CFS ist über die kostenlose Open-Meteo-API nicht verfügbar. Diese Ansicht nutzt
        stattdessen ECMWF EC46, das vergleichbare europäische Langfrist-Modell. Nicht
        bias-korrigiert - als grobe Tendenz zu verstehen, nicht als Tagesvorhersage.
      </p>"""

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; color:#222;">
      <h2>Wetter für {ort_name}</h2>
      <p style="font-size:15px;">{klartext}</p>
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
      <h3>Verlauf nächste 24h</h3>
      <img src="cid:diagramm" width="600">
      <h3>7-Tage-Modellvergleich (GFS / ECMWF / AIFS / ICON)</h3>
      {modellvergleich_html}
      <h3>7-Tage-Temperaturverlauf im Modellvergleich</h3>
      <img src="cid:modelltemp" width="600">
      <h3>Taupunktverlauf</h3>
      <img src="cid:taupunkt" width="600">
      {trend_block}
      <p style="color:#888; font-size:12px;">
        Automatisch erstellt am {datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')}.
        Datenquelle: Open-Meteo, DWD.
      </p>
    </body>
    </html>
    """


def sende_mail(ort_name: str, html: str, diagramm_pfad: str, modelltemp_pfad: str,
                taupunkt_pfad: str, trend_pfad: str = None, betreff_praefix: str = ""):
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
    ]
    for pfad, cid in bilder:
        with open(pfad, "rb") as f:
            bild = MIMEImage(f.read())
            bild.add_header("Content-ID", f"<{cid}>")
            msg.attach(bild)

    if trend_pfad and os.path.exists(trend_pfad):
        with open(trend_pfad, "rb") as f:
            bild = MIMEImage(f.read())
            bild.add_header("Content-ID", "<trend>")
            msg.attach(bild)

    kontext = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=kontext)
        server.login(GMAIL_ADRESSE, GMAIL_APP_PASSWORT)
        server.sendmail(GMAIL_ADRESSE, EMPFAENGER, msg.as_string())


def berechne_betreff_praefix(daily: dict, warnungen: list) -> str:
    """Auffälliger Betreff-Prefix, wenn diese Woche Extremwerte oder aktive DWD-Warnungen vorliegen."""
    if warnungen:
        hoechste_stufe = max(w["level"] for w in warnungen)
        if hoechste_stufe >= 3:
            return "🔴 UNWETTERWARNUNG - "
        return "🟠 Wetterwarnung - "

    max_werte = [v for v in daily.get("temperature_2m_max", []) if v is not None]
    min_werte = [v for v in daily.get("temperature_2m_min", []) if v is not None]
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
        trend_pfad = os.path.join(tmp, "wetter_trend.png")

        erstelle_diagramm(daten, diagramm_pfad)
        erstelle_modelltemperaturdiagramm(vergleich, modelltemp_pfad)
        erstelle_taupunktdiagramm(daten, taupunkt_pfad)

        # Die folgenden drei Zusatz-Features sind optional: schlägt eine
        # dieser APIs mal fehl, soll die restliche Mail trotzdem verschickt
        # werden.
        hat_trend = False
        try:
            saison_daten = hole_langfristtrend(lat, lon)
            erstelle_trenddiagramm(saison_daten, trend_pfad)
            hat_trend = True
        except Exception as e:
            print(f"Hinweis: Langfrist-Trend konnte nicht geladen werden: {e}", file=sys.stderr)

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

        betreff_praefix = berechne_betreff_praefix(daten["daily"], warnungen)
        html = baue_html(ort_name, daten, modellvergleich_html, hat_trend, warnungen, klimavergleich)
        sende_mail(ort_name, html, diagramm_pfad, modelltemp_pfad, taupunkt_pfad,
                   trend_pfad if hat_trend else None, betreff_praefix)
        print(f"Wetter-Mail für {ort_name} erfolgreich versendet.")
    except Exception as e:
        print(f"Fehler beim Versenden der Wetter-Mail: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
