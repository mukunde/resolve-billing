"""The persistent fake billing platform, proven on its own before A4 rests
on it (specs/006-restart-failures, T020)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import ProviderRejected
from tests.e2e._persistent_fake import TIMEOUT_AFTER_WRITE_ONCE, LedgerRefused, PersistentFake

INVOICE = "inv_002"  # paid, EUR 99


async def credit(platform, key: str, amount: int = 9900, action_id: str = "act_1"):
    allocation = await platform.plan_write(INVOICE, "credit_note")
    return await platform.create_credit_note(
        INVOICE,
        amount,
        "EUR",
        "duplicate",
        key,
        action_id=action_id,
        allocation=allocation,
        send_by=datetime.now(UTC) + timedelta(hours=1),
    )


async def test_a_write_outlives_the_instance_that_made_it(tmp_path):
    ledger = tmp_path / "ledger.json"
    made = await credit(PersistentFake(ledger), "key_1")

    later = PersistentFake(ledger)

    assert later.writes() == [made]
    assert await later.find_action("key_1", INVOICE) != []


async def test_a_replayed_key_is_the_same_write_never_a_second(tmp_path):
    ledger = tmp_path / "ledger.json"
    first = await credit(PersistentFake(ledger), "key_1")

    again = await credit(PersistentFake(ledger), "key_1")

    assert again.provider_action_id == first.provider_action_id
    assert len(PersistentFake(ledger).writes()) == 1


async def test_what_an_invoice_gave_back_caps_the_next_write_across_instances(tmp_path):
    ledger = tmp_path / "ledger.json"
    await credit(PersistentFake(ledger), "key_1", amount=9900)

    with pytest.raises(ProviderRejected, match="left to correct"):
        await credit(PersistentFake(ledger), "key_2", amount=100, action_id="act_2")
    assert len(PersistentFake(ledger).writes()) == 1


async def test_two_live_instances_see_each_other_s_writes_before_writing(tmp_path):
    ledger = tmp_path / "ledger.json"
    first, second = PersistentFake(ledger), PersistentFake(ledger)

    await credit(first, "key_1", amount=9900)

    with pytest.raises(ProviderRejected, match="left to correct"):
        await credit(second, "key_2", amount=100, action_id="act_2")
    assert len(PersistentFake(ledger).writes()) == 1


async def test_the_fault_loses_one_answer_after_the_write_is_committed(tmp_path):
    ledger = tmp_path / "ledger.json"
    platform = PersistentFake(ledger, fault=TIMEOUT_AFTER_WRITE_ONCE)

    with pytest.raises(TimeoutError):
        await credit(platform, "key_1")

    (held,) = PersistentFake(ledger).writes()
    assert held.idempotency_key == "key_1"
    # A retry under the key finds the write: no second write, no second fault.
    again = await credit(platform, "key_1")
    assert again.provider_action_id == held.provider_action_id
    assert len(PersistentFake(ledger).writes()) == 1


async def test_a_spent_fault_stays_spent_for_the_next_instance(tmp_path):
    ledger = tmp_path / "ledger.json"
    with pytest.raises(TimeoutError):
        await credit(PersistentFake(ledger, fault=TIMEOUT_AFTER_WRITE_ONCE), "key_1", amount=5000)

    # Armed again, as a restarted service would arm it: a new write answers.
    restarted = PersistentFake(ledger, fault=TIMEOUT_AFTER_WRITE_ONCE)
    second = await credit(restarted, "key_2", amount=4900, action_id="act_2")

    keys = [write.idempotency_key for write in PersistentFake(ledger).writes()]
    assert keys == ["key_1", "key_2"]
    assert second.idempotency_key == "key_2"


async def test_a_refused_write_commits_nothing(tmp_path):
    ledger = tmp_path / "ledger.json"
    platform = PersistentFake(ledger, fault=TIMEOUT_AFTER_WRITE_ONCE)

    with pytest.raises(ProviderRejected):
        await credit(platform, "key_1", amount=100_000)

    assert not ledger.exists() or PersistentFake(ledger).writes() == []


@pytest.mark.parametrize(
    "content",
    [
        "{nope",
        '{"writes": []}',
        '{"writes": {}, "fault_spent": false}',
        '{"writes": [{}], "fault_spent": false}',
    ],
)
def test_a_ledger_that_cannot_be_used_is_refused_and_left_as_it_is(tmp_path, content):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(content, encoding="utf-8")

    with pytest.raises(LedgerRefused):
        PersistentFake(ledger)
    assert ledger.read_text(encoding="utf-8") == content


def _write(**changes) -> dict:
    write = {
        "provider": "fake",
        "provider_action_id": "cn_001",
        "action_type": "credit_note",
        "idempotency_key": "key_1",
        "status": "succeeded",
        "created_at": "2026-10-25T12:00:00Z",
        "invoice_id": INVOICE,
        "amount_minor": 9900,
        "currency": "EUR",
        "test_mode": True,
        "allocation": "customer_balance",
        "action_id": "act_1",
        "observed_at": "2026-10-25T12:00:00Z",
    }
    return write | changes


@pytest.mark.parametrize(
    "writes",
    [
        [_write(amount_minor=-9900)],
        [_write(), _write(idempotency_key="key_2")],  # one reference twice
        [_write(), _write()],  # one key twice
        [_write(provider="stripe")],
        [_write(test_mode=False)],
        [_write(allocation="reduce_due", action_type="refund")],
        [_write(invoice_id="inv_unknown")],
        [_write(currency="USD")],
    ],
    ids=[
        "negative",
        "reference-twice",
        "key-twice",
        "provider",
        "live",
        "allocation",
        "invoice",
        "currency",
    ],
)
def test_a_ledger_this_platform_could_not_have_written_is_refused(tmp_path, writes):
    import json

    ledger = tmp_path / "ledger.json"
    content = json.dumps({"writes": writes, "fault_spent": False})
    ledger.write_text(content, encoding="utf-8")

    with pytest.raises(LedgerRefused, match="inconsistent"):
        PersistentFake(ledger)
    assert ledger.read_text(encoding="utf-8") == content


def test_a_well_formed_ledger_is_accepted(tmp_path):
    import json

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"writes": [_write()], "fault_spent": False}), encoding="utf-8")

    assert len(PersistentFake(ledger).writes()) == 1


@pytest.mark.parametrize("helper", ["plant_action", "set_status", "change_invoice"])
def test_helpers_whose_effects_would_not_outlive_the_process_are_refused(tmp_path, helper):
    platform = PersistentFake(tmp_path / "ledger.json")

    with pytest.raises(NotImplementedError):
        getattr(platform, helper)(
            *(
                {
                    "plant_action": [None],
                    "set_status": ["cn_001", "failed"],
                    "change_invoice": [INVOICE],
                }[helper]
            )
        )


def test_an_unknown_fault_is_refused(tmp_path):
    with pytest.raises(ValueError, match="fault"):
        PersistentFake(tmp_path / "ledger.json", fault="drop_everything")
