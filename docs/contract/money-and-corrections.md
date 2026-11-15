# Money and corrections

How amounts are held, and what a correction is worth -- or why the code
proposes none. Written on 2026-11-15 from the existing records, as they
stood, with spec 016 (specs/016-bank-evidence), the first change to touch the
capability since contracts exist: a bank finding now withholds every amount.
Reasons are in constitution IV, ADR-004 (human approval of every financial
action), ADR-005 (money as minor units), taxonomy S3-S5 and D14
(`.specify/bugs/d14-silent-rules/`).

## What the system relies on

- **Money is an integer count of minor units and a currency**
  (`app/domain/money.py`), stored as an integer. `Money.from_decimal` and
  `Money.multiply` refuse a float. One function turns a decimal into minor units,
  `Money.from_decimal`, with one rounding mode, half-up. A currency's minor
  unit is two decimals unless listed otherwise (zero-decimal currencies such
  as JPY). Two amounts in different currencies are never added, subtracted or
  ordered: the domain raises instead of converting.
- **The engine names a kind of correction, never an amount.** What a credit
  note or a refund is worth is computed by `correction_for`
  (`app/policies/correction.py`) from the records, or withheld with a reason.
- **An amount only when the whole invoice is the error** (S5, provisional):
  - a duplicated invoice, or one for a period after cancellation: the whole
    invoice is corrected, by its total;
  - an invoice collected at least twice, by distinct settled payments each
    equal to its total: the extra payment is refunded -- by a refund only.
- **Withheld, with the reason recorded**, in this order:

  | When | Why no amount |
  |---|---|
  | the invoice is a draft, uncollectible or void | it cannot be corrected (S4) |
  | a currency mismatch was found | amounts in different currencies |
  | a check could not conclude (`NOT_EVALUABLE`, `USAGE_UNAVAILABLE`) | any such check blocks any correction (D14, option a) |
  | a `BANK_CREDIT_OBSERVED` or `BANK_CREDIT_TO_RECONCILE` finding (spec 016) -- a bank check that could not conclude is withheld by the row above | a person weighs the bank's evidence; never a ground for a figure |
  | the invoice's total is zero or negative | nothing to correct |
  | no finding grounds a correction | nothing the code can stand behind |
  | two distinct corrections on one invoice | the code does not combine them |
  | a partial correction (proration, usage) | no amount until the correction-amount specification exists |
  | a duplicate payment, asked as a credit note | only a refund corrects it |
  | a refund that no settled payment covers | partial payments adding up are left to a person |

- **Every amount needs a person.** A correction with an amount is a
  financial action: it is queued for approval, never executed automatically
  (ADR-004; `approval-and-execution.md`). The billing platform is the
  authority on what an invoice can still give back (ADR-008).

## Not guaranteed today

- **Partial corrections have no amount**: a mispriced proration or a usage
  gap goes to a person without a figure.
- **Overpayment other than a full duplicate** is not recognised.
- **No conversion**: an invoice and a payment in different currencies are
  left to a person.

## Covered by

`tests/unit/test_money.py`, `tests/unit/test_correction.py`,
`tests/unit/test_bank_check.py` (the bank's veto),
`tests/integration/test_bank_evidence.py`, and the development set's cases on
duplicates, cancellations, payments and the bank.
