"""Revolut Business as bank evidence, read-only, on its Sandbox (specs/016-bank-evidence).

Revolut's vocabulary lives in this package alone: `client` speaks the wire (pinned host,
GET only, signed, paced), `mapping` turns its transactions into the domain's bank types,
`adapter` is the `BankEvidence` port over both.
"""
