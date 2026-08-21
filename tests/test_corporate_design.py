"""Apps built from this template must look like UZH, without phoning home.

Corporate design conformance is easy to lose by accident — someone swaps a
colour, or "temporarily" pulls a font from a CDN and it never comes back. These
tests pin the parts that can be checked mechanically. They cannot judge whether
the result *looks* right; that is what a screenshot in review is for.

They are part of the template on purpose: an app that inherits the macros and
the stylesheet should inherit the checks that keep them honest, so drift shows
up in that app's own CI rather than at a design review months later.

Reference: UZH frontend framework 2.10.0,
https://www.frontend.uzh.ch/prod/index.html
"""

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "app" / "static"
TEMPLATES = Path(__file__).parent.parent / "app" / "templates"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")

#: The stylesheet with comments removed. The comments in `app.css` quote the
#: framework's own declarations to explain where we follow it and where we do
#: not, so a naive substring search finds them and concludes the opposite.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

#: The framework's palette, as the bare RGB triples it stores them in.
CD_COLOURS = {
    "--c-blue": "0, 40, 165",
    "--c-blue-light": "245, 245, 251",
    "--c-blue-muted": "27, 33, 74",
    "--c-black": "18, 18, 18",
    "--c-grey": "102, 102, 102",
    "--c-grey-light": "250, 250, 250",
    "--c-grey-medium": "233, 233, 233",
}


def test_the_palette_matches_the_framework():
    for name, value in CD_COLOURS.items():
        assert f"{name}: {value};" in RULES, f"{name} does not match UZH CD 2.10.0"


def test_the_type_scale_matches_the_framework():
    # 42/26/18px at 600, the framework's heading sizes.
    assert "font-size: 2.625rem;" in RULES  # h1
    assert "font-size: 1.625rem;" in RULES  # h2
    # Body copy is 18px, which is larger than a typical default and is a
    # visible part of the CD rather than an incidental choice.
    assert "font-size: 1.125rem;" in RULES


def test_the_corporate_font_is_vendored():
    for weight in ("Regular", "Semibold"):
        font = STATIC / "fonts" / f"SourceSans3-{weight}.otf.woff2"
        assert font.is_file(), f"{font.name} is missing"
        assert font.stat().st_size > 10_000
    assert 'font-family: "Source Sans Pro"' in RULES


def test_every_macro_component_is_styled():
    """Anything `_macros.html` emits needs styling, or it lands unstyled.

    The macros are the only sanctioned way to produce UI here, so this is the
    complete surface the stylesheet has to cover.
    """
    for selector in (
        ".field", ".field__label", ".field__input", ".field__help", ".field__error",
        ".btn", ".btn--primary", ".btn--secondary",
        ".table", ".table__caption", ".table__scroll",
        ".alert", ".alert--info", ".alert--success", ".alert--error", ".alert--warning",
        ".nav__list", ".nav__link",
        ".skip-link", ".visually-hidden",
    ):
        assert selector in RULES, f"{selector} is emitted by a macro but never styled"


def test_the_uzh_logo_is_vendored_and_shown():
    logo = STATIC / "uzh_logo.svg"
    assert logo.is_file()
    assert "<svg" in logo.read_text(encoding="utf-8")[:200]

    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "uzh_logo.svg" in base


def test_the_logo_is_marked_decorative():
    """The brand link already carries the app name in text.

    Giving the logo alt text as well would make a screen reader announce the
    same link twice over.
    """
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'alt=""' in base


def test_nothing_is_loaded_from_a_cdn():
    """Everything ships with the app (AGENTS.md).

    A remote font or stylesheet would also leak every visitor's IP to whoever
    hosts it, which for a public university page is a privacy question, not
    just an availability one.
    """
    assert "http://" not in RULES
    assert "https://" not in RULES
    assert "url(" in RULES and "url(\"fonts/" in RULES  # the fonts are local

    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', text):
            raise AssertionError(f"{template.name} loads {match} from the network")


def test_focus_stays_visible():
    """The one place we knowingly deviate from the framework.

    UZH CD 2.10.0 sets `:focus-visible { outline: none !important }` and relies
    on per-component focus styling we do not ship. Dropping the outline without
    replacing it would strand keyboard users — WCAG 2.4.7 — and no automated
    checker would notice, so this is pinned deliberately.
    """
    assert "outline: none !important" not in RULES
    focus = re.search(r":focus-visible\s*\{([^}]*)\}", RULES)
    assert focus and "outline:" in focus.group(1)
    assert "var(--c-blue)" in focus.group(1)
