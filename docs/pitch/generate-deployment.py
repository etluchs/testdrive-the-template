"""Erzeugt folie-4-deployment.svg.

Wie beim Architekturbild werden die Chip-Breiten aus der Zeichenzahl berechnet,
damit sich beim Ändern von Ressourcennamen nichts überlappt.
Aufruf: ``python3 generate-deployment.py``.
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


def arrow_down(x, y0, y1):
    add(
        f'<path d="M{x} {y0} V{y1}" stroke="{GREY}" stroke-width="3" '
        'marker-end="url(#arw4)"/>'
    )


def spoke(x, env, y=590, w=520):
    """Eine Landing Zone: Karte, CAE-Kasten, umgebende Ressourcen."""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="330" rx="10" fill="{PANEL}"/>')
    text(x + 40, y + 48, 26, f"Landing Zone {env.upper()}", fill=BLUE, weight=600)
    text(x + 40, y + 76, 20, "eigene Subscription · vended Spoke", fill=GREY)

    add(
        f'<rect x="{x + 40}" y="{y + 94}" width="{w - 80}" height="124" rx="8" '
        f'fill="#FFFFFF" stroke="{LINE}" stroke-width="2"/>'
    )
    text(x + 64, y + 124, 21, f"cae-scat-{env}", weight=600)
    text(x + 64, y + 148, 18, "internes Load-Balancing · Workload Profiles", fill=GREY)
    chips([f"ca-scat-{env}", f"caj-scat-{env}-live"], x + 64, y + 160, w - 128, size=19)

    chips([f"psql-scat-{env}", f"id-scat-{env}", "Easy-Auth-Token-Store", "Log Analytics"],
          x + 40, y + 232, w - 80, size=19)


add(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
    f'width="1920" height="1080" font-family="{FONT}">'
)
add("<title>Deployment: zwei Landing Zones, ein Eingang</title>")
add(
    '<defs><marker id="arw4" viewBox="0 0 12 10" refX="11" refY="5" '
    'markerWidth="9" markerHeight="8" orient="auto">'
    f'<path d="M0 0 L12 5 L0 10 z" fill="{GREY}"/></marker></defs>'
)
add('<rect width="1920" height="1080" fill="#FFFFFF"/>')
add(f'<rect x="0" y="0" width="1920" height="10" fill="{BLUE}"/>')

# Kopf
text(100, 118, 24, "DEPLOYMENT", fill=GREY, weight=600, spacing=2.5)
text(100, 212, 68, "Zwei Landing Zones, ein Eingang", fill=BLUE, weight=600)
text(100, 276, 31,
     "Öffentlich erreichbar wird eine App nur über das zentrale Application "
     "Gateway — eine eigene öffentliche IP verbietet die Policy.")

# Internet
add(
    '<rect x="555" y="320" width="190" height="52" rx="26" fill="#FFFFFF" '
    f'stroke="{LINE}" stroke-width="2"/>'
)
text(650, 354, 22, "Internet", anchor="middle")
arrow_down(650, 376, 416)
text(672, 404, 19, "HTTPS 443", fill=GREY)

# Connectivity Hub
add(f'<rect x="100" y="422" width="1100" height="120" rx="10" fill="{BLUE}"/>')
text(140, 466, 26, "Connectivity Hub — eigene Subscription", fill="#FFFFFF", weight=600)
chips(["Application Gateway", "WAF", "scat-test.azr.uzh.ch", "scat.azr.uzh.ch",
       "internal.azr.uzh.ch"],
      140, 482, 1020, dark=True)

# Peering in die beiden Spokes
for x0 in (360, 940):
    arrow_down(x0, 546, 586)
text(378, 578, 19, "VNet-Peering", fill=GREY)
text(958, 578, 19, "VNet-Peering", fill=GREY)

spoke(100, "test")
spoke(680, "prod")

# Azure OpenAI steht in einer eigenen Subscription und wird von beiden Landing
# Zones benutzt — deshalb liegt es ausserhalb der Spoke-Karten.
for x0 in (360, 940):
    arrow_down(x0, 922, 946)
add(
    '<rect x="100" y="950" width="1100" height="74" rx="10" fill="#FFFFFF" '
    f'stroke="{BLUE}" stroke-width="2"/>'
)
text(140, 979, 22, "Azure OpenAI — eigene Subscription, für Test und Prod dieselbe",
     fill=BLUE, weight=600)
text(140, 1006, 19,
     "Embeddings für die semantische Suche · Zugriff über Managed Identity "
     "· künftig der LLM-Proxy", fill=GREY)

# Rechte Spalte
add(
    '<rect x="1300" y="320" width="520" height="704" rx="10" fill="#FFFFFF" '
    f'stroke="{LINE}" stroke-width="2"/>'
)
text(1340, 376, 23, "WAS DARAUS FOLGT", fill=BLUE, weight=600, spacing=2)

blocks = [
    ("Keine öffentliche IP", [
        "Die Landing Zone verbietet sie per",
        "Policy — der Weg über das Gateway",
        "ist nicht bevorzugt, sondern der einzige.",
    ]),
    ("Zwei Pull Requests", [
        "Subnetze kommen von Subscription",
        "Vending, der Ingress-Eintrag von",
        "Connectivity Management.",
    ]),
    ("X-Forwarded-Host", [
        "Ohne den injizierten Header baut Easy",
        "Auth die Redirect-URI auf den internen",
        "FQDN — und der Login scheitert.",
    ]),
    ("Egress über die Firewall", [
        "Graph, Login und Registry brauchen",
        "Regeln; sonst kommt die App nicht raus.",
    ]),
]
y = 452
for head, lines in blocks:
    add(f'<rect x="1340" y="{y - 21}" width="9" height="9" fill="{BLUE}"/>')
    text(1366, y - 10, 27, head, weight=600)
    ly = y + 24
    for line in lines:
        text(1366, ly, 21, line, fill=GREY)
        ly += 28
    y = ly + 46

text(100, 1058, 21, "Adate / Appkit — Pitch", fill=GREY)
text(1820, 1058, 21, "4 / 6", fill=GREY, anchor="end")
add("</svg>")

path = pathlib.Path(__file__).parent / "folie-4-deployment.svg"
path.write_text("\n".join(out), encoding="utf-8")
print("ok", path)
