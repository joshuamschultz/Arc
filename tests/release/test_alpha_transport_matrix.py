from __future__ import annotations

from scripts.validate_alpha_transport import CHECKS, ROOT


def test_alpha_transport_matrix_covers_all_streaming_surfaces() -> None:
    assert {check.name for check in CHECKS} == {
        "cli-streaming",
        "gateway-streaming",
        "provider-routing-streaming",
        "transport-lint",
        "tui-streaming",
    }


def test_alpha_transport_matrix_references_existing_local_targets() -> None:
    for check in CHECKS:
        for argument in check.command:
            if argument.startswith(("packages/", "tests/")):
                assert (ROOT / argument).exists(), f"missing matrix target: {argument}"


def test_alpha_transport_matrix_does_not_claim_database_validation() -> None:
    assert all("arcstore" not in argument for check in CHECKS for argument in check.command)
