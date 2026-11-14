"""Invoice numbers and ids found in free text (specs/014-pennylane-read, FR-010;
specs/016-bank-evidence).

Moved from Pennylane's mapper: the recogniser knows invoices, not a platform, so a
customer's message and a bank transfer's reference are read the same way everywhere.
Whole tokens, compared case-folded in full (ß as ss), bounded by a character that is
neither a letter nor a digit in any script, the longest kept first where two overlap,
never overlapping; the spans are the original text's.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.models import Invoice


def _numbers(invoices: Sequence[Invoice]) -> dict[str, str]:
    """Each number, case-folded, to the one invoice that carries it; a number
    two invoices share is left out: it designates neither."""
    by_number: dict[str, set[str]] = {}
    for item in invoices:
        if item.number:
            by_number.setdefault(item.number.casefold(), set()).add(item.id)
    return {n: next(iter(ids)) for n, ids in by_number.items() if len(ids) == 1}


def _tokens(invoices: Sequence[Invoice]) -> dict[str, str]:
    """Numbers as above, and each invoice's own id, case-folded."""
    tokens = _numbers(invoices)
    for item in invoices:
        tokens.setdefault(item.id.casefold(), item.id)
    return tokens


def resolve_number(reference: str, invoices: Sequence[Invoice]) -> str | None:
    """The invoice whose whole number `reference` is, case-folded."""
    return _numbers(invoices).get(reference.strip().casefold())


def _folded(text: str) -> tuple[str, list[int]]:
    """`text` case-folded in full (ß as ss), and for each folded character the
    index of the original character it came from."""
    parts: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = char.casefold()
        parts.append(folded)
        origin.extend([index] * len(folded))
    return "".join(parts), origin


def _spans(text: str, known: dict[str, str]) -> list[tuple[int, int, str]]:
    if not known or not text:
        return []
    folded, origin = _folded(text)
    candidates: list[tuple[int, int, str]] = []
    for token, invoice_id in known.items():
        at = folded.find(token)
        while at != -1:
            end = at + len(token)
            whole = (at == 0 or origin[at - 1] != origin[at]) and (
                end == len(folded) or origin[end] != origin[end - 1]
            )
            if whole:
                start, stop = origin[at], origin[end - 1] + 1
                before = start == 0 or not text[start - 1].isalnum()
                after = stop == len(text) or not text[stop].isalnum()
                if before and after:
                    candidates.append((start, stop, invoice_id))
            at = folded.find(token, at + 1)
    chosen: list[tuple[int, int, str]] = []
    for start, stop, invoice_id in sorted(candidates, key=lambda c: (c[0] - c[1], c[0])):
        if all(stop <= s or start >= e for s, e, _ in chosen):
            chosen.append((start, stop, invoice_id))
    return sorted(chosen)


def references(text: str, invoices: Sequence[Invoice]) -> list[tuple[int, int, str]]:
    """Where the printed numbers of `invoices` sit in `text`, and the invoice each
    stands for."""
    return _spans(text, _numbers(invoices))


def cited_invoices(text: str | None, invoices: Sequence[Invoice]) -> set[str]:
    """The loaded invoices a text cites, by printed number or by id -- only those:
    a token naming an invoice that is not loaded is not recognised."""
    if not text:
        return set()
    return {invoice_id for _, _, invoice_id in _spans(text, _tokens(invoices))}


def merge_spans(*groups: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Spans from several recognisers, the longest kept first where two overlap."""
    chosen: list[tuple[int, int, str]] = []
    every = [span for group in groups for span in group]
    for start, stop, ref in sorted(every, key=lambda c: (c[0] - c[1], c[0])):
        if all(stop <= s or start >= e for s, e, _ in chosen):
            chosen.append((start, stop, ref))
    return sorted(chosen)
