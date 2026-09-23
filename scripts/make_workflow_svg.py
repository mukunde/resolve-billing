"""Generate the animated workflow SVG for the README.

    python scripts/make_workflow_svg.py      # writes docs/workflow.svg

Timings are declared in seconds and the CSS keyframe percentages are computed,
because hand-written percentages make every retiming a rewrite.

Three scenarios share one pipeline and one master loop:
  phase A  a duplicate, money on the line, a human approves
  phase B  a correct invoice, nothing proposed
  phase F  an injected instruction, the engine is never called
"""

from pathlib import Path
from xml.sax.saxutils import escape

W, H = 980, 448
MASTER = 24.0
PHASE = 8.0
PHASES = {"A": 0.0, "B": 8.0, "F": 16.0}

INK = "#1f2328"
MUTED = "#6e7781"
DIM = "#d8dee4"
LANE = "#aab2bb"
CARD = "#fbfcfd"
TEAL = "#00695C"
TEAL_D = "#004D40"
CRIMSON = "#C2185B"
SLATE = "#57606a"

BOX_W, BOX_H, BOX_Y = 160, 62, 150
BOX_X = [50, 230, 410, 590, 770]

OUT_W, OUT_H, OUT_Y = 240, 62, 298
OUT_X = [95, 370, 645]

STAGES = [
    ("Dispute in", "n8n webhook"),
    ("Extraction", "regex, 0 tokens"),
    ("Context + rules", "one load, 5 checks"),
    ("Decision engine", "1 call, typed schema"),
    ("Action policy", "authorises, or not"),
]

OUTCOMES = [
    ("Automatic reply", "no money moves", SLATE),
    ("Approval e-mail", "a human decides, then a credit note", TEAL),
    ("Alert, no buttons", "nothing to approve", CRIMSON),
]

TITLES = {
    "A": ("Case A — charged twice", "the duplicate is real, so a person signs for the money"),
    "B": (
        "Case B — the invoice is correct",
        "two debits, both legitimate: the agent proposes nothing",
    ),
    "F": ("Case F — an injected instruction", "screened by a rule, before any model sees it"),
}

CAPTIONS = {
    "A": "DUPLICATE_INVOICE → credit_note 99,00 € → pending_approval → executed",
    "B": "no finding → valid_invoice → verdict auto → nothing is queued",
    "F": "SUSPECTED_PROMPT_INJECTION → decision_engine_skipped → no decision_created",
}


def pct(seconds: float) -> float:
    return max(0.0, min(100.0, seconds / MASTER * 100))


def keyframes(name: str, windows, ramp: float = 0.35) -> str:
    """Opacity keyframes: 0 outside every window, 1 inside, with a short ramp."""
    stops: dict[float, float] = {0.0: 0.0, 100.0: 0.0}
    for start, end in windows:
        stops[pct(start)] = 0.0
        stops[pct(start + ramp)] = 1.0
        stops[pct(end)] = 1.0
        stops[pct(end + ramp)] = 0.0
    body = "".join(
        f"{p:.3f}%{{opacity:{v}}}" for p, v in sorted(stops.items())
    )
    return f"@keyframes {name}{{{body}}}"


def draw_keyframes(name: str, windows, length: float, ramp: float = 0.55) -> str:
    """Stroke-dashoffset keyframes: a connector that draws itself, then holds."""
    stops: dict[float, float] = {0.0: length, 100.0: length}
    for start, end in windows:
        stops[pct(start)] = length
        stops[pct(start + ramp)] = 0.0
        stops[pct(end)] = 0.0
        stops[pct(end + ramp)] = length
    body = "".join(
        f"{p:.3f}%{{stroke-dashoffset:{v:.1f}}}" for p, v in sorted(stops.items())
    )
    return f"@keyframes {name}{{{body}}}"


# -- when each thing is lit, relative to the start of its phase -------------
LIT = [0.25, 1.05, 1.85, 2.70, 3.75]        # the five pipeline boxes
CONNECT = [0.70, 1.50, 2.30, 3.40]           # the four connectors between them
HOLD = 7.55                                   # everything stays lit until here
BYPASS_AT = 2.30
OUTCOME_AT = 4.70

rules: list[str] = []
body: list[str] = []


def win(phases, start_rel, end_rel=HOLD):
    return [(PHASES[p] + start_rel, PHASES[p] + end_rel) for p in phases]


def box(idx, x, y, w, h, title, sub, phases, start_rel, accent=None):
    """A stage: a dim version always visible, a lit version fading over it."""
    cx = x + w / 2
    accent = accent or TEAL_D
    name = f"b{idx}"
    rules.append(keyframes(name, win(phases, start_rel)))
    return f"""
  <g>
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#fff" stroke="{DIM}"/>
    <text class="t1" x="{cx}" y="{y + 26}" fill="{MUTED}">{escape(title)}</text>
    <text class="t2" x="{cx}" y="{y + 45}" fill="{DIM}">{escape(sub)}</text>
  </g>
  <g style="animation:{name} {MASTER}s linear infinite;opacity:0">
    <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#fff"
          stroke="{accent}" stroke-width="1.8"/>
    <text class="t1" x="{cx}" y="{y + 26}" fill="{accent}">{escape(title)}</text>
    <text class="t2" x="{cx}" y="{y + 45}" fill="{MUTED}">{escape(sub)}</text>
  </g>"""


def connector(idx, x1, x2, y, phases, start_rel, accent=TEAL_D):
    length = x2 - x1
    name = f"c{idx}"
    rules.append(draw_keyframes(name, win(phases, start_rel), length))
    return f"""
  <line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{DIM}" stroke-width="2"/>
  <line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{accent}" stroke-width="2.4"
        stroke-dasharray="{length}" stroke-dashoffset="{length}"
        style="animation:{name} {MASTER}s linear infinite"/>"""


# ---------------------------------------------------------------- pipeline
mid = BOX_Y + BOX_H / 2
for i, (title, sub) in enumerate(STAGES):
    # The engine box is lit only when it actually runs.
    phases = ["A", "B"] if i == 3 else ["A", "B", "F"]
    accent = TEAL_D
    body.append(box(i, BOX_X[i], BOX_Y, BOX_W, BOX_H, title, sub, phases, LIT[i], accent))

for i in range(4):
    x1 = BOX_X[i] + BOX_W
    x2 = BOX_X[i + 1]
    # The connector into and out of the engine is dead on the injection path.
    phases = ["A", "B"] if i in (2, 3) else ["A", "B", "F"]
    body.append(connector(i, x1, x2, mid, phases, CONNECT[i]))

# ---------------------------------------------------------------- bypass arc
arc_x1, arc_x2 = BOX_X[2] + BOX_W, BOX_X[4]
arc = f"M {arc_x1} {mid - 24} C {arc_x1 + 42} 92, {arc_x2 - 42} 92, {arc_x2} {mid - 24}"
rules.append(draw_keyframes("bypass", win(["F"], BYPASS_AT), 300, ramp=0.7))
rules.append(keyframes("bypasslbl", win(["F"], BYPASS_AT + 0.3)))
body.append(f"""
  <path d="{arc}" fill="none" stroke="{CRIMSON}" stroke-width="2.4"
        stroke-dasharray="300" stroke-dashoffset="300"
        style="animation:bypass {MASTER}s linear infinite"/>
  <text class="t3" x="{(arc_x1 + arc_x2) / 2}" y="86" fill="{CRIMSON}"
        style="animation:bypasslbl {MASTER}s linear infinite;opacity:0"
  >the engine is never asked</text>""")

# ---------------------------------------------------------------- outcomes
# B -> automatic reply, A -> approval, F -> alert
for i, (title, sub, colour) in enumerate(OUTCOMES):
    phase = ["B", "A", "F"][i]
    body.append(
        box(10 + i, OUT_X[i], OUT_Y, OUT_W, OUT_H, title, sub, [phase], OUTCOME_AT, colour)
    )
    # a short elbow from the policy box down to this outcome
    sx = BOX_X[4] + BOX_W / 2
    sy = BOX_Y + BOX_H
    tx = OUT_X[i] + OUT_W / 2
    path = f"M {sx} {sy} L {sx} {(sy + OUT_Y) / 2} L {tx} {(sy + OUT_Y) / 2} L {tx} {OUT_Y}"
    length = (OUT_Y - sy) + abs(tx - sx) + 20
    rules.append(draw_keyframes(f"e{i}", win([phase], OUTCOME_AT - 0.5), length))
    body.append(f"""
  <path d="{path}" fill="none" stroke="{DIM}" stroke-width="2"/>
  <path d="{path}" fill="none" stroke="{colour}" stroke-width="2.4"
        stroke-dasharray="{length}" stroke-dashoffset="{length}"
        style="animation:e{i} {MASTER}s linear infinite"/>""")

# ---------------------------------------------------------------- captions
for phase, (title, sub) in TITLES.items():
    colour = CRIMSON if phase == "F" else TEAL_D
    rules.append(keyframes(f"h{phase}", [(PHASES[phase] + 0.1, PHASES[phase] + HOLD)]))
    body.append(f"""
  <g style="animation:h{phase} {MASTER}s linear infinite;opacity:0">
    <text class="h1" x="{W / 2}" y="36" fill="{colour}">{escape(title)}</text>
    <text class="h2" x="{W / 2}" y="58" fill="{MUTED}">{escape(sub)}</text>
    <text class="mono" x="{W / 2}" y="{H - 22}"
          fill="{MUTED}">{escape(CAPTIONS[phase])}</text>
  </g>""")

# ---------------------------------------------------------------- lane labels
body.append(f"""
  <text class="lane l" x="{BOX_X[0]}" y="{BOX_Y - 14}"
        fill="{LANE}">ORCHESTRATION — HOLDS NO STATE</text>
  <text class="lane r" x="{BOX_X[4] + BOX_W}" y="{BOX_Y - 14}"
        fill="{LANE}">DETERMINISTIC BEFORE PROBABILISTIC</text>""")

style = f"""
    text{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,
         Helvetica,Arial,sans-serif;text-anchor:middle}}
    .h1{{font-size:19px;font-weight:600}}
    .h2{{font-size:14px}}
    .t1{{font-size:15px;font-weight:600}}
    .t2{{font-size:11.5px}}
    .t3{{font-size:12.5px;font-weight:600}}
    .lane{{font-size:10px;font-weight:600;letter-spacing:.11em}}
    .l{{text-anchor:start}}
    .r{{text-anchor:end}}
    .l{{text-anchor:start}}
    .r{{text-anchor:end}}
    .mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}}
    {''.join(rules)}
"""

ALT = (
    "One dispute, three outcomes: a duplicate goes to a human for approval, "
    "a correct invoice proposes nothing, and an injected instruction stops "
    "before the decision engine is ever called."
)

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"
     width="{W}" height="{H}" role="img" aria-label="{ALT}">
  <style>{style}</style>
  <rect width="{W}" height="{H}" rx="12" fill="{CARD}" stroke="{DIM}"/>
  {''.join(body)}
</svg>
"""

out = Path("docs/workflow.svg")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(svg, encoding="utf-8")
print(f"{out} — {len(svg) / 1024:.1f} Ko, {len(rules)} animations, boucle {MASTER:.0f} s")
