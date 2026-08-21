# Pitchfolien

Sechs Folien (deutsch, 1920 × 1080) für zwei Zielgruppen: die Fachseite, die
wissen will, was sie bekommt und was sie beitragen muss — und die Entwicklung,
die sicher sein will, dass an ihr keine KI-Altlast hängen bleibt.

| Folie | Inhalt |
| --- | --- |
| 1 · Worum es geht | SharePoint → Indexer → Elasticsearch → CGI-Proxy → SPA gegen SharePoint → App |
| 2 · Für Fachnutzer:innen | Was du erreichst / was du beiträgst / womit du arbeitest |
| 3 · Architektur | adate wird geklont, appkit wird importiert — und was daraus folgt |
| 4 · Deployment | Zwei Landing Zones hinter dem Application Gateway im Connectivity Hub |
| 5 · Für Entwickler:innen | Die drei AGENTS.md-Regeln und was ruff, pytest und pa11y erzwingen |
| 6 · Beleg und nächste Schritte | Gemessene Suchqualität, dann die offenen Punkte |

## Vorführen

`index.html` ist ein reveal.js-Deck über denselben SVG-Dateien. Öffnen genügt:

```sh
open docs/pitch/index.html          # oder: python3 -m http.server, dann im Browser
```

Pfeiltasten blättern, `F` geht in den Vollbildmodus, `S` öffnet die
Sprechernotizen in einem zweiten Fenster, `?` zeigt alle Tastenkürzel, und
`index.html?print-pdf` ergibt über den Druckdialog ein PDF. reveal.js 6.0.1
liegt vendored in `reveal/` (MIT, vier Dateien, 252 KB) — kein CDN, damit das
Deck auch ohne Netz läuft.

## Einzelne Folien

Jede Folie liegt als `.svg` (Original) und als `.png` in 1920 px Breite vor —
SVG für PowerPoint und Google Slides, PNG für Keynote und alles, was SVG nicht
sauber importiert. Nach einer Änderung am SVG das PNG neu erzeugen:

```sh
rsvg-convert -w 1920 folie-1-worum-es-geht.svg -o folie-1-worum-es-geht.png
```

Folie 3 und 4 werden von `generate-architektur.py` und
`generate-deployment.py` erzeugt, damit die Chip-Breiten beim Umbenennen von
Modulen oder Ressourcen automatisch stimmen. Die übrigen vier sind
handgeschriebenes SVG und werden direkt editiert; Text bricht in SVG nicht um,
jede Zeile ist also ein eigenes `<text>` bzw. `<tspan>`.

Folie 4 folgt der Namensgebung von
[uzh-app-platform](https://github.com/uzh-zi/uzh-app-platform): alle Ressourcen
heissen `<präfix>-${project}-${env}`, und die Folie setzt `project = "scat"` ein
— also die konkrete App statt des Template-Namens. Zwei Vereinfachungen sind
bewusst: Prod existiert dort heute nicht als eigene `tfvars` (die Folie zeigt
Test und Prod als Zielbild, das die Trennung der Subscriptions vorgibt), und die
beiden Hostnamen am Gateway sind illustrativ — verbindlich wird ein Hostname
erst mit dem Routing-Eintrag in UZH-Connectivity-Management.

Zwei Kästchen werden gern verwechselt: der **Easy-Auth-Token-Store** ist der
Blob-Container, in dem die Plattform das ID-Token ablegt (`APPKIT_AUTH=verify`
funktioniert ohne ihn nicht), und gehört deshalb in die Landing Zone. Das
**Azure-OpenAI-Deployment** für die Embeddings steht dagegen ausserhalb: eigene
Subscription, von Test und Prod gemeinsam genutzt, künftig der LLM-Proxy.

## Woher die Zahlen stammen

Alle Zahlen kommen aus dem Servicekatalog (`scat`), mit dem das Template
validiert wurde — nichts davon ist geschätzt:

- **112 von 187** Einträgen publiziert, per Test gepinnt (`tests/test_catalog.py`)
- **106 Tests**, rund **1570 Zeilen** Python für die ganze Anwendung
- **Trefferquote 67 % → 83 %** (Top 1) und **75 % → 100 %** (Top 3), gemessen mit
  `scripts/eval_search.py` über alle 112 publizierten Services

**Beim Vortragen wichtig:** die höheren Werte gelten nur mit der semantischen
Ebene. `appkit.embeddings` ist in appkit `main` vorhanden, braucht zur Laufzeit
aber einen Azure-OpenAI-Endpunkt (`APPKIT_EMBEDDINGS_ENDPOINT`) und die Rolle
*Cognitive Services OpenAI User* für die Managed Identity. Ohne beides sind es
67 % und 75 % — die App läuft dann vollständig auf Lexik und Synonymen. Die
Einschränkung steht als Fussnote auf Folie 5; bitte nicht weglassen.

## Gestaltung

UZH Corporate Design (frontend framework 2.10.0): UZH-Blau `#0028A5`, Schrift
Source Sans Pro mit Fallback auf Helvetica/Arial. Die Balken auf Folie 5 nutzen
zwei Stufen desselben Blaus (`#0028A5` / `#6478C9`) und tragen jeweils ein
direktes Zahlenlabel, damit die Aussage nicht allein an der Farbe hängt.
