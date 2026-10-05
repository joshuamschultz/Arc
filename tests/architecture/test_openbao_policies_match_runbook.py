"""The OpenBao policy HCL in the runbook is the HCL infra applies, line for line."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICIES = sorted((ROOT / "deploy" / "openbao" / "policies").glob("*.hcl"))
RUNBOOK = (ROOT / "docs" / "runbooks" / "operate" / "openbao-accounts.md").read_text()


def test_the_account_policy_set_is_complete() -> None:
    assert {p.stem for p in POLICIES} == {"issuer", "cipher", "anchor", "config-reader", "enroll"}


@pytest.mark.parametrize("policy", POLICIES, ids=lambda p: p.stem)
def test_runbook_quotes_every_policy_rule(policy: Path) -> None:
    rules = [
        line.strip()
        for line in policy.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rules
    for rule in rules:
        assert rule in RUNBOOK, f"{policy.name}: runbook is missing {rule!r}"
