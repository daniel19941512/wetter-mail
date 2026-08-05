# Wetter-Mail – Einrichtung

Verschickt 2x täglich eine E-Mail mit aktuellem Wetter, einer Karte und einem
24h-Diagramm für einen festen Ort. Läuft auf deinem PC im Dauerbetrieb.

## 1. Python installieren (falls noch nicht vorhanden)

Python 3.9+ von https://www.python.org/downloads/ (bei der Installation unter
Windows unbedingt "Add Python to PATH" anhaken).

## 2. Ordner vorbereiten

Diesen Ordner (`wettermail/`) irgendwohin kopieren, z. B. nach
`C:\Wettermail` (Windows) oder `~/wettermail` (Linux/Mac).

Dann im Terminal / der Kommandozeile in den Ordner wechseln und installieren:

```
pip install -r requirements.txt
```

## 3. Gmail App-Passwort erstellen

Gmail lässt normale Passwörter für den Mail-Versand per Skript nicht mehr zu.
Du brauchst ein **App-Passwort**:

1. 2-Faktor-Authentifizierung für dein Google-Konto aktivieren (falls noch
   nicht geschehen): https://myaccount.google.com/security
2. App-Passwörter erstellen: https://myaccount.google.com/apppasswords
3. Als App z. B. "Mail", als Gerät "Sonstiges" mit Namen "Wettermail" wählen
4. Das erzeugte 16-stellige Passwort kopieren (nicht dein normales
   Gmail-Passwort!)

## 4. Skript konfigurieren

`wetter_mail.py` öffnen und im Konfigurationsblock ganz oben anpassen:

```python
ORT = "Berlin"                                   # dein Ort
GMAIL_ADRESSE = "deine.adresse@gmail.com"        # Absender-Gmail-Adresse
GMAIL_APP_PASSWORT = "abcd efgh ijkl mnop"       # das App-Passwort aus Schritt 3
EMPFAENGER = "empfaenger@example.com"            # wohin die Mail gehen soll
```

## 5. Testen

```
python wetter_mail.py
```

Bei Erfolg erscheint "Wetter-Mail für ... erfolgreich versendet." und die
Mail sollte im Postfach ankommen (ggf. auch im Spam-Ordner nachsehen).

## 6. Automatisch 2x täglich ausführen

Zwei Möglichkeiten: **A) GitHub Actions** – läuft in der Cloud, dein PC muss
nicht an sein (empfohlen, wenn du nicht immer Zugriff auf deinen Rechner hast).
**B) eigener PC** – Task Scheduler/Cron, der PC muss zu den Zeiten laufen.

### A) GitHub Actions (kein eigener Rechner nötig, kostenlos)

1. Kostenlosen Account auf https://github.com erstellen (falls noch nicht
   vorhanden)
2. Neues **privates** Repository erstellen (z. B. "wettermail")
3. Alle Dateien aus diesem Ordner (inkl. des Unterordners `.github/`) in das
   Repository hochladen – entweder per Weboberfläche ("Add file" → "Upload
   files", Ordnerstruktur bleibt beim Ziehen&Ablegen erhalten) oder per Git:
   ```
   git init
   git add .
   git commit -m "Wetter-Mail"
   git branch -M main
   git remote add origin https://github.com/<dein-name>/wettermail.git
   git push -u origin main
   ```
4. Im Repository zu **Settings → Secrets and variables → Actions** gehen und
   auf "New repository secret" die folgenden vier Secrets anlegen:
   - `WETTER_ORT` → z. B. `Berlin`
   - `GMAIL_ADRESSE` → deine Gmail-Adresse
   - `GMAIL_APP_PASSWORT` → das App-Passwort aus Schritt 3
   - `WETTER_EMPFAENGER` → Empfänger-Adresse
5. Fertig. Der Workflow (`.github/workflows/wetter-mail.yml`) läuft danach
   automatisch 2x täglich (Standard: 6:00 und 17:00 UTC – Zeiten in der
   Workflow-Datei nach Bedarf anpassen, die Uhrzeiten sind in UTC, nicht in
   deutscher Zeit)
6. Manuell testen: im Repository auf **Actions → Wetter-Mail → Run workflow**
   klicken

Die Werte in `wetter_mail.py` selbst (Schritt 4 oben) müssen für diesen Weg
**nicht** angepasst werden – das Skript liest sie automatisch aus den Secrets.

### B) Eigener Rechner

#### Windows (Task Scheduler / Aufgabenplanung)

1. "Aufgabenplanung" öffnen (Windows-Suche → "Aufgabenplanung")
2. Rechts auf "Aufgabe erstellen..." klicken
3. **Allgemein**: Name "Wettermail", Haken bei "Unabhängig von der
   Benutzeranmeldung ausführen"
4. **Trigger**: Neu → Täglich, Startzeit z. B. 07:00 Uhr → OK
   Diesen Schritt wiederholen für eine zweite Zeit, z. B. 18:00 Uhr
5. **Aktionen**: Neu → Programm/Skript: Pfad zu `python.exe`
   (z. B. `C:\Users\<Name>\AppData\Local\Programs\Python\Python312\python.exe`)
   Argumente hinzufügen: `wetter_mail.py`
   Starten in: `C:\Wettermail` (der Ordnerpfad aus Schritt 2)
6. Mit OK speichern, ggf. Windows-Passwort eingeben

#### Linux / Mac (Cron)

Im Terminal:

```
crontab -e
```

Folgende Zeilen einfügen (Pfade an dein System anpassen):

```
0 7  * * * /usr/bin/python3 /home/nutzer/wettermail/wetter_mail.py
0 18 * * * /usr/bin/python3 /home/nutzer/wettermail/wetter_mail.py
```

Das schickt die Mail täglich um 7:00 und 18:00 Uhr.

## Hinweise

- Der PC muss zu den geplanten Zeiten laufen und online sein.
- Alle genutzten Dienste (Open-Meteo, OpenStreetMap Static Map) sind
  kostenlos und benötigen keinen API-Key.
- Ort ändern: einfach `ORT` in `wetter_mail.py` anpassen, z. B. auf
  "München", "Hamburg, Deutschland" o. ä.
