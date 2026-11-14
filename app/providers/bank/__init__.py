"""Banks as evidence that a payment arrived, read-only (specs/016-bank-evidence; ADR-014).

Revolut Business, on its Sandbox, is the one bank today, and its vocabulary lives in this
package alone: `client` speaks the wire (pinned host, GET only, signed, paced), `mapping`
turns its transactions into the domain's bank types, `revolut` is the `BankEvidence` port
over both. The fake bank, for tests and evaluation, is `app/providers/fake/bank.py`.
"""
