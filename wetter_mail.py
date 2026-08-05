#!/usr/bin/env python3
"""
Wetter-Mail
-----------
Holt aktuelle Wetterdaten (inkl. Taupunkt), eine Wetterkarte, einen
Modellvergleich (GFS/ECMWF/AIFS/ICON) für 7 Tage sowie einen Langfrist-Trend
und verschickt alles per Gmail als HTML-Mail. Gedacht für den Aufruf
3x täglich über GitHub Actions, Task Scheduler oder Cron.

Genutzte APIs (alle kostenlos, kein API-Key nötig):
- Open-Meteo Geocoding + Forecast API + Seasonal API: https://open-meteo.com
- OpenStreetMap-Kacheln: https://tile.openstreetmap.org

Hinweis Langfrist-Trend: NOAA CFS wird von Open-Meteo nicht angeboten.
Stattdessen wird ECMWF EC46 (bis 46 Tage) / SEAS5 (bis 9 Monate) genutzt,
die vergleichbaren europäischen Langfrist-Modelle.
"""

import smtplib
import ssl
import sys
import tempfile
import os
import math
import io
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import requests
from PIL import Image, ImageDraw
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
    """Aktuelle Werte (inkl. Taupunkt) + stündliche Vorhersage (48h) über Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "dew_point_2m,precipitation,weathercode,windspeed_10m",
        "hourly": "temperature_2m,precipitation,precipitation_probability,dew_point_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "sunrise,sunset,weathercode",
        "forecast_days": 3,
        "timezone": "auto",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


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
    kopf += "<th>Niederschlag Ø</th></tr>"
    zeilen.append(kopf)
    zeilen.append(
        "<tr><td></td>" + "<th>Max</th><th>Min (Nacht)</th>" * len(MODELLE) + "<td></td></tr>"
    )

    for i, tag in enumerate(tage):
        datum = datetime.fromisoformat(tag).strftime("%a %d.%m.")
        zeile = f"<tr><td><b>{datum}</b></td>"
        niederschlag_werte = []
        for suffix in MODELLE.values():
            max_key = f"temperature_2m_max_{suffix}"
            min_key = f"temperature_2m_min_{suffix}"
            regen_key = f"precipitation_sum_{suffix}"
            max_t = daily.get(max_key, [None] * len(tage))[i]
            min_t = daily.get(min_key, [None] * len(tage))[i]
            regen = daily.get(regen_key, [None] * len(tage))[i]
            if regen is not None:
                niederschlag_werte.append(regen)
            max_txt = f"{max_t:.0f}°" if max_t is not None else "-"
            min_txt = f"{min_t:.0f}°" if min_t is not None else "-"
            # Kälteste Nächte (< 5°C) hervorheben
            min_style = ' style="color:#1a5fb4; font-weight:bold;"' if (min_t is not None and min_t < 5) else ""
            zeile += f"<td>{max_txt}</td><td{min_style}>{min_txt}</td>"
        regen_avg = f"{sum(niederschlag_werte)/len(niederschlag_werte):.1f} mm" if niederschlag_werte else "-"
        zeile += f"<td>{regen_avg}</td></tr>"
        zeilen.append(zeile)

    return (
        "<table cellpadding='4' style='border-collapse:collapse; font-size:13px;' border='1'>"
        + "".join(zeilen) + "</table>"
        + "<p style='color:#888; font-size:11px;'>Min-Werte unter 5°C sind hervorgehoben "
          "(relevant für Nachtfrost-Risiko). Niederschlag Ø = Mittelwert über alle vier Modelle.</p>"
    )


def lade_karte(lat: float, lon: float, pfad: str, zoom: int = 10):
    """
    Baut eine Karte direkt aus offiziellen OpenStreetMap-Kacheln zusammen
    (3x3-Kachelraster um den Standort). Braucht keinen API-Key und ist
    unabhängig von Drittanbieter-Static-Map-Diensten, die häufig instabil
    sind oder abgeschaltet werden.
    """
    def latlon_zu_kachel(lat, lon, zoom):
        lat_rad = math.radians(lat)
        n = 2.0 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
        return x, y

    tile_groesse = 256
    raster = 3  # 3x3 Kacheln
    mitte = raster // 2
    x0, y0 = latlon_zu_kachel(lat, lon, zoom)

    gesamt = Image.new("RGB", (tile_groesse * raster, tile_groesse * raster))
    headers = {"User-Agent": "WetterMail/1.0 (privates Automatisierungsskript)"}

    for dx in range(-mitte, mitte + 1):
        for dy in range(-mitte, mitte + 1):
            x, y = x0 + dx, y0 + dy
            url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            kachel = Image.open(io.BytesIO(r.content))
            gesamt.paste(kachel, ((dx + mitte) * tile_groesse, (dy + mitte) * tile_groesse))

    # Roten Punkt als Markierung für den Standort in die Bildmitte zeichnen
    zeichner = ImageDraw.Draw(gesamt)
    cx, cy = gesamt.width // 2, gesamt.height // 2
    radius = 8
    zeichner.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                      fill="red", outline="white", width=2)

    gesamt.save(pfad)


def baue_html(ort_name: str, daten: dict, modellvergleich_html: str, hat_trend: bool) -> str:
    cur = daten["current"]
    daily = daten["daily"]
    heute_max = daily["temperature_2m_max"][0]
    heute_min = daily["temperature_2m_min"][0]
    sonnenaufgang = daily["sunrise"][0].split("T")[1]
    sonnenuntergang = daily["sunset"][0].split("T")[1]

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
      <p><b>{wettercode_text(cur['weathercode'])}</b></p>
      <table cellpadding="4">
        <tr><td>Aktuelle Temperatur:</td><td><b>{cur['temperature_2m']} °C</b>
            (gefühlt {cur['apparent_temperature']} °C)</td></tr>
        <tr><td>Heute Min/Max:</td><td>{heute_min} °C / {heute_max} °C</td></tr>
        <tr><td>Taupunkt:</td><td>{cur['dew_point_2m']} °C</td></tr>
        <tr><td>Luftfeuchtigkeit:</td><td>{cur['relative_humidity_2m']} %</td></tr>
        <tr><td>Wind:</td><td>{cur['windspeed_10m']} km/h</td></tr>
        <tr><td>Niederschlag aktuell:</td><td>{cur['precipitation']} mm</td></tr>
        <tr><td>Sonnenaufgang / -untergang:</td><td>{sonnenaufgang} / {sonnenuntergang}</td></tr>
      </table>
      <h3>Karte</h3>
      <img src="cid:karte" width="600"><br><br>
      <h3>Verlauf nächste 24h</h3>
      <img src="cid:diagramm" width="600">
      <h3>7-Tage-Modellvergleich (GFS / ECMWF / AIFS / ICON)</h3>
      {modellvergleich_html}
      {trend_block}
      <p style="color:#888; font-size:12px;">
        Automatisch erstellt am {datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')}.
        Datenquelle: Open-Meteo.
      </p>
    </body>
    </html>
    """


def sende_mail(ort_name: str, html: str, karte_pfad: str, diagramm_pfad: str, trend_pfad: str = None):
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


def main():
    try:
        lat, lon, ort_name = geocode(ORT)
        daten = hole_wetterdaten(lat, lon)
        vergleich = hole_modellvergleich(lat, lon)
        modellvergleich_html = baue_modellvergleich_tabelle(vergleich)

        tmp = tempfile.gettempdir()
        karte_pfad = os.path.join(tmp, "wetter_karte.png")
        diagramm_pfad = os.path.join(tmp, "wetter_diagramm.png")
        trend_pfad = os.path.join(tmp, "wetter_trend.png")

        lade_karte(lat, lon, karte_pfad)
        erstelle_diagramm(daten, diagramm_pfad)

        # Langfrist-Trend ist optional: schlägt diese API mal fehl, soll die
        # restliche Mail trotzdem verschickt werden.
        hat_trend = False
        try:
            saison_daten = hole_langfristtrend(lat, lon)
            erstelle_trenddiagramm(saison_daten, trend_pfad)
            hat_trend = True
        except Exception as e:
            print(f"Hinweis: Langfrist-Trend konnte nicht geladen werden: {e}", file=sys.stderr)

        html = baue_html(ort_name, daten, modellvergleich_html, hat_trend)
        sende_mail(ort_name, html, karte_pfad, diagramm_pfad,
                   trend_pfad if hat_trend else None)
        print(f"Wetter-Mail für {ort_name} erfolgreich versendet.")
    except Exception as e:
        print(f"Fehler beim Versenden der Wetter-Mail: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
