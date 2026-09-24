# SimsonRunner

Ein konfigurierbarer Markt-Scanner für Simson S51, Schwalbe KR51/1 und KR51/2 im Umkreis von 100 km um 38533. Er erkennt neue Angebote, bewertet Anbieterangaben, versendet einen HTML-Bericht und veröffentlicht die Markthistorie als statisches Dashboard.

> Die Bewertung gibt ausschließlich Formulierungen aus Anzeigen wieder. Papiere, Fahrgestellnummer, Eigentum, technischer Zustand und rechtliche Zulässigkeit müssen vor einem Kauf geprüft werden.

## Lokal ausführen

Voraussetzung ist Python 3.11 oder neuer. Zusätzliche Pakete werden nicht benötigt.

```bash
python -m unittest discover -v
python -m scanner.scanner --max-pages 1
```

Ohne SMTP-Zugangsdaten entsteht ausschließlich `scanner/preview.html`. Seen-State, Favoriten, Datenbank und Dashboard bleiben dabei unverändert. `DEBUG=1` speichert zusätzlich die abgerufenen Ergebnisseiten unter `scanner/debug_*.html`.

## GitHub konfigurieren

Unter **Settings → Secrets and variables → Actions** werden diese Repository Secrets benötigt:

- `GMAIL_USER`: Gmail-Absenderadresse
- `GMAIL_APP_PASSWORD`: 16-stelliges Google-App-Passwort
- `NOTIFY_EMAIL`: Empfängeradresse

Als Repository Variable wird `MARKET_DASHBOARD_URL`, beispielsweise `https://andilar.github.io/simmenscanner/`, angelegt. Das normale Gmail-Passwort gehört nicht in GitHub. Ein App-Passwort lässt sich im Google-Konto nach Aktivierung der Zwei-Faktor-Anmeldung erzeugen.

Unter **Settings → Actions → General → Workflow permissions** ist **Read and write permissions** zu aktivieren. Unter **Settings → Pages** wird **GitHub Actions** als Quelle gewählt. Der Workflow läuft täglich um 07:00 UTC und kann manuell gestartet werden.

## Anpassungen

Such-URLs, Standort, Preisbereiche, Ausschlussbegriffe und Branding liegen in `scanner/config.py`. Der Zeitplan steht zusätzlich im Workflow, weil GitHub den Cron-Ausdruck nicht aus Python-Konfiguration lesen kann. Das Dashboard-Layout liegt in `scanner/dashboard_template.html`.

