"""Generate the landscape diagram for the README: where ResolveBilling sits among the tools a
company already runs.

    python scripts/make_landscape_svg.py     # writes docs/landscape.svg, syncs the README

A customer's dispute reaches ResolveBilling through n8n's webhook today -- or, once spec 017
lands, through the company's support tool; ResolveBilling investigates it between the
billing platform, the bank and a model provider; n8n orchestrates the notifications, the wait
for a person and the request to execute; a person approves every movement of money.

**What exists is drawn as existing, what does not as not.** Solid: built and tested (in test
mode or on a sandbox where it is a real platform). Dashed: being built (Zammad, spec 017) or
a target (Zendesk). Arrows follow the direction of the call. Brand marks come from Simple
Icons (CC0), in `docs/brand-icons/`; a brand without one there is a plain name. The README's
alternative text is written from `ALT` below, so the two never drift.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "docs" / "brand-icons"
OUT = ROOT / "docs" / "landscape.svg"
README = ROOT / "README.md"

W, H = 1000, 590
INK = "#1f2328"
MUTED = "#6e7781"
LANE = "#aab2bb"
DIM = "#d8dee4"
CARD = "#ffffff"
BAND = "#f4f7fa"
TEAL = "#004D40"
CRIMSON = "#C2185B"

#: Brand colours, as Simple Icons gives them.
BRANDS = {
    "n8n": "#EA4B71",
    "stripe": "#635BFF",
    "revolut": "#191C1F",
    "anthropic": "#191919",
    "zendesk": "#03363D",
}

ALT = (
    "Where ResolveBilling sits. Today a customer's dispute arrives through n8n's webhook; "
    "a company's support tool is planned as the other way in (spec 017, in progress), "
    "by a signed webhook (Zammad; Zendesk a target), "
    "drawn dashed. ResolveBilling runs deterministic rules, asks a decision engine through a "
    "model provider -- Anthropic's Claude; OpenAI on synthetic data only -- with the masked "
    "text, a pseudonymised context and finding codes, computes the amount and its policy, "
    "and a person approves. It reads the billing platform (Stripe, Pennylane), the authority "
    "on amounts, and writes there only once approved and only where writes are enabled; it "
    "reads the bank (Revolut) as evidence only. n8n orchestrates the notifications, the wait "
    "for a person and the request to execute. Every run that completes is audited. Solid: "
    "built and tested, in test mode or on a sandbox for real platforms; dashed: being built "
    "or a target."
)

parts: list[str] = []


def add(fragment: str) -> None:
    parts.append(fragment)


def icon(slug: str, x: float, y: float, size: float = 20) -> None:
    svg = (ICONS / f"{slug}.svg").read_text(encoding="utf-8")
    path = re.search(r'<path d="([^"]+)"', svg).group(1)
    add(f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 24 24">'
        f'<path d="{path}" fill="{BRANDS[slug]}"/></svg>')


def box(x, y, w, h, *, stroke=DIM, dashed=False, fill=CARD, rx=10):
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.5"{dash}/>')


def text(x, y, value, *, cls="t", fill=INK, anchor="middle"):
    add(f'<text class="{cls}" x="{x}" y="{y}" fill="{fill}" text-anchor="{anchor}">'
        f'{escape(value)}</text>')


def arrow(x1, y1, x2, y2, *, colour=LANE, dashed=False):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    marker = {LANE: "head", TEAL: "head-teal", CRIMSON: "head-crimson"}[colour]
    add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
        f'stroke-width="1.6"{dash} marker-end="url(#{marker})"/>')


def chip(x, y, slug, name, *, dashed=False):
    width = 18 + len(name) * 6.6 + (22 if slug else 0)
    box(x, y, width, 26, stroke=LANE if dashed else DIM, dashed=dashed, rx=13)
    tx = x + 10
    if slug:
        icon(slug, tx, y + 4, 18)
        tx += 22
    text(tx, y + 17, name, cls="c", anchor="start", fill=MUTED if dashed else INK)
    return width


# -- frame --------------------------------------------------------------------------
add(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(W / 2, 34, "Where ResolveBilling sits", cls="h1")
text(W / 2, 56, "a dispute in, the billing platform, the bank and a model around it -- "
     "a person signs for every movement of money", cls="h2", fill=MUTED)

# -- the customer, n8n (today's intake) and the support tool (being built) ----------
box(30, 100, 140, 66)
text(100, 128, "Customer")
text(100, 148, "writes about an invoice", cls="s", fill=MUTED)

box(300, 84, 400, 82)
icon("n8n", 316, 100, 22)
text(346, 116, "n8n", anchor="start")
text(346, 136, "today's intake (webhook); notifications, the wait", cls="s",
     fill=MUTED, anchor="start")
text(346, 152, "for a person, the request to execute", cls="s", fill=MUTED, anchor="start")
arrow(170, 128, 298, 128)
text(234, 120, "today", cls="s", fill=MUTED)

box(30, 330, 190, 116, stroke=LANE, dashed=True)
text(125, 355, "Support tool")
text(125, 373, "target: the case lives here", cls="s", fill=MUTED)
chip(42, 384, None, "Zammad (being built)", dashed=True)
chip(42, 414, "zendesk", "Zendesk (target)", dashed=True)
arrow(100, 166, 100, 328, dashed=True)
text(108, 250, "ticket", cls="s", fill=MUTED, anchor="start")

# -- ResolveBilling (centre) -------------------------------------------------------
box(250, 196, 500, 214, stroke=TEAL, fill=BAND, rx=14)
text(500, 220, "ResolveBilling", cls="h3", fill=TEAL)
steps = [("Deterministic", "rules", TEAL), ("Decision", "engine", TEAL),
         ("Amount and", "policy", TEAL), ("A person", "approves", CRIMSON)]
for i, (top, bottom, colour) in enumerate(steps):
    x = 266 + i * 120
    box(x, 242, 108, 66, stroke=colour)
    text(x + 54, 270, top, cls="b")
    text(x + 54, 289, bottom, cls="b")
    if i:
        arrow(x - 12, 275, x - 2, 275, colour=TEAL)
text(500, 334, "rules before any model; the model names a kind of action, code computes the",
     cls="s", fill=MUTED)
text(500, 350, "amount; no money moves without an authenticated person", cls="s", fill=MUTED)
box(266, 366, 468, 30, stroke=DIM, rx=8)
text(500, 386, "Audit trail: every run that completes, its findings, decisions, approvals",
     cls="s")

arrow(470, 166, 470, 194)
arrow(530, 194, 530, 168)
arrow(220, 388, 248, 360, dashed=True)

# -- the model provider (bottom) ---------------------------------------------------
box(330, 456, 340, 90)
text(500, 480, "Model provider")
text(500, 498, "masked text, pseudonymised context, finding codes", cls="s", fill=MUTED)
w = chip(350, 510, "anthropic", "Anthropic (Claude)")
chip(350 + w + 8, 510, None, "OpenAI (synthetic data only)")
arrow(440, 410, 440, 454)

# -- billing platform and bank (right) ---------------------------------------------
box(820, 196, 160, 134)
text(900, 220, "Billing platform")
text(900, 238, "the authority on amounts", cls="s", fill=MUTED)
chip(832, 254, "stripe", "Stripe")
chip(832, 288, None, "Pennylane")
arrow(750, 242, 818, 242, colour=TEAL)
text(784, 234, "reads", cls="s", fill=MUTED)
arrow(750, 280, 818, 280, colour=CRIMSON)
text(784, 298, "writes, once", cls="s", fill=MUTED)
text(784, 311, "approved, if", cls="s", fill=MUTED)
text(784, 324, "enabled", cls="s", fill=MUTED)

box(820, 350, 160, 90)
text(900, 374, "Bank")
text(900, 392, "evidence of a payment", cls="s", fill=MUTED)
chip(856, 404, "revolut", "Revolut")
arrow(750, 372, 818, 395)
text(784, 412, "read only", cls="s", fill=MUTED)

# -- legend ------------------------------------------------------------------------
text(30, 572, "Solid: built and tested (test mode or sandbox for real platforms). Dashed: "
     "being built or a target. Arrows: the direction of the call. Marks: Simple Icons (CC0), "
     "their owners' trademarks; no partnership implied.", cls="l", fill=MUTED, anchor="start")

STYLE = """<style>
.h1{font:600 22px system-ui,-apple-system,Segoe UI,sans-serif}
.h2{font:400 13px system-ui,-apple-system,Segoe UI,sans-serif}
.h3{font:600 16px system-ui,-apple-system,Segoe UI,sans-serif}
.t{font:600 14px system-ui,-apple-system,Segoe UI,sans-serif}
.b{font:600 12.5px system-ui,-apple-system,Segoe UI,sans-serif}
.s{font:400 11.5px system-ui,-apple-system,Segoe UI,sans-serif}
.c{font:500 12px system-ui,-apple-system,Segoe UI,sans-serif}
.l{font:400 10.5px system-ui,-apple-system,Segoe UI,sans-serif}
</style>"""


def marker(name: str, colour: str) -> str:
    return (f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
            f'markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
            f'fill="{colour}"/></marker>')


def main() -> None:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}" role="img" aria-label="{escape(ALT)}">'
           f'<defs>{marker("head", LANE)}{marker("head-teal", TEAL)}'
           f'{marker("head-crimson", CRIMSON)}</defs>{STYLE}{"".join(parts)}</svg>\n')
    OUT.write_text(svg, encoding="utf-8")
    # The README's image line takes the same text.
    readme = README.read_bytes().decode("utf-8")
    line = re.compile(r"!\[[^\]]*\]\(docs/landscape\.svg\)")
    synced = "README image line not found"
    if line.search(readme):
        readme = line.sub(lambda _: f"![{ALT}](docs/landscape.svg)", readme, count=1)
        README.write_bytes(readme.encode("utf-8"))
        synced = "README alt text synced"
    print(f"{OUT.relative_to(ROOT)} · {len(svg) / 1024:.1f} KB; {synced}")


if __name__ == "__main__":
    main()
