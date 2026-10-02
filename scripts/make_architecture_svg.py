"""Generate the architecture and flow diagram for the README.

    python scripts/make_architecture_svg.py     # writes docs/architecture.svg

Four things this draws that an earlier version of the same picture did not,
each of them a claim the repository has to be able to keep:

**Adapters that do not exist are drawn as not existing.** Stripe, Zuora, Jev
and Vertex are dashed and labelled `roadmap`. The README has a section naming
them as absent; a diagram that lists them beside the working ones says the
opposite, and a picture is believed faster than a paragraph.

**`ActionPolicy` is not in the ports band.** It is a rule, not a boundary --
no Protocol, no interchangeable implementation. It belongs where it already
is, inside the investigation, and putting it between two real ports makes a
reader doubt those too.

**The injection path is on the picture.** `decision engine (if needed)` hints
at a short circuit without saying why one exists. The strongest claim this
project makes is that a screened message never reaches a model, and it cannot
be the one thing the diagram leaves out.

**The audit arrow points one way.** The application writes the audit; the
ports do not.
"""

from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1080, 828

INK = "#1f2328"
MUTED = "#6e7781"
DIM = "#d8dee4"
LANE = "#aab2bb"
CARD = "#fbfcfd"
PANEL = "#f4f7fa"
TEAL_D = "#004D40"
CRIMSON = "#C2185B"
SLATE = "#57606a"
GREEN = "#1a7f37"
BLUE = "#0969da"
AMBER = "#9a6700"

out: list[str] = []


def add(markup: str) -> None:
    out.append(markup)


def box(x, y, w, h, title, sub="", accent=DIM, ink=INK, dash=False, fill="#ffffff"):
    stroke = ' stroke-dasharray="5 4"' if dash else ""
    add(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" '
        f'stroke="{accent}" stroke-width="1.5"{stroke}/>'
    )
    cx = x + w / 2
    if sub:
        add(f'<text class="bt" x="{cx}" y="{y + h / 2 - 3}" fill="{ink}">{escape(title)}</text>')
        add(f'<text class="bs" x="{cx}" y="{y + h / 2 + 16}" fill="{MUTED}">{escape(sub)}</text>')
    else:
        add(f'<text class="bt" x="{cx}" y="{y + h / 2 + 5}" fill="{ink}">{escape(title)}</text>')


def arrow(x1, y1, x2, y2, colour=LANE, width=1.8):
    add(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
        f'stroke-width="{width}" marker-end="url(#a{colour[1:]})"/>'
    )


def step(n, label, x, y):
    add(f'<text class="step l" x="{x}" y="{y}" fill="{MUTED}">{escape(f"{n}. {label}")}</text>')


# ------------------------------------------------------------------ header
add(f'<text class="h1" x="{W / 2}" y="40" fill="{INK}">ResolveBilling</text>')
add(
    f'<text class="h2" x="{W / 2}" y="65" fill="{MUTED}">'
    "One dispute, end to end: what code settles, what a model reads, "
    "and what a person signs for</text>"
)

# ------------------------------------------------- row 1: intake and investigation
Y1, BH = 132, 74
step(1, "Arrives", 40, 110)
step(2, "Orchestrate", 205, 110)
step(3, "Entry point", 370, 110)
step(4, "Investigate", 535, 110)

box(40, Y1, 145, BH, "Dispute", "webhook or mail")
box(205, Y1, 145, BH, "n8n", "trigger, notify")
box(370, Y1, 145, BH, "Billing API", "FastAPI")

arrow(185, Y1 + BH / 2, 205, Y1 + BH / 2)
arrow(350, Y1 + BH / 2, 370, Y1 + BH / 2)
arrow(515, Y1 + BH / 2, 535, Y1 + BH / 2)

# the investigator, with its four steps inside
IX, IW = 535, 505
add(
    f'<rect x="{IX}" y="{Y1 - 26}" width="{IW}" height="{BH + 44}" rx="11" '
    f'fill="{PANEL}" stroke="{TEAL_D}" stroke-width="1.5"/>'
)
add(
    f'<text class="lane l" x="{IX + 16}" y="{Y1 - 9}" fill="{TEAL_D}" '
    '>DISPUTEINVESTIGATOR &#183; THE APPLICATION LAYER</text>'
)

inner = [
    ("Load context", "once, in full"),
    ("Rules", "5 checks, 0 tokens"),
    ("Decision engine", "1 call, typed"),
    ("Action policy", "authorises"),
]
iw, gap = 112, 11
for i, (t, s) in enumerate(inner):
    x = IX + 16 + i * (iw + gap)
    accent = CRIMSON if i == 2 else TEAL_D
    box(x, Y1 + 4, iw, BH - 8, t, s, accent=DIM if i == 2 else TEAL_D,
        ink=MUTED if i == 2 else INK, dash=(i == 2))
    if i:
        arrow(x - gap - 1, Y1 + BH / 2, x - 2, Y1 + BH / 2, TEAL_D)

# correction 3: the injection bypass, drawn rather than implied
bx1 = IX + 16 + 1 * (iw + gap) + iw
bx2 = IX + 16 + 3 * (iw + gap)
add(
    f'<path d="M {bx1} {Y1 + 12} C {bx1 + 30} {Y1 - 34}, {bx2 - 30} {Y1 - 34}, {bx2} {Y1 + 12}" '
    f'fill="none" stroke="{CRIMSON}" stroke-width="2" marker-end="url(#a{CRIMSON[1:]})"/>'
)
add(
    f'<text class="tiny" x="{(bx1 + bx2) / 2}" y="{Y1 - 38}" fill="{CRIMSON}">'
    "prompt injection &#8594; the engine is never asked</text>"
)

# ------------------------------------------------------------- row 2: outcomes
Y2 = 268
step(5, "Outcome", 40, Y2 - 12)
outcomes = [
    ("Automatic reply", "no money moves", SLATE),
    ("Ask the customer", "which invoice is unclear", BLUE),
    ("Financial action proposed", "nothing has happened yet", AMBER),
]
ow, ogap = 320, 30
for i, (t, s, colour) in enumerate(outcomes):
    x = 30 + i * (ow + ogap)
    box(x, Y2, ow, 62, t, s, accent=colour, ink=colour)
    arrow(IX + IW / 2, Y1 + BH + 20, x + ow / 2, Y2 - 4, colour if i == 2 else LANE)

# --------------------------------------------------------------- row 3: the act
Y3 = 388
step(6, "Act, only after a person says so", 42, Y3 - 12)
acts = [
    ("Human approval", "on a page we serve", TEAL_D),
    ("Execute once", "derived idempotency key", TEAL_D),
    ("Customer notified", "", SLATE),
    ("99,00 € credit note", "provider ref cn_001", GREEN),
]
aw, agap = 240, 22
for i, (t, s, colour) in enumerate(acts):
    x = 42 + i * (aw + agap)
    box(x, Y3, aw, 62, t, s, accent=colour, ink=colour)
    if i:
        arrow(x - agap - 1, Y3 + 31, x - 2, Y3 + 31, TEAL_D)
arrow(30 + 2 * (ow + ogap) + ow / 2, Y2 + 62, 42 + aw / 2, Y3 - 4, AMBER)

# ------------------------------------------------- correction 4: audit, one way
Y4 = 480
add(
    f'<rect x="42" y="{Y4}" width="{W - 84}" height="52" rx="9" fill="#ffffff" '
    f'stroke="{DIM}" stroke-width="1.5"/>'
)
add(
    f'<text class="bt l" x="72" y="{Y4 + 31}" fill="{INK}">'
    "Audit + state</text>"
)
add(
    f'<text class="bs l" x="212" y="{Y4 + 31}" fill="{MUTED}">'
    "runs &#183; events &#183; pending actions &#183; SQLite "
    "&#8212; traceable, reproducible, attributable</text>"
)
arrow(IX + IW / 2, Y1 + BH + 20, W - 130, Y4 - 4)
add(
    f'<text class="tiny r" x="{W - 42}" y="{Y4 - 12}" fill="{LANE}">'
    "written by the application, one way</text>"
)

# ------------------------------------------------------------- the ports band
Y5 = 560
add(
    f'<rect x="30" y="{Y5 - 34}" width="{W - 60}" height="182" rx="11" '
    f'fill="{PANEL}" stroke="{DIM}" stroke-width="1.5"/>'
)
add(
    f'<text class="band" x="{W / 2}" y="{Y5 - 12}" fill="{INK}">'
    "The application depends on contracts, not on vendors</text>"
)

ports = [
    ("BillingProvider", ["FakeBillingProvider"], ["Stripe", "Zuora", "OpenCell"]),
    ("DecisionEngine", ["Mock", "Claude"], ["Jev", "Vertex"]),
    ("UsageReport", ["File export"], ["Admin API*"]),
]
pw, pgap = 314, 24
for i, (name, built, planned) in enumerate(ports):
    x = 45 + i * (pw + pgap)
    add(
        f'<rect x="{x}" y="{Y5}" width="{pw}" height="132" rx="9" fill="#ffffff" '
        f'stroke="{TEAL_D}" stroke-width="1.5"/>'
    )
    add(f'<text class="pt" x="{x + pw / 2}" y="{Y5 + 26}" fill="{TEAL_D}">{escape(name)}</text>')
    add(
        f'<text class="tiny l" x="{x + 14}" y="{Y5 + 48}" fill="{MUTED}">built</text>'
    )
    cx = x + 14
    for label in built:
        w = 11 + len(label) * 7.0
        add(
            f'<rect x="{cx}" y="{Y5 + 54}" width="{w}" height="23" rx="5" '
            f'fill="#eef6f4" stroke="{TEAL_D}" stroke-width="1"/>'
        )
        add(
            f'<text class="chip" x="{cx + w / 2}" y="{Y5 + 70}" '
            f'fill="{TEAL_D}">{escape(label)}</text>'
        )
        cx += w + 7
    # correction 1: what does not exist is drawn as not existing
    add(
        f'<text class="tiny l" x="{x + 14}" y="{Y5 + 94}" fill="{LANE}">'
        'roadmap &#8212; not implemented</text>'
    )
    cx = x + 14
    for label in planned:
        w = 11 + len(label) * 6.6
        add(
            f'<rect x="{cx}" y="{Y5 + 102}" width="{w}" height="21" rx="5" fill="none" '
            f'stroke="{LANE}" stroke-width="1" stroke-dasharray="4 3"/>'
        )
        add(
            f'<text class="chip" x="{cx + w / 2}" y="{Y5 + 117}" '
            f'fill="{LANE}">{escape(label)}</text>'
        )
        cx += w + 6

add(
    f'<text class="tiny r" x="{W - 42}" y="{Y5 + 172}" fill="{LANE}">'
    "* written against the documented shape, never run: "
    "the endpoint rejects workspace-scoped keys</text>"
)

# --------------------------------------------------------------- principles
Y6 = 762
principles = [
    ("Deterministic before probabilistic", TEAL_D),
    ("A person signs for money", CRIMSON),
    ("Provider and engine interchangeable", SLATE),
]
prw, prgap = 320, 30
for i, (label, colour) in enumerate(principles):
    x = 30 + i * (prw + prgap)
    add(
        f'<rect x="{x}" y="{Y6}" width="{prw}" height="42" rx="21" fill="#ffffff" '
        f'stroke="{colour}" stroke-width="1.4"/>'
    )
    add(f'<text class="pr" x="{x + prw / 2}" y="{Y6 + 27}" fill="{colour}">{escape(label)}</text>')

# ------------------------------------------------------------------- assemble
markers = "".join(
    f'<marker id="a{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
    f'markerHeight="6" orient="auto-start-reverse">'
    f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{c}"/></marker>'
    for c in (LANE, TEAL_D, CRIMSON, AMBER, SLATE, BLUE, GREEN)
)

ALT = (
    "Architecture and flow of the billing exception handler: a dispute arrives "
    "through n8n, the application loads the billing context, runs deterministic "
    "rules, asks a decision engine only when the rules cannot settle it, and an "
    "action policy decides whether a person must approve. A prompt injection "
    "bypasses the engine entirely. Billing providers, decision engines and usage "
    "reports sit behind ports, with unimplemented adapters marked roadmap."
)

style = """
    text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
         Helvetica,Arial,sans-serif;text-anchor:middle}
    .h1{font-size:23px;font-weight:600}
    .h2{font-size:13.5px}
    .l{text-anchor:start}
    .r{text-anchor:end}
    .step{font-size:12px;font-weight:600}
    .bt{font-size:14px;font-weight:600}
    .bs{font-size:11px}
    .lane{font-size:10px;font-weight:600;letter-spacing:.09em}
    .band{font-size:15px;font-weight:600}
    .pt{font-size:15px;font-weight:600}
    .chip{font-size:11px;font-weight:600}
    .tiny{font-size:10.5px}
    .pr{font-size:13px;font-weight:600}
"""

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"
     width="{W}" height="{H}" role="img" aria-label="{ALT}">
  <style>{style}</style>
  <defs>{markers}</defs>
  <rect width="{W}" height="{H}" rx="12" fill="{CARD}" stroke="{DIM}"/>
  {"".join(out)}
</svg>
"""

path = Path("docs/architecture.svg")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(svg, encoding="utf-8")
print(f"{path} — {len(svg) / 1024:.1f} Ko")
