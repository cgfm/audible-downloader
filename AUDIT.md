# Funktions- und Sicherheitsprüfung

Stand: 12. September 2026

## Management Summary

Der Kernfunktionsumfang ist für einen Single-User-Downloader bereits gut
ausgebaut. Dazu gehören Bibliothekssynchronisierung, eine persistente Queue,
Fortschrittsanzeige, Multipart-Zusammenführung, Lautstärkenormalisierung,
konfigurierbare Ausgabepfade, das Verknüpfen vorhandener Dateien und die
browsergestützte Audible-Einrichtung.

Der aktuelle Stand sollte dennoch nicht produktiv freigegeben werden. Vor einer
Freigabe müssen insbesondere die Offenlegung von Audible-Schlüsseln, bekannte
Schwachstellen im Container, die standardmäßig ungeschützte Weboberfläche und
zwei Risiken beim Überschreiben vorhandener M4B-Dateien behoben werden.

## Release-Blocker

### 1. Audible-Schlüssel können in Logs, SQLite und Weboberfläche gelangen

**Priorität:** P0
**Schweregrad:** hoch

Aktivierungsbytes sowie AAXC-Key und IV werden als FFmpeg-Argumente übergeben:

- `app/audible_service.py:1934` (`_activation_bytes`)
- `app/audible_service.py:2090` (`_ffmpeg_input_options`)

Bei einem Fehler enthält `subprocess.CalledProcessError` die vollständige
Kommandozeile. Der aktuelle Fehlerpfad übernimmt `str(exc)` und schreibt den
Wert in `jobs.error_message` und `processing_runs.error_message`, in das
Anwendungslog und anschließend in die Job- und Buchansicht:

- `app/audible_service.py:1832` (`_finish_job`)
- `app/audible_service.py:2163` (`_run_ffmpeg_with_progress`)
- `app/audible_service.py:3082` und `app/audible_service.py:3297`
- `app/templates/jobs.html:24`
- `app/templates/convert.html:22`

Zusätzlich gibt die installierte `audible-cli`-Implementierung von
`activation-bytes` den Wert selbst auf stdout aus. Der Aufruf in
`_activation_bytes` übernimmt stdout unverändert in die Container-Logs. Dieses
Verhalten wurde im gebauten Image verifiziert.

**Erforderliche Maßnahmen:**

- Eigene, bereinigte Subprozessfehler erzeugen und niemals die vollständige
  geheime Kommandozeile persistieren.
- Aktivierungsbytes, AAXC-Key, IV, Authorization-Codes und Token zentral
  redigieren.
- stdout und stderr von `audible activation-bytes` abfangen.
- Fehlertexte für UI und Datenbank von internen Diagnoseinformationen trennen.
- Regressionstests mit Marker-Geheimnissen für Log, Datenbank und Webantwort
  ergänzen.

### 2. Veraltetes und verwundbares Container-Basisimage

**Priorität:** P0
**Schweregrad:** hoch

`Dockerfile:1` verwendet `python:alpine3.20`. Das gebaute Image enthält Alpine
3.20.6. Alpine 3.20 ist laut offizieller Supportmatrix seit dem 1. April 2026
außerhalb des regulären Supports:

<https://www.alpinelinux.org/releases/>

Ein aktueller Trivy-Scan des tatsächlich gebauten Images mit
`--severity HIGH,CRITICAL --ignore-unfixed` ergab:

- 18 High-/Critical-Pakettreffer
- 10 eindeutige CVEs
- 16 High- und 2 Critical-Einträge
- betroffene Pakete unter anderem: OpenSSL, musl, SQLite und zlib
- für alle gemeldeten Treffer existieren neuere Paketversionen

Die beiden Critical-Einträge gehören zur selben als 32-Bit-spezifisch
beschriebenen OpenSSL-Lücke; das geprüfte Image ist amd64. Die übrigen
High-Befunde bleiben relevant.

**Erforderliche Maßnahmen:**

- Auf einen unterstützten Alpine-Zweig wechseln.
- Das Basisimage kontrolliert aktualisieren und reproduzierbar referenzieren.
- Betriebssystempakete im Release-Build tatsächlich aktualisieren.
- Image- und Dependency-Scans als CI-Gate einführen.
- Regelmäßige, cache-unabhängige Rebuilds vorsehen.

### 3. Weboberfläche ist standardmäßig ungeschützt und global gebunden

**Priorität:** P0
**Schweregrad:** hoch, sofern der Port erreichbar ist

`WEB_ADMIN_PASSWORD` ist in `app/web.py:38` standardmäßig leer. Gleichzeitig
bindet der Server in `app/web.py:443` standardmäßig an `0.0.0.0`. Bei einer
versehentlichen Portfreigabe kann ein nicht authentifizierter Benutzer:

- Bibliotheksmetadaten, Fehler und Dateipfade lesen,
- Downloads und rechenintensive Normalisierungen starten,
- vorhandene M4B-Dateien mit bestätigtem Overwrite überschreiben,
- Speicherplatz, Netzwerk und CPU erschöpfen,
- eine noch nicht konfigurierte Instanz mit einem Audible-Konto verbinden.

Die Dokumentation empfiehlt zwar ein Passwort oder einen Reverse Proxy, die
Anwendung erzwingt aber keine sichere Betriebsart.

**Erforderliche Maßnahmen:**

- Fail-closed starten, wenn ohne lokales Passwort an eine nicht lokale Adresse
  gebunden wird.
- Für Reverse-Proxy-Authentifizierung einen expliziten, dokumentierten Modus
  verlangen.
- Passwortdateien beziehungsweise Docker Secrets unterstützen.
- Rate-Limits und eine maximale Request-Größe einführen.
- Sensible Seiten generell mit `Cache-Control: no-store` ausliefern.

### 4. Zielpfade sind nicht exklusiv einem Hörbuch zugeordnet

**Priorität:** P0
**Schweregrad:** hoch

Die Datenbank erzwingt nur `UNIQUE(run_id, relative_path)` in
`app/audible_service.py:735`. Vor einer Konvertierung wird nicht geprüft, ob der
Zielpfad bereits in einem erfolgreichen Lauf einem anderen ASIN zugeordnet ist.

Das Problem wurde reproduziert: Eine Datei ließ sich erfolgreich mit Buch A
verknüpfen; anschließend akzeptierte `enqueue_conversion` für Buch B denselben
Pfad mit `overwrite=True` ohne Konflikt.

Dadurch können mehrere Bücher dieselbe physische Datei beanspruchen oder eine
korrekte Datei eines anderen Buchs überschrieben werden.

**Erforderliche Maßnahmen:**

- Eine zentrale Zielpfad-Eigentümerschaft einführen.
- Konflikte bereits beim Einreihen und erneut direkt vor der Veröffentlichung
  prüfen.
- Ein Überschreiben fremd zugeordneter Dateien standardmäßig verbieten.
- Für bewusste Neuzuordnungen einen getrennten, deutlich bestätigten Workflow
  anbieten.
- Automatische Pfadkollisionen beispielsweise durch einen ASIN-Suffix auflösen.

### 5. Einzeldateikonvertierungen werden nicht atomar veröffentlicht

**Priorität:** P0
**Schweregrad:** hoch

Die normale Einzeldateikonvertierung schreibt in
`app/audible_service.py:2744` direkt in das endgültige Ziel. Im Gegensatz zur
Normalisierung und zum Multipart-Merge gibt es keine temporäre Datei mit
anschließender atomarer Veröffentlichung.

Bei einem FFmpeg-Abbruch kann deshalb:

- ein vorhandenes Original bei `overwrite=True` beschädigt werden,
- eine unvollständige neue Zieldatei zurückbleiben,
- eine lediglich teilweise valide Datei aufgrund der reinen Größenprüfung als
  Erfolg behandelt werden.

**Erforderliche Maßnahmen:**

- Immer in eine eindeutig benannte temporäre Datei im Zielverzeichnis schreiben.
- Die Ausgabe mit ffprobe auf Audiospur und plausible Dauer prüfen.
- Datei und Zielverzeichnis flushen und erst danach mit `os.replace` publizieren.
- Fehler- und Neustartbereinigung testen.

## Weitere Sicherheits- und Betriebsrisiken

### Flask-Entwicklungsserver im Produktionsimage

**Priorität:** P1
**Schweregrad:** mittel bis hoch

Der Container startet über `Dockerfile:28` direkt `python /app/web.py`, das in
`app/web.py:442` `app.run()` verwendet. Flask weist ausdrücklich darauf hin,
dass der Entwicklungsserver nicht für den Produktivbetrieb ausgelegt ist:

<https://flask.palletsprojects.com/en/stable/deploying/>

Es fehlt ein produktionsfähiger, einzelprozessiger WSGI-Server mit sauberem
Start- und Shutdown-Lifecycle für Scheduler, Queue-Worker und Kindprozesse.

### Authentifizierungsinstallation ist nicht vollständig crash-sicher

**Priorität:** P1
**Schweregrad:** mittel

In `app/audible_service.py:514` wird die neue unverschlüsselte Auth-Datei in
einem privaten Staging-Verzeichnis validiert. Danach werden Auth-Datei und
`config.toml` jedoch nacheinander mit `os.replace` installiert. Ein Abbruch
zwischen beiden Operationen kann ein inkonsistentes Profil hinterlassen.

Nach einem harten Prozessabbruch können außerdem `.audible-auth-*`-Verzeichnisse
mit unverschlüsselten Zugangsdaten unter `/config` verbleiben. Eine gezielte
Startup-Bereinigung fehlt.

Empfohlen ist eine neue, eindeutige Auth-Datei pro Rotation. Erst nachdem diese
vollständig geschrieben und validiert ist, sollte `config.toml` atomar auf sie
verweisen. Alte Dateien können anschließend kontrolliert entfernt werden.

### Aktive Prozesse können nicht abgebrochen werden

**Priorität:** P1
**Schweregrad:** mittel

Der Audible-Download verwendet in `app/audible_service.py:2046` ausdrücklich
`--timeout 0`. Auch FFmpeg- und ffprobe-Prozesse haben keine harte Laufzeitgrenze.
`cancel_job` in `app/audible_service.py:1707` akzeptiert ausschließlich wartende
Jobs. Ein hängender Download oder Medienprozess blockiert den jeweiligen Worker
bis zum Containerneustart.

Benötigt werden Prozessgruppen, kooperatives Cancel, ein Terminate/Kill-Fallback
und getrennte, konfigurierbare Zeitlimits für Netzwerk- und Medienoperationen.

### Dateirechte und beschreibbarer Anwendungscode

**Priorität:** P1
**Schweregrad:** mittel

Positiv ist der Betrieb als numerischer Nicht-Root-Benutzer. Das erzeugte
`audiobooks.db` besitzt jedoch standardmäßig Modus `0644`. `/app` und der darin
enthaltene Anwendungscode gehören ebenfalls dem Runtime-Benutzer und sind für
ihn beschreibbar. Ein kompromittierter Prozess könnte dadurch den laufenden
Container verändern.

Empfohlen sind `umask 077`, eine Migration vorhandener sensibler Dateien auf
`0600`, root-eigener schreibgeschützter Anwendungscode und ein separates
beschreibbares Arbeitsverzeichnis. Ein read-only Root-Filesystem sollte als
optionaler Betriebsmodus dokumentiert werden.

### Nicht-ASCII-Eingaben erzeugen HTTP 500 bei Basic Auth

**Priorität:** P1
**Schweregrad:** niedrig bis mittel

`hmac.compare_digest` wird in `app/web.py:72` direkt mit Strings verwendet.
Nicht-ASCII-Zeichen werden von dieser API nicht unterstützt. Eine entsprechend
kodierte Basic-Auth-Anfrage erzeugt reproduzierbar einen ungefangenen
`TypeError` und HTTP 500. Das ermöglicht zumindest Log-Spam und vermeidbare
Fehlerlast.

Verglichen werden sollten stattdessen Bytes beziehungsweise vorab berechnete
kryptografische Digests. Für Fehlversuche ist ein einheitliches 401-Verhalten
erforderlich.

### Login-URL wird nicht gegen Marketplace-Hosts geprüft

**Priorität:** P2
**Schweregrad:** niedrig bis mittel

`receive_login_url` prüft in `app/audible_service.py:486` lediglich auf HTTPS
und einen vorhandenen Hostnamen. Die Rückgabe-URL wird dagegen gegen eine
Marketplace-Hostliste geprüft. Da die UI den ersten Link als Amazon-Anmeldung
beschriftet, sollte auch dieser Link auf erlaubte Amazon-/Audible-Hosts und
übliche HTTPS-Ports beschränkt werden.

### Abhängigkeiten sind nicht reproduzierbar

**Priorität:** P1
**Schweregrad:** mittel

`audible-cli` ist gepinnt, Flask und transitive Abhängigkeiten dagegen nicht.
Ein frischer Build kann daher ohne Quellcodeänderung andere Pakete enthalten.
Der geprüfte Build enthielt unter anderem Flask 3.1.3 und `pip 25.0.1`.

Ein aktueller `pip-audit` meldete sechs eindeutige Advisories für die enthaltene
pip-Version. pip wird nicht von einem Webendpunkt aufgerufen, daher ist die
direkte Laufzeitexposition gering. Es bleibt ein Build- und Supply-Chain-Risiko.

Erforderlich sind eine Lockdatei beziehungsweise vollständig gepinnte
Produktionsabhängigkeiten, Hashprüfung, ein aktualisiertes oder aus dem
Runtime-Image entferntes pip und automatisierte Dependency-Updates.

### Weitere Härtungspunkte

**Priorität:** P2

- Keine globale maximale Request-Größe.
- Keine Rate-Limits für Basic Auth, Bibliotheksexport und Verbindungstest.
- Bibliotheks-, Buch- und Jobseiten erhalten kein generelles
  `Cache-Control: no-store`.
- `WEB_SECURE_COOKIES` ist standardmäßig deaktiviert.
- Kein HSTS; dieses sollte bei HTTPS vorzugsweise der Reverse Proxy setzen.
- Cover werden direkt von beliebigen HTTPS-Hosts im Browser geladen. Das ist ein
  Datenschutz- und Tracking-Risiko; Proxying, lokales Caching oder eine Hostliste
  wären robuster.
- Konfigurationsverzeichnisse und Arbeitswurzeln werden nicht gegen gefährliche
  Werte wie `/` oder überlappende Verzeichnisse validiert.
- Metadatenbasierte Pfadkomponenten besitzen keine robuste Längenbegrenzung und
  können an Dateisystemlimits scheitern.

## Fehlende Kernfeatures

### MP3-Fallback von audible-cli 0.6.0

**Priorität:** P1

Die installierte `audible-cli`-Version kann für bestimmte AAXC-Inhalte eine
MP3-Datei liefern. `_download_audio` berücksichtigt in
`app/audible_service.py:2079` jedoch ausschließlich `.aax` und `.aaxc`. Das
Ergebnis ist anschließend fälschlich „Audible lieferte keine Audiodatei“.

Die gepinnte Version 0.6.0 war zum Prüfzeitpunkt die aktuelle Veröffentlichung:

<https://pypi.org/project/audible-cli/0.6.0/>

MP3 muss als bereits entschlüsseltes Eingabeformat erkannt, konvertiert und
einschließlich eventuell vorhandener Voucher korrekt bereinigt werden. Die
vorzeitige Aktivierungsbyte-Ermittlung sollte für solche Formate entfallen.

### Zuverlässige Jobsteuerung

**Priorität:** P1

Es fehlen:

- Retry mit exponentiellem Backoff und maximaler Versuchszahl,
- manuelles Wiederholen eines fehlgeschlagenen Jobs mit gespeicherten Optionen,
- Abbrechen aktiver Downloads und FFmpeg-Prozesse,
- Pause/Fortsetzen der automatischen Verarbeitung,
- konfigurierbare Netzwerk- und Medien-Timeouts,
- verständliche Klassifizierung temporärer und permanenter Fehler.

### Speicher- und Ressourcenmanagement

**Priorität:** P1

Vor Download und Transcoding werden weder freier Speicherplatz noch erwartete
Dateigröße geprüft. Es fehlen konfigurierbare Grenzwerte für Queue-Länge,
temporären Speicher, Parallelität und Dateigröße. Das Standard-Arbeitsverzeichnis
liegt in der beschreibbaren Containerschicht; bei einer Container-Neuerstellung
gehen wartende Download-Artefakte verloren.

### Zielpfad- und Kollisionsmanagement

**Priorität:** P1

Neben der sicherheitsrelevanten Fremdzuordnung fehlen:

- automatische Auflösung gleicher Autor-/Titelpfade verschiedener ASINs,
- Vorschau und Validierung aller Zielpfade vor einem Batchlauf,
- Behandlung veralteter Multipart-Dateien nach einer Neukonvertierung,
- robuste Längenbegrenzung pro Pfadkomponente,
- eine verwaltete Funktion zum Verschieben bereits vorhandener Ausgaben nach
  einer Templateänderung.

### Skalierung großer Bibliotheken

**Priorität:** P2

`list_books` in `app/audible_service.py:1317` lädt zunächst alle Bücher und ruft
anschließend für jedes Buch `book_status` mit mehreren neuen SQLite-Abfragen auf.
Die Dateiverknüpfungsansicht scannt bei jedem Aufruf rekursiv den gesamten
Audiobook-Baum und schneidet erst nach dem Scan auf 500 Ergebnisse ab.

Es fehlen Pagination, gemeinsame Statusabfragen, serverseitige SQL-Suche,
inkrementelle Dateierkennung und ein Cache beziehungsweise Dateisystemindex.

### Bibliotheks-Lifecycle

**Priorität:** P2

Die Synchronisierung führt ausschließlich Upserts aus. Audible-Titel, die beim
nächsten Export fehlen, bleiben dauerhaft ohne Kennzeichnung in SQLite. Sinnvoll
wären ein `last_seen_at`, ein Status für entfernte oder archivierte Titel und
eine UI-Filtermöglichkeit. Eine automatische Löschung lokaler Dateien sollte
nicht erfolgen.

### Betrieb und Wartung

**Priorität:** P2

Es fehlen:

- Health- und Readiness-Endpunkte,
- Anzeige des letzten automatischen Laufs und seines Ergebnisses,
- grundlegende Metriken und strukturierte Logs,
- Datenbank-Backup und dokumentierte Wiederherstellung,
- Aufbewahrungsregeln für Jobs und Verarbeitungshistorie,
- CI für Tests, Image-Build, Linting, Dependency- und Container-Scans,
- UI-Einstellungen für Zeitplan, Pfadvorlagen, Downloadqualität und
  Normalisierungswerte.

Optionale Produkterweiterungen sind mehrere Audible-Profile, Podcast-Support,
Begleit-PDFs und ein vorsichtiger regelbasierter Workflow für Produkte, die
Audible als getrennte ASINs ausliefert, inhaltlich aber zu einem Werk gehören.

## Bereits vorhandene Schutzmaßnahmen

- Betrieb des Containers als Nicht-Root-Benutzer.
- Subprozessaufrufe als Argumentlisten ohne `shell=True`.
- Parametrisierte SQL-Werte; der von Bandit gemeldete dynamische `IN`-Ausdruck
  erzeugt nur die notwendige Anzahl von `?`-Platzhaltern und ist kein bestätigter
  SQL-Injection-Befund.
- Zielpfade werden auf das konfigurierte Audiobook-Verzeichnis begrenzt.
- Jinja-Autoescaping für serverseitige Templates.
- CSRF-Tokens für produktive POST-Formulare.
- Content-Security-Policy, Frame-, Referrer- und Content-Type-Header.
- Private Staging-Dateien mit Modus `0600` im normalen Authentifizierungsflow.
- Atomare Veröffentlichung bei Normalisierung und Multipart-Merge.
- Persistente Queue mit Transaktionen und Schutz vor parallelen Jobs derselben
  ASIN.

Eine offensichtliche Shell-Injection, bestätigte SQL-Injection oder direkt über
die Weboberfläche nutzbare Pfadüberschreitung außerhalb von `/audiobooks` wurde
nicht gefunden.

## Prüfprotokoll

Ausgeführt wurden:

- `python3 -m compileall -q app tests` — erfolgreich
- `git diff --check` — erfolgreich
- Produktionsnaher Docker-Build — erfolgreich
- `python3 -m pytest -q` im gebauten Image — **90 Tests bestanden**
- Coverage-Messung — insgesamt etwa **81 % Statement Coverage**
- `web.py` — etwa **67 % Statement Coverage**
- Bandit 1.9.4 — 14 Low- und 2 Medium-Hinweise, keine High-Meldung
- Ruff 0.16.7 — 11 überwiegend stilistische beziehungsweise
  Exception-Handling-Hinweise
- pip-audit 2.10.1 — sechs eindeutige Advisories gegen `pip 25.0.1`
- Trivy 0.74.0 — 18 High-/Critical-Pakettreffer im gebauten Image

Die Tests verwenden Mocks für Audible und decken keinen echten Account-Login
oder echten Bibliotheksdownload ab. Einzelne reale FFmpeg-Smoke-Tests sind
vorhanden. Kritische Negativtests für Secret-Redaction, fremde Zielpfade,
atomare Einzelkonvertierung, verwaiste Auth-Staging-Verzeichnisse,
Nicht-ASCII-Basic-Auth und MP3-Fallback fehlen.

Der temporäre Audit-Container sowie die nur für die Prüfung erzeugten Audit- und
Scanner-Images wurden nach Abschluss entfernt.

## Empfohlene Umsetzungsreihenfolge

1. Geheimnisse in Subprozess-, Datenbank-, Log- und UI-Fehlern vollständig
   redigieren.
2. Unterstütztes Basisimage und vollständig aktualisierte Pakete verwenden;
   Scans als Release-Gate einführen.
3. Webzugriff fail-closed absichern und den Flask-Entwicklungsserver ersetzen.
4. Globale Zielpfad-Eigentümerschaft und atomare Einzeldateikonvertierung
   implementieren.
5. Authentifizierungsrotation crash-sicher machen und verwaiste Staging-Daten
   bereinigen.
6. Timeout, aktiven Abbruch, Retry/Backoff und Speicherplatzprüfungen ergänzen.
7. MP3-Fallback unterstützen.
8. Skalierung, Healthchecks, Backups, Aufbewahrung und erweiterte UI-Einstellungen
   umsetzen.
