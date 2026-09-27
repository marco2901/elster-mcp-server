# elster-mcp-server

Ein **Model Context Protocol (MCP) Server** für das deutsche Steuerportal
[ELSTER](https://www.elster.de). Claude oder ein anderer MCP-Client kann damit
UStVA, Anlage EÜR und ESt vorbereiten sowie Posteingang und Übermittlungshistorie lesen.

Seit v0.2 ist die **Python-Implementierung** (`elster_mcp/`, Playwright + offizielles
`mcp`-SDK) die Hauptvariante. Die ursprüngliche TypeScript/Puppeteer-Version liegt
unverändert in `src/` (siehe [Legacy TypeScript](#legacy-typescript-version)).

---

## ⚖️ Legal Notice / Rechtlicher Hinweis

**English**

- This project is an **experimental, community-built tool**. It is **not affiliated with, endorsed by, or supported by** the Bundesministerium der Finanzen, the ELSTER project, or any tax authority.
- The official, supported way to submit tax data programmatically is the **ERiC library** (registration as a software vendor required). This tool instead automates the public ELSTER **web portal** with a real user session — the same path a human user takes — using credentials YOU provide.
- The official ELSTER **terms of use ("Nutzungsbedingungen")** may restrict automated access to the portal. Whether your specific use is permitted is **your responsibility to verify** before running this software.
- **Use at your own risk.** The author(s) provide this software **AS IS, WITHOUT WARRANTY OF ANY KIND** (see [LICENSE](LICENSE)). The author(s) **accept NO liability** for incorrect tax submissions, account suspensions, missed deadlines, lost data, or any other consequences arising from the use of this software.
- This project is **not tax advice** (no "Hilfeleistung in Steuersachen" in the sense of § 2 StBerG). If you are unsure whether a submission is correct, consult a *Steuerberater*.
- Operators using this software in a **commercial context** (e.g. submitting on behalf of third parties) may be subject to the German *Steuerberatungsgesetz* and must verify their own licensing situation.

**Deutsch**

- Dieses Projekt ist ein **experimentelles, von der Community gebautes Werkzeug**. Es ist **weder vom Bundesministerium der Finanzen noch vom ELSTER-Projekt noch von einer Finanzbehörde unterstützt, autorisiert oder geprüft**.
- Der offizielle, vom BMF unterstützte Weg zur programmatischen Übermittlung von Steuerdaten ist die **ERiC-Bibliothek** (Registrierung als Softwarehersteller erforderlich). Dieses Tool nimmt stattdessen den Weg über das öffentliche **ELSTER-Webportal** — denselben Weg, den ein menschlicher Nutzer per Browser geht — mit Zertifikatsdaten, die DU bereitstellst.
- Die offiziellen **ELSTER-Nutzungsbedingungen** können automatisierten Zugriff auf das Portal einschränken oder verbieten. Es liegt **in deiner alleinigen Verantwortung** zu prüfen, ob dein konkreter Anwendungsfall erlaubt ist, bevor du dieses Tool nutzt.
- **Nutzung auf eigenes Risiko.** Die Autor:innen stellen die Software **OHNE JEGLICHE GEWÄHRLEISTUNG** bereit (siehe [LICENSE](LICENSE)). Die Autor:innen übernehmen **keine Haftung** für fehlerhafte Steuerübermittlungen, gesperrte Konten, versäumte Fristen, Datenverluste oder sonstige Folgen aus der Nutzung dieser Software.
- Dieses Projekt ist **keine Steuerberatung** im Sinne des § 2 StBerG. In Zweifelsfällen ist ein:e Steuerberater:in zu konsultieren.
- Wer diese Software **gewerblich** einsetzt (z.B. Übermittlung im Auftrag Dritter), unterliegt unter Umständen dem Steuerberatungsgesetz und muss seine Berechtigung selbst sicherstellen.

**Practical safeguards built into the tool**

- The only tool that actually transmits data is `elster_ustva_confirm` — it requires an **explicit second call** after `elster_ustva_start` has paused at `AWAITING_CONFIRM`. Nothing is sent without that second confirmation.
- The EÜR and ESt tools **never submit**. They only fill the form up to "Prüfen" and stop, so you review and submit yourself in the ELSTER portal.
- All sync / history / inbox tools are read-only and never modify state on the ELSTER side.


---

## Sicherheitsmerkmale – wie sie geschützt werden

Deine ELSTER-Sicherheitsmerkmale sind die **Zertifikatsdatei (`.pfx`)** und das
**Zertifikats-Passwort**. Dazu kommen Steuernummer und Stammdaten. Der Server behandelt sie so:

| Schutz | Umsetzung |
|---|---|
| Nie im Repository | `*.pfx`, `*.p12`, `config.json`, `secrets/`, `audit/`, Downloads und Screenshots sind gitignored. Das Passwort gehört nicht in `config.json`. |
| Sichere Passwortquellen | Reihenfolge: `ELSTER_PASSWORD_FILE` (Docker-Secret/Datei mit `chmod 600`) → OS-Keyring (`python -m elster_mcp store-secret cert-password`) → `ELSTER_PASSWORD` → `config.json` (nur mit Warnung) |
| Keine Klartext-Lecks | Passwort und Token werden als `SecretStr` gehalten. Passwort, Token und Steuernummer werden in **allen Logs** und im Audit-Log durch `***` ersetzt. `elster_config_show` maskiert sie. |
| Dateirechte | Zertifikat und Secret-Dateien werden geprüft. Mit `ELSTER_STRICT_PERMISSIONS=1` ist eine gruppen- oder weltlesbare Datei ein harter Fehler. Screenshots, PDFs und das Audit-Log werden mit `0600` geschrieben, die Verzeichnisse mit `0700`. |
| Zertifikatsprüfung | Nur `.pfx`/`.p12` mit plausibler Größe wird in den Browser geladen. |
| Browser-Isolation | Jede Sitzung bekommt einen frischen Browser-Kontext ohne Cookies. **Alle Requests außer `https://*.elster.de` werden blockiert**, es gibt also keinen Datenabfluss an Tracker oder Dritte. Die Chromium-Sandbox ist standardmäßig aktiv. |
| Übermittlungssperre | `ELSTER_ALLOW_SUBMIT` ist standardmäßig **aus**. Ohne diesen Schalter kann nichts ans Finanzamt gesendet werden. |
| Freigabecode | Nach der ELSTER-Prüfung erzeugt der Server einen HMAC-Code, der an **genau diese Sitzung und genau diese Beträge** gebunden ist. `elster_ustva_confirm` verlangt diesen Code. |
| Mensch im Loop | Unterstützt der Client *Elicitation*, fragt der Server **dich direkt** (nicht das Modell) und du musst den Code abtippen. `ELSTER_REQUIRE_ELICITATION=1` erzwingt das. |
| Strikte Eingaben | Nur bekannte Kennziffern und EÜR-Felder, keine negative Vorsteuer, Plausibilitätsgrenzen für Beträge, Jahr und Zeitraum. Nicht gefundene Kennziffern führen zum **Abbruch** statt zu einer stillen Teilübermittlung. |
| Audit-Log | Jede Übermittlungsstufe, Ablehnung, jeder Login-Test und jeder Abbruch landet als JSONL in `audit/elster-audit.jsonl`, ohne Geheimnisse. |
| HTTP nur mit Token | Der HTTP-Transport startet nur mit einem Token (mind. 32 Zeichen, Vergleich in konstanter Zeit) und bindet standardmäßig an `127.0.0.1`. |
| Ressourcen | Höchstens 2 parallele Browser. Nicht bestätigte UStVA-Sitzungen verfallen nach 15 min, ohne dass etwas übermittelt wird. |

## Installation (Python ≥ 3.10)

```bash
git clone https://github.com/marco2901/elster-mcp-server.git
cd elster-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[keyring]"
playwright install chromium
```

## Sicherheitsmerkmale hinterlegen

```bash
# 1. Zertifikat außerhalb des Repos ablegen und schützen
mkdir -p ~/.elster && chmod 700 ~/.elster
cp /pfad/zu/deinem/Zertifikat.pfx ~/.elster/zertifikat.pfx
chmod 600 ~/.elster/zertifikat.pfx

# 2. Passwort in den OS-Keyring (macOS-Schlüsselbund / Windows Credential Manager / Secret Service)
python -m elster_mcp store-secret cert-password

# 3. Stammdaten
cp config.example.json config.json   # pfxPath, taxNumber, stateCode, Name, Adresse eintragen

# 4. Prüfen (ohne Login)
python -m elster_mcp check
```

Alternativ ohne Keyring: das Passwort in eine Datei mit `chmod 600` schreiben und
`ELSTER_PASSWORD_FILE=/pfad/zur/datei` setzen.

## Mit Claude Desktop / Claude Code (stdio)

```json
{
  "mcpServers": {
    "elster": {
      "command": "/absolute/path/to/elster-mcp-server/.venv/bin/elster-mcp",
      "args": ["serve"],
      "env": { "ELSTER_CONFIG_PATH": "/absolute/path/to/elster-mcp-server/config.json" }
    }
  }
}
```

Claude Code: `claude mcp add elster -- /absolute/path/.venv/bin/elster-mcp serve`

## Betrieb auf dem Docker-Host (Portainer, Traefik, Authelia)

Der Aufbau entspricht den übrigen biegel24-MCP-Servern:

- **Image:** `ghcr.io/marco2901/elster-mcp-server`, gebaut von `.github/workflows/docker.yml`
  (Tests, dann Push; `latest` nur aus `main`).
- **Stack:** `deploy/portainer-stack.yml`. Traefik übernimmt TLS (`*.biegel24.de`),
  `middlewares-rate-limit` und `middlewares-secure-headers`. Der Container läuft read-only,
  ohne Capabilities und als `pwuser` (UID 1001). Watchtower ist für diesen Container bewusst
  deaktiviert.
- **Anmeldung:** Claude.ai findet über `/.well-known/oauth-protected-resource/mcp` den
  Authelia-Server, du meldest dich dort an (Client `elster-mcp`, **2FA-Pflicht**, siehe
  `deploy/authelia-client.yml`). Der Server prüft jedes Token per Introspection.
  `OIDC_ALLOWED_USERS` begrenzt den Zugang auf deinen Benutzer. Für CLI-Clients gilt
  zusätzlich `Authorization: Bearer <mcp_api_key>`.
- **Geheimnisse:** als Dateien unter `/docker-data/secrets/elster-mcp/`
  (`elster_cert.pfx`, `elster_password`, `mcp_api_key`, `oidc_client_secret`;
  Rechte 700/600, Eigentümer 1001). Sie werden nicht als Portainer-Variablen gesetzt.

Zertifikat und Passwort ablegen (auf dem Docker-Host):

```bash
sudo install -o 1001 -g 1001 -m 600 /pfad/zu/Zertifikat.pfx /docker-data/secrets/elster-mcp/elster_cert.pfx
sudo sh -c 'umask 077; read -rs -p "Zertifikats-Passwort: " P; printf %s "$P" > /docker-data/secrets/elster-mcp/elster_password'
sudo chown 1001:1001 /docker-data/secrets/elster-mcp/elster_password
```

Lokal ohne Traefik genügt `docker-compose.yml` im Repo-Root mit Secrets aus `./secrets/`.

## Tools

| Tool | Zweck | Übermittelt? |
|---|---|---|
| `elster_security_check` | Zertifikat, Passwortquelle, Dateirechte, Freigabe-Status prüfen (lokal) | Nein |
| `elster_config_show` | Geladene Konfiguration, maskiert | Nein |
| `elster_login_test` | Login mit Zertifikat testen | Nein |
| `elster_kennziffern_list` | Unterstützte UStVA-Kennziffern | Nein |
| `elster_ustva_generate_xml` | UStVA validieren + XML-Snapshot fürs Archiv | Nein |
| `elster_ustva_detect_reverse_charge` | §13b-Erkennung anhand der Lieferantenmuster | Nein |
| `elster_ustva_start` | Login, Formular, Prüfung, dann **Pause** bei `AWAITING_CONFIRM` | Nein |
| `elster_ustva_confirm` | „Absenden“, nur mit Sperre aus + Freigabecode (+ Elicitation) | **Ja** |
| `elster_eur_start` | Anlage EÜR bis zur Prüfung füllen, als Entwurf speichern | Nein |
| `elster_est_start` | ESt 1 A vorbereiten, 30 min zur Kontrolle offen | Nein |
| `elster_session_status` / `_list` / `_cancel` | Sitzungsverwaltung | Nein |
| `elster_sync_history` | „Übermittelte Formulare“ lesen | Nein |
| `elster_sync_inbox` | Posteingang lesen (Betreff, Lesestatus), optional Nachrichten-PDF + alle Anhänge | Nein |

### Ablauf UStVA

```text
1. elster_security_check                      → ok: true
2. elster_ustva_start(year=2026, period="Q1",
     report={"81": 12000, "86": 300, "66": 1845.30})   → sessionId
3. elster_session_status(sessionId)           → AWAITING_CONFIRM, summary, screenshotPath, confirmationCode
   → Mensch prüft Screenshot + Beträge
4. elster_ustva_confirm(sessionId, confirmationCode)
   → (Client fragt dich direkt) → Transferticket
```

## Wichtige Umgebungsvariablen

| Variable | Bedeutung |
|---|---|
| `ELSTER_PFX_PATH` | Pfad zur Zertifikatsdatei |
| `ELSTER_PASSWORD_FILE` / `ELSTER_PASSWORD` | Zertifikats-Passwort (Datei bevorzugt) |
| `ELSTER_TAX_NUMBER`, `ELSTER_STATE_CODE` | Steuernummer, Bundesland-Code |
| `ELSTER_ALLOW_SUBMIT` | `1` = Übermittlung freigeschaltet (Standard: aus) |
| `ELSTER_REQUIRE_ELICITATION` | `1` = nur mit direkter Nutzerbestätigung |
| `ELSTER_STRICT_PERMISSIONS` | `1` = unsichere Dateirechte sind ein Fehler |
| `ELSTER_SCREENSHOTS` | `0` = keine Screenshots (enthalten Steuerdaten) |
| `ELSTER_HEADLESS` | `false` = Browser sichtbar (zum Debuggen) |
| `ELSTER_CHROMIUM_PATH` | eigenes Chrome/Chromium verwenden |
| `ELSTER_MCP_TOKEN_FILE` / `ELSTER_MCP_TOKEN` | Bearer-Token für HTTP |
| `ELSTER_MCP_HOST`, `ELSTER_MCP_PORT` | HTTP-Bindung (Standard `127.0.0.1:8765`) |

## Entwicklung

```bash
pip install -e ".[dev]"
pytest          # Tests für Secrets, Validierung, Freigabelogik, Token-Middleware
ruff check elster_mcp tests
```

## Grenzen

- Die Selektoren des ELSTER-Portals können sich ändern. Zum Debuggen mit
  `ELSTER_HEADLESS=false` starten und die Screenshots in `./screenshots/` ansehen.
- Lädt das Portal Ressourcen von Hosts außerhalb von `elster.de`, müssen diese unter
  `security.allowedHosts` ergänzt werden. Blockierte Requests erscheinen mit
  `ELSTER_LOG_LEVEL=DEBUG` im Log.
- `elster_sync_history` liefert in der Python-Version nur die Liste, keine PDFs.
- Offizielle programmatische Übermittlung geht nur über ERiC (Hersteller-Registrierung).

---

## Legacy TypeScript-Version

Die ursprüngliche Implementierung (Node.js ≥ 18, Puppeteer) liegt in `src/` und wird mit
`npm install && npm run build && node dist/index.js` gestartet. Ihre Dokumentation:

### Features

| Tool | What it does | Submits? |
|------|--------------|----------|
| `elster_login_test` | Verifies your certificate + password can log in | No |
| `elster_config_show` | Shows the loaded config (secrets redacted) | No |
| `elster_kennziffern_list` | Returns the supported UStVA Kennziffern with descriptions | No |
| `elster_ustva_generate_xml` | Generates a UStVA XML snapshot (archive only) | No |
| `elster_ustva_detect_reverse_charge` | Detects §13b reverse-charge suppliers | No |
| `elster_ustva_start` | Logs in, fills, runs Prüfung, then **pauses for confirmation** | Pauses |
| `elster_ustva_confirm` | Clicks "Absenden" after you reviewed | **Yes** |
| `elster_eur_start` | Fills Anlage EÜR up to Prüfung, then "Speichern und Verlassen" | No |
| `elster_est_start` | Opens ESt 1 A, fills basics, runs Prüfung, keeps browser open 30 min | No |
| `elster_sync_history` | Reads "Übermittelte Formulare" (optionally with PDFs) | No |
| `elster_sync_inbox` | Reads ELSTER inbox (subject, read status), optionally message PDF + all attachments | No |
| `elster_session_status` / `_list` / `_cancel` | Session management | No |

### Requirements

- **Node.js ≥ 18**
- An **ELSTER certificate file** (`.pfx`) — get it from `https://www.elster.de` → "Mein ELSTER" → "Mein Benutzerkonto" → "Zertifikat verlängern"
- The certificate password
- Your **Steuernummer** and **Bundesland-Code**

### Install

```bash
git clone https://github.com/YOUR_USERNAME/elster-mcp-server.git
cd elster-mcp-server
npm install
npm run build
```

Puppeteer will install a bundled Chromium on first install (~150 MB).

### Configuration

```bash
cp config.example.json config.json
$EDITOR config.json
```

All keys in `config.json` can be overridden by environment variables
(`ELSTER_PFX_PATH`, `ELSTER_PASSWORD`, `ELSTER_TAX_NUMBER`,
`ELSTER_STATE_CODE`, `ELSTER_NAME`, `ELSTER_FIRST_NAME`, `ELSTER_STREET`,
`ELSTER_HOUSE_NUMBER`, `ELSTER_ZIP`, `ELSTER_CITY`, `ELSTER_COUNTRY`,
`ELSTER_DOWNLOAD_DIR`, `ELSTER_SCREENSHOT_DIR`, `ELSTER_HEADLESS`,
`ELSTER_EST_SKIP_EUR`). Env vars win over the file.

You can also point the loader at a different config file via
`ELSTER_CONFIG_PATH=/path/to/your/config.json`.

The two-digit `stateCode` for your Finanzamt is published by ELSTER —
look up the current value in the official ELSTER documentation.

#### Reverse-Charge supplier list

Add your `§13b UStG` suppliers under `ustva.reverseChargeSuppliers` in
`config.json`. Patterns are case-insensitive regexes matched against the
voucher's `contactName` or `description`. Example entry:

```json
{ "pattern": "your-supplier\\s+ireland", "region": "EU", "name": "Your Supplier Ireland" }
```

### Use with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "elster": {
      "command": "node",
      "args": ["/absolute/path/to/elster-mcp-server/dist/index.js"],
      "env": {
        "ELSTER_CONFIG_PATH": "/absolute/path/to/elster-mcp-server/config.json"
      }
    }
  }
}
```

See `examples/claude_desktop_config.json` for the template.

### Use with any MCP client

Run the server in stdio mode:

```bash
node dist/index.js
```

Then connect via your client's MCP transport.

### Typical UStVA flow

```text
1. elster_login_test                          → { ok: true }
2. elster_kennziffern_list                    → reference for valid codes
3. elster_ustva_start({                       → { sessionId: "ustva-..." }
     year: 2026,
     period: "Q1",
     report: { "81": 12000, "86": 300, "66": 1845.30 }
   })
4. elster_session_status({ sessionId })       → poll until status == AWAITING_CONFIRM
   (open the screenshot at screenshotPath to verify)
5. elster_ustva_confirm({ sessionId })        → { success: true, ticket: "..." }
```

### Typical EÜR flow

```text
1. elster_login_test
2. elster_eur_start({
     year: 2025,
     data: {
       betriebseinnahmen: 50000,
       fahrzeugkosten: 1200,
       afa: 800,
       homeOffice: 1260
     }
   })
3. elster_session_status (poll until SAVED or AWAITING_REVIEW)
4. open the ELSTER portal in your browser → "Meine Formulare" → review the draft → submit manually
```

### Security notes

- **Never commit your `.env`, `config.json`, or `.pfx`.** They are gitignored by default.
- The certificate password is read from env / config and passed to Puppeteer — make sure
  the host running this server is trusted.
- Set `ELSTER_HEADLESS=false` once to watch the first run and confirm everything is wired correctly.

### Limitations

- The ELSTER portal selectors can change. If a flow breaks, run with `ELSTER_HEADLESS=false`
  and check the screenshots written to `./screenshots/`.
- The ESt tool is intentionally a thin wrapper — German income-tax forms (Anlage G, V, N, S, KAP …)
  are dozens of different forms with thousands of fields. This server provides the framework
  (login, open, fill-by-label-or-id, Prüfen) and leaves the field choices to you.
- No XML submission path. Official programmatic submission requires the ERiC library
  (registration as a software vendor). This server uses the same Online-Formular path
  that any taxpayer uses.

### License

[MIT](LICENSE)

### Contributing

PRs welcome. The most useful additions are:

1. More robust selectors for changed ELSTER pages
2. Pre-filled Anlage G / V / N / S templates for ESt
3. A typed `report` schema validator for `elster_ustva_*`

When opening an issue, please run with `ELSTER_HEADLESS=false` and attach the
screenshot under `./screenshots/` that shows the failure.

