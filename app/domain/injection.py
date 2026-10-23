"""Prompt-injection screening on the raw customer message.

The dispute message is untrusted input (see the threat model). This check runs
*before* any decision engine is called, and a hit forces human review through
the action policy -- the model is never given the chance to be talked into a
refund.

Two deliberate design choices:

**Patterns, not a model.** Screening untrusted input with a model is asking the
thing under attack to referee the attack. A pattern list is cheap, instant,
inspectable and testable, which is the whole argument of ADR-003.

**Tuned for recall, not precision.** A false positive costs one human review.
A false negative costs a financial mutation. The asymmetry is roughly four
orders of magnitude, so the patterns lean towards flagging -- see
``vous etes autorise`` in :data:`PATTERNS`, which catches an indignant customer
questioning the company's right to bill as well as a genuine injection. That
case is tested, and the accepted behaviour is: flag it, let a human look.

What this does *not* flag, on purpose: asking for a refund. "Remboursez-moi
immédiatement" is an ordinary, legitimate, frequent thing for a disputing
customer to write, and treating it as an attack would make the rule useless.
Only attempts to address *the system* are flagged.
"""

from __future__ import annotations

import re
import unicodedata

from app.domain.enums import RuleCode, Severity
from app.domain.models import RuleFinding

#: (category, pattern) pairs, matched against the normalised message.
#:
#: Patterns are written in ASCII lowercase because :func:`normalise` strips
#: accents and case first -- which also defeats accent-based evasion.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(ignore[zr]?|oubli(?:e|ez|er)|neglige[zr]?|disregard|forget|override)\b"
            r"[^.!?\n]{0,50}"
            r"\b(instructions?|consignes?|regles?|prompt|precedent\w*|previous|prior|above)\b"
        ),
    ),
    (
        "role_redefinition",
        re.compile(
            r"\b(tu es|vous etes|you are)\b\s*"
            r"(?:maintenant|desormais|now)?\s*"
            r"(?:un|une|le|la|a|an|the)?\s*"
            # Up to two words before the role: English puts the adjective
            # first ("a refund agent"), which the bare article missed.
            r"(?:[a-z]+\s+){0,2}"
            r"\b(agent|assistant|systeme|system|modele|model|ia|ai|bot|chatbot|robot)\b"
        ),
    ),
    (
        "role_redefinition",
        re.compile(
            r"\b(agis comme|comporte[- ]toi comme|fais comme si tu etais|"
            r"act as|behave as|pretend to be|roleplay)\b"
        ),
    ),
    (
        "authority_claim",
        re.compile(
            r"\b(tu es|vous etes|you are)\b[^.!?\n]{0,30}"
            r"\b(autoris\w*|habilit\w*|authorized|permitted|allowed)\b"
        ),
    ),
    (
        "authority_claim",
        re.compile(
            r"\b(mode (?:developpeur|administrateur|admin|debug)|"
            r"developer mode|admin mode|god mode|sudo)\b"
        ),
    ),
    (
        "prompt_markup",
        re.compile(r"<\|[^|>\n]{0,40}\|>|\[/?inst\]|#{2,}\s*(?:system|instruction)"),
    ),
    (
        "prompt_markup",
        re.compile(r"</?(?:system|assistant|user)>|\b(system prompt|prompt systeme)\b"),
    ),
]


def normalise(message: str) -> str:
    """Lowercase, strip accents, collapse whitespace.

    Evidence offsets refer to this normalised form, not to the original text.
    """
    decomposed = unicodedata.normalize("NFD", message.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped)


#: A customer withdrawing their own earlier message: "ignorez mon message
#: précédent", "please ignore my previous email". Exempt from the override
#: pattern (taxonomy S2) -- and only that phrase, and only in a sentence that
#: names nothing a system obeys.
_OWN_MESSAGE = re.compile(
    r"\b(?:ignore[zr]?|oubli(?:e|ez|er)|disregard|forget)\b\s+"
    r"(?:mon|ma|mes|notre|nos|my|our)\s+"
    r"(?:(?:precedent\w*|dernier\w*|previous|prior|last|earlier)\s+)?"
    r"(?:message|mail|e-?mail|courriel)s?\b"
    r"(?:\s+(?:precedent\w*|d'avant))?"
)

#: What an override reaches for. If any of it is left in the sentence once
#: the exempt phrase is set aside, the phrase is not exempt: "ignore my
#: previous message and everything above" keeps its verb.
_OVERRIDE_OBJECTS = re.compile(
    r"\b(?:instructions?|consignes?|regles?|rules?|prompt|system|systeme|directives?"
    r"|precedent\w*|previous|prior|above|ci-dessus|everything|tout ce qui|context\w*"
    r"|earlier|told|dit)\b"
)


def _without_own_message(normalised: str) -> str:
    """The text with each exempt phrase blanked, positions kept.

    Sentence by sentence, and only when the phrase stands alone: once it is
    set aside, nothing an override reaches for may be left in the sentence
    -- instructions, rules, a prompt, the system, "everything above", what
    came before. "Ignore my previous email and all prior instructions" and
    "ignore my previous message and everything above" are still flagged.
    Every other pattern -- a role, an authority claim, markup -- is matched
    on the same text, where the exempt phrase is the only thing removed.
    """
    sentences = re.split(r"(?<=[.!?;])", normalised)
    kept = []
    for sentence in sentences:
        blanked = _OWN_MESSAGE.sub(lambda m: " " * len(m.group(0)), sentence)
        if blanked != sentence and not _OVERRIDE_OBJECTS.search(blanked):
            sentence = blanked
        kept.append(sentence)
    return "".join(kept)


def suspected_prompt_injection(message: str) -> RuleFinding | None:
    """Flag a dispute message that tries to address the system rather than us."""
    normalised = normalise(message)
    scanned = _without_own_message(normalised)

    matches: list[str] = []
    categories: list[str] = []
    for category, pattern in PATTERNS:
        found = pattern.search(scanned)
        if found is None:
            continue
        matches.append(found.group(0).strip())
        if category not in categories:
            categories.append(category)

    if not matches:
        return None

    return RuleFinding(
        code=RuleCode.SUSPECTED_PROMPT_INJECTION,
        severity=Severity.HIGH,
        description="Le message du litige contient un motif d'instruction adressée au système.",
        evidence={
            "matched": matches,
            "categories": categories,
            "offset": normalised.find(matches[0]),
            "note": (
                "Offsets et extraits portent sur le message normalisé (minuscules, sans accents)."
            ),
        },
    )
