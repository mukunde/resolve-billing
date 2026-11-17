"""The bank check's record, as the run page and the review page show it
(specs/016-bank-evidence, FR-005).

Built from `BankCheck` alone -- the audit's closed allowlist -- so a page can show
nothing the record does not hold: no payer, account number, description or raw
reference ever reaches it. Every value is escaped; the labels are this module's own.
"""

from __future__ import annotations

from app.api.templating import Safe, escape
from app.domain.bank import BankCheck, CheckedCredit, CheckedReturn

OUTCOMES = {
    "observed": (
        "crédit constaté — candidat fondé sur la référence, rien n'est conclu sur la facture"
    ),
    "to_reconcile": "à rapprocher par une personne",
    "not_evaluable": "non évaluable — le contrôle n'a pas pu conclure",
    "nothing": "aucun crédit citant la facture — ce n'est pas une preuve de non-paiement",
    "not_run": "non exécuté",
}

WHY = {
    "no_bank": "aucune banque configurée",
    "no_invoice": "aucune facture désignée",
    "paid_on_platform": "facture déjà payée sur la plateforme",
    "short_circuit": "run arrêté avant le contrôle",
    "read_incomplete": "lecture incomplète",
    "coverage_not_established": "couverture non établie",
    "account_not_visible": "compte déclaré non visible",
    "no_account_in_currency": "aucun compte déclaré dans la devise de la facture",
    "bank_unavailable": "banque indisponible",
}

REASONS = {
    "reference_matched": "la référence cite la facture",
    "amount_equal": "montant égal au total",
    "amount_partial": "montant partiel",
    "amount_over": "montant supérieur au total",
    "currency_differs": "autre devise",
    "reversed": "annulé",
    "several_invoices_cited": "plusieurs factures citées",
    "several_candidates": "plusieurs candidats",
    "fx_present": "change",
    "fee_present": "frais",
    "related_return_found": "retour lié trouvé",
    "origin_unknown": "origine inconnue",
    "origin_internal": "origine interne",
}

LIMITATIONS = {
    "allocation_unverified_beyond_loaded_invoices": (
        "attribution non vérifiée au-delà des factures chargées : le payeur n'est pas "
        "vérifié, une référence peut être erronée"
    ),
    "coverage_declared_accounts_from_issue": (
        "couverture limitée aux comptes déclarés, depuis l'émission de la facture"
    ),
    "later_reversal_not_followed": "une annulation ultérieure n'est pas suivie",
}


def bank_section(check: BankCheck | None) -> Safe:
    """The record as a page section; empty for a run recorded before it existed."""
    if check is None:
        return Safe("")
    rows = [("Issue", OUTCOMES[check.outcome])]
    if check.reason is not None:
        rows.append(("Motif", WHY[check.reason]))
    if check.reasons:
        rows.append(("Raisons", _reasons(check.reasons)))
    if check.invoice_id is not None:
        rows.append(("Facture vérifiée", check.invoice_id))
    if check.window_start is not None and check.window_end is not None:
        margin = f" (marge de lecture : {check.margin_days} jours)" if check.margin_days else ""
        window = f"{check.window_start.isoformat()} → {check.window_end.isoformat()}"
        rows.append(("Fenêtre", window + margin))
    if check.complete is not None:
        rows.append(("Lecture complète", "oui" if check.complete else "non"))
    if check.coverage_established is not None:
        rows.append(("Couverture établie", "oui" if check.coverage_established else "non"))
    if check.accounts:
        rows.append(
            (
                "Comptes déclarés",
                ", ".join(
                    f"{a.id} ({a.currency or 'devise inconnue'}"
                    f"{'' if a.visible else ', non visible'})"
                    for a in check.accounts
                ),
            )
        )
    listing = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in rows)
    items = "".join(_item(item) for item in check.items)
    limits = "".join(f"<li>{escape(LIMITATIONS[x])}</li>" for x in check.limitations)
    return Safe(
        "<section class='bank'><h2>Contrôle bancaire</h2>"
        f"<dl>{listing}</dl>"
        + (f"<ul class='bank-items'>{items}</ul>" if items else "")
        + (f"<p class='bank-limits'>Limites :</p><ul>{limits}</ul>" if limits else "")
        + "</section>"
    )


def _reasons(reasons) -> str:
    return ", ".join(REASONS[r] for r in reasons)


def _item(item: CheckedCredit | CheckedReturn) -> str:
    if isinstance(item, CheckedCredit):
        completed = item.completed_at.isoformat() if item.completed_at else "aucune"
        text = (
            f"Crédit {item.transaction_id} sur le compte {item.account_id} : {item.amount}, "
            f"état « {item.state} » observé le {item.observed_at.isoformat()}, "
            f"complété le {completed} — {_reasons(item.reasons)}"
        )
    else:
        label = "complété" if item.instant_is == "completed" else "créé"
        related = item.related_credit_id or "aucun crédit nommé"
        text = (
            f"Retour {item.id} ({item.type}) : {item.amount}, {label} le "
            f"{item.instant.isoformat()}, lié à {related}"
        )
    return f"<li>{escape(text)}</li>"
