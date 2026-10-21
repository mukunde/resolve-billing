"""The case tags are checked against the taxonomy, and the tables follow them."""

import copy
import json

import pytest

from evals import coverage

TAXONOMY = coverage.TAXONOMY_PATH.read_text(encoding="utf-8")
CASES = json.loads(coverage.CASES_PATH.read_text(encoding="utf-8"))
KNOWN = coverage.boundaries(TAXONOMY)


def test_every_development_case_is_tagged_validly():
    assert coverage.problems(CASES, KNOWN) == []


def test_the_committed_tables_are_the_generated_ones():
    assert coverage.updated(TAXONOMY, coverage.render(CASES, KNOWN)) == TAXONOMY


def test_the_taxonomy_is_read_as_written():
    assert KNOWN["B07"].sided and KNOWN["B07"].called_for == ("CP", "BV", "MU")
    assert not KNOWN["B26"].sided  # one rule and its outcomes
    assert "B23" in KNOWN


def with_tag(**tag):
    cases = copy.deepcopy(CASES)
    cases[0]["covers"] = [tag]
    return cases


@pytest.mark.parametrize(
    "tag, problem",
    [
        ({"boundary": "B99", "side": "A", "type": "EX"}, "unknown or untaggable"),
        ({"boundary": "B23", "type": "EX"}, "unknown or untaggable"),
        ({"boundary": "B07", "side": "A", "type": "ZZ"}, "unknown type"),
        ({"boundary": "B07", "type": "EX"}, "a side A, B or X is required"),
        ({"boundary": "B26", "side": "A", "type": "EX"}, "has no sides"),
        ({"boundary": "B07", "side": "X", "type": "EX"}, "names an outcome section 1 defines"),
        ({"boundary": "B05", "side": "A", "type": "MU"}, "names the case it varies from"),
        (
            {"boundary": "B05", "side": "A", "type": "MU", "with": "nope", "varies": "tone"},
            "is not another case",
        ),
        (
            {"boundary": "B05", "side": "A", "type": "MU", "with": "duplicate_en"},
            "what `varies`",
        ),
        ({"boundary": "B07", "side": "A", "type": "EX", "asserts": "maybe"}, "`asserts`"),
        (
            {"boundary": "B07", "side": "X", "type": "EX", "outcome": "identity_conflict"},
            "an outcome section 1 defines",
        ),
        ({"boundary": "B07", "side": "A", "type": "EX", "outcome": "x"}, "side X and B26 only"),
        ({"boundary": "B26", "type": "EX"}, "must match `expect_automatic_reply`"),
        ({"boundary": "B07", "side": 1, "type": "EX"}, "`side` is a string"),
        (
            {"boundary": "B05", "side": "A", "type": "MU", "with": "duplicate_fr", "varies": "x"},
            "is not another case",
        ),
    ],
)
def test_an_invalid_tag_is_named(tag, problem):
    found = coverage.problems(with_tag(**tag), KNOWN)
    assert any(problem in p for p in found), found


def test_an_untagged_case_is_named():
    cases = copy.deepcopy(CASES)
    del cases[0]["covers"]
    assert coverage.problems(cases, KNOWN) == [f"{cases[0]['id']}: no `covers`"]


def test_an_unknown_invariance_is_named():
    cases = copy.deepcopy(CASES)
    cases[0]["invariance"] = ["I9"]
    assert "unknown invariance" in coverage.problems(cases, KNOWN)[0]


def test_a_type_called_for_with_no_case_is_marked():
    table = coverage.render(CASES, KNOWN)
    b06 = next(line for line in table.splitlines() if line.startswith("| B06 |") and "**" in line)
    assert "**0**" in b06


def test_a_stale_table_fails_the_check(monkeypatch, tmp_path, capsys):
    stale = tmp_path / "taxonomy.md"
    stale.write_text(TAXONOMY.replace("| B06 |", "| B06x |", 1), encoding="utf-8")
    monkeypatch.setattr(coverage, "TAXONOMY_PATH", stale)
    monkeypatch.setattr("sys.argv", ["coverage", "--check"])
    assert coverage.main() == 1
    assert "stale" in capsys.readouterr().err


def test_b26_must_agree_with_the_expectation():
    cases = copy.deepcopy(CASES)
    case = next(c for c in cases if c["id"] == "unknown_sender_plain")
    tag = next(t for t in case["covers"] if t["boundary"] == "B26")
    tag["outcome"] = "allowed"
    assert any("expect_automatic_reply" in p for p in coverage.problems(cases, KNOWN))


def test_the_same_side_twice_is_named():
    cases = with_tag(boundary="B07", side="A", type="EX")
    cases[0]["covers"].append({"boundary": "B07", "side": "A", "type": "CD"})
    assert any("tagged twice" in p for p in coverage.problems(cases, KNOWN))


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda t: t.replace("** · case", "**", 1), "unreadable boundary heading"),
        (lambda t: t.replace("**B06 — ", "**B05 — ", 1), "defined twice"),
        (lambda t: t.replace("- Tests: CP, BV, MU.", "- Tests: contrast pairs.", 1), "Tests line"),
        (lambda t: t.replace("- Tests: CP, BV, MU.", "- Test types: CP.", 1), "Tests line"),
        (lambda t: t.replace("## 2. Invariances", "## 2. Invariance", 1), "heading not found"),
    ],
)
def test_section_1_is_read_strictly(edit, message):
    with pytest.raises(coverage.TaxonomyError, match=message):
        coverage.boundaries(edit(TAXONOMY))
