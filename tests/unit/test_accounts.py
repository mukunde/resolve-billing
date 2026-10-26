"""Who a run's vendor account is, by non-secret identity
(specs/007-openai-qwen-engines, T040)."""

import hashlib

import pytest

from app.decision.hosted import configuration
from evals import accounts, first_calls
from evals.accounts import AccountRefused

KEY = "sk-SENTINEL-KEY-0123456789"


@pytest.fixture(autouse=True)
def declared(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
    monkeypatch.setenv("SCALEWAY_API_KEY", KEY)
    monkeypatch.setenv("SCALEWAY_ORGANIZATION_ID", "11111111-scw-org")
    monkeypatch.setenv("SCALEWAY_PROJECT_ID", "22222222-scw-project")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-set-by-the-test")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key-set-by-the-test")
    monkeypatch.setenv("GEMINI_QUOTA_PROJECT", "resolve-billing-eval")


class TestTheExistingConfigurations:
    def test_their_identity_is_spec_004_s(self):
        key = hashlib.sha256(b"gemini-key-set-by-the-test").hexdigest()[:12]

        assert accounts.identity(configuration("gemini-3.8-flash")) == {
            "key": key,
            "project": "resolve-billing-eval",
        }
        assert first_calls.account(configuration("gemini-3.8-flash")) == {
            "key": key,
            "project": "resolve-billing-eval",
        }

    def test_their_run_lock_is_unchanged(self):
        assert accounts.billing_account(configuration("mistral-medium-3-5")) == "mistral"
        assert accounts.billing_account(configuration("gemini-3.8-flash")) == "google"


class TestTheNewConfigurations:
    def test_openai_s_identity_is_its_organisation_and_project(self):
        identity = accounts.identity(configuration("gpt-6-luna"))

        assert identity == {"organisation": "org-test", "project": "proj_test"}

    def test_scaleway_s(self):
        identity = accounts.identity(configuration("qwen3.5-397b-a17b"))

        assert identity == {
            "organisation": "11111111-scw-org",
            "project": "22222222-scw-project",
        }

    def test_no_key_and_no_key_hash_in_it(self):
        identity = accounts.identity(configuration("gpt-6-luna"))
        digest = hashlib.sha256(KEY.encode()).hexdigest()

        assert KEY not in repr(identity)
        assert digest[:12] not in repr(identity)
        assert "key" not in identity

    def test_changing_the_key_changes_nothing(self, monkeypatch):
        before = accounts.identity(configuration("gpt-6-luna"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-another-key")

        assert accounts.identity(configuration("gpt-6-luna")) == before
        assert accounts.billing_account(configuration("gpt-6-luna")) == "openai-org-test"

    def test_the_run_lock_and_envelope_are_the_billing_organisation_s(self, monkeypatch):
        luna = accounts.billing_account(configuration("gpt-6-luna"))
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_other")
        sol = accounts.billing_account(configuration("gpt-6.1-sol"))

        assert luna == sol == "openai-org-test"
        assert (
            accounts.billing_account(configuration("qwen3.5-397b-a17b"))
            == "scaleway-11111111-scw-org"
        )

    @pytest.mark.parametrize(
        ("name", "missing"),
        [
            ("gpt-6-luna", "OPENAI_ORG_ID"),
            ("gpt-6.1-sol", "OPENAI_PROJECT_ID"),
            ("qwen3.5-397b-a17b", "SCALEWAY_ORGANIZATION_ID"),
            ("qwen3.5-397b-a17b", "SCALEWAY_PROJECT_ID"),
        ],
    )
    def test_a_missing_variable_is_named_and_refused(self, name, missing, monkeypatch):
        monkeypatch.delenv(missing)

        assert missing in accounts.missing(configuration(name))
        with pytest.raises(AccountRefused, match=missing):
            accounts.identity(configuration(name))

    @pytest.mark.parametrize("value", ["../elsewhere", "org test", "org/x", ""])
    def test_an_identity_unfit_for_a_file_name_is_refused(self, value, monkeypatch):
        monkeypatch.setenv("OPENAI_ORG_ID", value)

        with pytest.raises(AccountRefused):
            accounts.billing_account(configuration("gpt-6-luna"))

    def test_nothing_is_missing_for_a_spec_004_configuration(self):
        assert accounts.missing(configuration("mistral-medium-3-5")) is None
