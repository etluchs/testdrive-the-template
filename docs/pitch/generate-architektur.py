"""Erzeugt folie-3-architektur.svg.

Die Chip-Breiten werden aus der Zeichenzahl berechnet, damit sich beim Ändern
der Modulnamen nichts überlappt. Aufruf: ``python3 generate-architektur.py``.
Die übrigen Folien sind handgeschriebenes SVG und werden direkt editiert.
"""

import pathlib

FONT = (
    "'Source Sans Pro','Source Sans 3','Helvetica Neue',"
    "Arial,Helvetica,sans-serif"
)
BLUE, INK, GREY, LINE, PANEL = "#0028A5", "#121212", "#666666", "#E9E9E9", "#F5F5FB"

out: list[str] = []


def add(s: str) -> None:
    out.append(s)


def text(x, y, size, content, fill=INK, weight=None, spacing=None, anchor=None):
    """Ein <text>-Element. SVG bricht nicht um — jede Zeile ist ein Aufruf."""
    attrs = [f'x="{x}"', f'y="{y}"', f'font-size="{size}"', f'fill="{fill}"']
    if weight:
        attrs.append(f'font-weight="{weight}"')
    if spacing:
        attrs.append(f'letter-spacing="{spacing}"')
    if anchor:
        attrs.append(f'text-anchor="{anchor}"')
    add(f"<text {' '.join(attrs)}>{content}</text>")


def chips(items, x, y, maxw, size=20, gap=10, pad=15, dark=False):
    """Fliesst Chips in Zeilen; gibt die y-Position nach der letzten Zeile zurück."""
    cx, cy, h = x, y, size + 18
    stroke = "rgba(255,255,255,.45)" if dark else LINE
    for label in items:
        w = int(len(label) * size * 0.50) + 2 * pad
        if cx + w > x + maxw:
            cx, cy = x, cy + h + gap
        add(
            f'<rect x="{cx}" y="{cy}" width="{w}" height="{h}" rx="{h // 2}" '
            f'fill="#FFFFFF" stroke="{stroke}" stroke-width="1.5"/>'
        )
        text(f"{cx + w / 2:.0f}", f"{cy + h / 2 + size * 0.35:.0f}", size, label,
             anchor="middle")
        cx += w + gap
    return cy + h


add(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
    f'width="1920" height="1080" font-family="{FONT}">'
)
add("<title>Drei Repos, drei Zuständigkeiten</title>")
add(
    '<defs><marker id="arw3" viewBox="0 0 12 10" refX="11" refY="5" '
    'markerWidth="9" markerHeight="8" orient="auto">'
    f'<path d="M0 0 L12 5 L0 10 z" fill="{GREY}"/></marker></defs>'
)
add('<rect width="1920" height="1080" fill="#FFFFFF"/>')
add(f'<rect x="0" y="0" width="1920" height="10" fill="{BLUE}"/>')

# Kopf
text(100, 118, 24, "ARCHITEKTUR", fill=GREY, weight=600, spacing=2.5)
text(100, 212, 68, "Drei Repos, drei Zuständigkeiten", fill=BLUE, weight=600)
text(100, 276, 31,
     "Eines wird geklont, eines importiert, eines gebaut — und genau das "
     "entscheidet, wo eine Änderung ankommt.")

# Reihe 1: Template -> Deine App
add(f'<rect x="100" y="360" width="500" height="230" rx="10" fill="{PANEL}"/>')
text(140, 410, 26, "adate — das Template", fill=BLUE, weight=600)
text(140, 442, 21, "Startpunkt für jede neue App", fill=GREY)
chips(["Projektgerüst", "_macros.html", "app.css (UZH CD)", "AGENTS.md",
       "CI: ruff · pytest · pa11y", "Dockerfile"], 140, 458, 420)

add(f'<rect x="720" y="360" width="500" height="230" rx="10" fill="{PANEL}"/>')
text(760, 410, 26, "Deine App — z. B. scat", fill=BLUE, weight=600)
text(760, 442, 21, "der Servicekatalog als Referenz", fill=GREY)
chips(["main.py", "logic.py", "catalog.py", "search.py", "templates/",
       "static/", "tests/"], 760, 458, 420)

add(f'<path d="M622 474 H700" stroke="{GREY}" stroke-width="3" marker-end="url(#arw3)"/>')
text(660, 452, 20, "git clone", weight=600, anchor="middle")
text(660, 516, 19, "einmalig", fill=GREY, anchor="middle")

# Reihe 2: appkit
add(f'<rect x="100" y="662" width="1120" height="176" rx="10" fill="{BLUE}"/>')
text(140, 712, 26, "appkit — die gemeinsame Bibliothek", fill="#FFFFFF", weight=600)
text(140, 744, 21,
     "Import, versioniert · besitzt Netzwerk, Anmeldung und Fakes",
     fill="rgba(255,255,255,.8)")
chips(["sharepoint", "mail", "auth", "db", "embeddings"], 140, 760, 1040, dark=True)

for x0 in (350, 970):
    add(f'<path d="M{x0} 650 V602" stroke="{GREY}" stroke-width="3" marker-end="url(#arw3)"/>')
text(382, 634, 19, "import", fill=GREY)
text(1002, 634, 19, "import", fill=GREY)

# Reihe 3: Plattform
add(
    '<rect x="100" y="906" width="1120" height="88" rx="10" fill="#FFFFFF" '
    f'stroke="{LINE}" stroke-width="2"/>'
)
text(140, 944, 21, "PLATTFORM", fill=GREY, weight=600, spacing=1.5)
text(140, 976, 22,
     "Azure Container Apps · Managed Identity · Easy Auth · "
     "Microsoft Graph · Postgres")

# Rechte Spalte
add(
    '<rect x="1300" y="360" width="520" height="634" rx="10" fill="#FFFFFF" '
    f'stroke="{LINE}" stroke-width="2"/>'
)
text(1340, 422, 23, "WARUM DAS WICHTIG IST", fill=BLUE, weight=600, spacing=2)

blocks = [
    ("Geklont", [
        "Spätere Verbesserungen am Template",
        "wandern nicht von selbst in eine",
        "bestehende App.",
    ]),
    ("Importiert", [
        "Was in appkit behoben wird, kommt",
        "mit dem nächsten Update bei allen",
        "Apps an — einmal statt in jeder App.",
    ]),
    ("Daraus folgt die Regel", [
        "Fehlendes gehört in appkit, mit Fake",
        "und Test — nicht als Sonderlösung",
        "in die einzelne App.",
    ]),
]
y = 490
for head, lines in blocks:
    add(f'<rect x="1340" y="{y - 21}" width="9" height="9" fill="{BLUE}"/>')
    text(1366, y - 10, 28, head, weight=600)
    ly = y + 26
    for line in lines:
        text(1366, ly, 23, line, fill=GREY)
        ly += 31
    y = ly + 52

text(100, 1058, 21, "Adate / Appkit — Pitch", fill=GREY)
text(1820, 1058, 21, "3 / 6", fill=GREY, anchor="end")
add("</svg>")

path = pathlib.Path(__file__).parent / "folie-3-architektur.svg"
path.write_text("\n".join(out), encoding="utf-8")
print("ok", path)
