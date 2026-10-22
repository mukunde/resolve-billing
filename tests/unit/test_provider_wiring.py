"""Choosing the billing platform, and where Stripe may be named (FR-023, FR-024).

`BILLING_PROVIDER` selects the platform, the fake by default; Stripe's key is
read in the wiring and nowhere else; the SDK is imported only by the
adapter's client, the seed script and Stripe's own tests -- checked on the
source, so a new import anywhere else fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.api import deps
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.stripe.adapter import StripeBillingProvider
from app.providers.stripe.keys import ProviderConfigurationError

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [p for d in ("app", "evals", "scripts", "tests") for p in (ROOT / d).rglob("*.py")]

#: Where the SDK may be imported.
SDK_ALLOWED = {"app/providers/stripe/client.py", "scripts/stripe_seed.py"}


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_stripe_test(name: str) -> bool:
    return name.startswith("tests/contract/") or (
        name.startswith("tests/") and "stripe" in Path(name).name
    )


@pytest.fixture
def chosen(monkeypatch):
    def choose(**environment):
        for name in ("BILLING_PROVIDER", "STRIPE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        deps.get_provider.cache_clear()
        return deps.get_provider

    return choose


class TestTheChoice:
    def test_the_fake_by_default(self, chosen):
        assert isinstance(chosen()(), FakeBillingProvider)

    def test_the_fake_by_name(self, chosen):
        assert isinstance(chosen(BILLING_PROVIDER="fake")(), FakeBillingProvider)

    def test_stripe_with_the_restricted_test_key(self, chosen, monkeypatch):
        opened = []
        monkeypatch.setattr(deps, "connect", lambda key: opened.append(key) or object())
        provider = chosen(BILLING_PROVIDER="stripe", STRIPE_API_KEY="rk_test_51RbAbc")()
        assert isinstance(provider, StripeBillingProvider)
        assert opened == ["rk_test_51RbAbc"]

    @pytest.mark.parametrize(
        ("environment", "category"),
        [({}, "missing_key"), ({"STRIPE_API_KEY": "sk_live_51RbAbc"}, "live_key")],
    )
    def test_stripe_refuses_a_wrong_key_before_anything(self, chosen, environment, category):
        with pytest.raises(ProviderConfigurationError) as raised:
            chosen(BILLING_PROVIDER="stripe", **environment)()
        assert raised.value.category == category

    def test_an_unknown_platform_is_refused(self, chosen):
        with pytest.raises(RuntimeError, match="unknown billing provider"):
            chosen(BILLING_PROVIDER="chargebee")()


class TestWhereStripeIsNamed:
    def test_the_key_is_read_in_the_wiring_only(self):
        readers = {
            relative(p)
            for p in (ROOT / "app").rglob("*.py")
            if "STRIPE_API_KEY" in p.read_text(encoding="utf-8")
        }
        assert readers == {"app/api/deps.py"}

    def test_the_seed_key_is_never_read_by_the_application(self):
        assert not [
            p for p in (ROOT / "app").rglob("*.py") if "STRIPE_SEED_KEY" in p.read_text("utf-8")
        ]

    def test_the_sdk_is_imported_only_where_allowed(self):
        offenders = []
        for path in SOURCES:
            name = relative(path)
            if name in SDK_ALLOWED or is_stripe_test(name):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module or ""]
                elif isinstance(node, ast.Call) and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        callee = ast.unparse(node.func)
                        if callee.endswith(("importorskip", "import_module", "__import__")):
                            modules = [first.value]
                if any(m == "stripe" or m.startswith("stripe.") for m in modules):
                    offenders.append(name)
        assert offenders == []

    def test_the_core_never_imports_the_adapter(self):
        """The domain, the application, the policies and the engines depend on
        the port alone; only the wiring names the Stripe package."""
        offenders = [
            relative(path)
            for path in (ROOT / "app").rglob("*.py")
            if not relative(path).startswith("app/providers/stripe/")
            and relative(path) != "app/api/deps.py"
            and imports_the_adapter(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.providers.stripe.adapter import StripeBillingProvider",
            "from app.providers.stripe import mapping",
            "import app.providers.stripe.client",
            "import app.providers.stripe as platform",
        ],
    )
    def test_every_form_of_that_import_is_seen(self, source):
        assert imports_the_adapter(source)

    def test_another_provider_is_not_that_import(self):
        assert not imports_the_adapter("from app.providers.base import BillingProvider")


def imports_the_adapter(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = [node.module or ""]
        else:
            continue
        if any(
            m == "app.providers.stripe" or m.startswith("app.providers.stripe.") for m in modules
        ):
            return True
    return False
