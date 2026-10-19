"""ADR-011: automated mail is recognised by rule, and never answered automatically."""

import pytest

from app.domain.automated import automated_message
from app.domain.enums import RuleCode

PERSON = "alice@acme.fr"


def code(sender=PERSON, **headers):
    pairs = []
    for name, value in headers.items():
        for one in value if isinstance(value, list) else [value]:
            pairs.append((name.replace("_", "-"), one))
    finding = automated_message(sender, pairs)
    return finding.code if finding else None


class TestAPerson:
    def test_no_headers_is_a_person(self):
        assert code() is None

    def test_auto_submitted_no_says_so(self):
        assert code(Auto_Submitted="no") is None

    @pytest.mark.parametrize(
        "headers",
        [
            {"Precedence": "first-class"},
            {"X-Auto-Response-Suppress": "None"},
            {"Return-Path": "<alice@acme.fr>"},
            {"Subject": "Auto-Submitted: auto-replied"},
        ],
        ids=["precedence-other", "suppress-none", "return-path", "unrelated-header"],
    )
    def test_headers_that_say_nothing_about_automation(self, headers):
        assert automated_message(PERSON, list(headers.items())) is None

    def test_a_delivery_report_name_inside_the_domain_is_not_a_report(self):
        assert code(sender="ops@mailer-daemon.example.test") is None


class TestAnAutomatedResponse:
    """An auto-reply or a delivery report: nothing to investigate."""

    @pytest.mark.parametrize(
        "sender, headers",
        [
            (PERSON, {"Auto-Submitted": "auto-replied"}),
            (PERSON, {"Auto-Submitted": "Auto-Replied; owner-email=alice@acme.fr"}),
            (PERSON, {"Precedence": "auto_reply"}),
            (PERSON, {"Return-Path": "<>"}),
            (PERSON, {"Return-Path": " < > "}),
            ("MAILER-DAEMON@mail.example.test", {}),
            ("postmaster@example.test", {}),
        ],
        ids=[
            "auto-replied",
            "auto-replied-with-parameter",
            "precedence-auto-reply",
            "null-return-path",
            "null-return-path-spaced",
            "mailer-daemon",
            "postmaster",
        ],
    )
    def test_recognised(self, sender, headers):
        assert automated_message(sender, list(headers.items())).code is RuleCode.AUTOMATED_RESPONSE

    def test_a_response_outranks_a_bulk_signal_and_keeps_both_as_evidence(self):
        finding = automated_message(
            PERSON, [("Auto-Submitted", "auto-replied"), ("Precedence", "bulk")]
        )
        assert finding.code is RuleCode.AUTOMATED_RESPONSE
        assert finding.evidence["signals"] == ["auto-submitted:auto-replied", "precedence:bulk"]


class TestAnAutomatedMessage:
    """Software sent it: investigate, never answer automatically."""

    @pytest.mark.parametrize(
        "headers",
        [
            {"Auto-Submitted": "auto-generated"},
            {"Auto-Submitted": "auto-notified"},
            {"Auto-Submitted": "yes please"},
            {"Auto-Submitted": ""},
            {"Precedence": "bulk"},
            {"Precedence": " LIST "},
            {"Precedence": "junk"},
            {"List-Id": "<news.example.test>"},
            {"List-Unsubscribe": "<mailto:unsubscribe@example.test>"},
            {"X-Auto-Response-Suppress": "OOF, AutoReply"},
            {"X-Auto-Response-Suppress": "All"},
        ],
        ids=[
            "auto-generated",
            "auto-notified",
            "malformed",
            "empty",
            "bulk",
            "list-spaced-upper",
            "junk",
            "list-id",
            "list-unsubscribe",
            "suppress-oof",
            "suppress-all",
        ],
    )
    def test_recognised(self, headers):
        assert automated_message(PERSON, list(headers.items())).code is RuleCode.AUTOMATED_MESSAGE

    def test_header_names_ignore_case_and_whitespace(self):
        assert automated_message(PERSON, [(" precedence ", "bulk")]) is not None

    def test_contradictory_repeats_count_as_automated(self):
        """One `no` does not cancel an `auto-generated` beside it."""
        pairs = [("Auto-Submitted", "no"), ("Auto-Submitted", "auto-generated")]
        assert automated_message(PERSON, pairs).code is RuleCode.AUTOMATED_MESSAGE

    def test_evidence_keeps_a_bounded_token_not_the_value(self):
        finding = automated_message(PERSON, [("Auto-Submitted", "x" * 500)])
        (signal,) = finding.evidence["signals"]
        assert len(signal) <= len("auto-submitted:") + 40


class TestNothingIsGranted:
    def test_authentication_results_are_never_read(self):
        """A forged pass changes nothing (RFC 8601: only a trusted gateway vouches)."""
        pairs = [("Authentication-Results", "mx.example.test; dmarc=pass; spf=pass")]
        assert automated_message(PERSON, pairs) is None
        pairs.append(("Precedence", "bulk"))
        assert automated_message(PERSON, pairs).code is RuleCode.AUTOMATED_MESSAGE
