#!/usr/bin/env python3
"""Run the cross-package zero-trust adversarial regression battery."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# These are scenario suites, not a grab bag of every file named ``security``.
# Each target drives a real production boundary and must fail if that boundary
# becomes bypassable. Keep the threat names aligned with the runbook.
SCENARIOS: dict[str, tuple[str, ...]] = {
    "artifact tampering and unauthorized capability load": (
        "packages/arcagent/tests/security/test_sign_gate_load.py",
        "packages/arcagent/tests/security/test_module_capability_trust.py",
        "packages/arcagent/tests/security/capabilities/test_capability_import_drift.py",
    ),
    "prompt replacement and instruction-boundary attacks": (
        "packages/arcprompt/tests/unit/test_verifier.py",
        "packages/arcagent/tests/security/test_prompt_overlay_isolation.py",
        "packages/arcrun/tests/security/test_prompt_injection.py",
        "packages/arcrun/tests/security/test_steering_injection.py",
    ),
    "log disclosure and audit tampering": (
        "packages/arcagent/tests/security/test_audit_at_rest.py",
        "packages/arcrun/tests/security/test_event_tampering.py",
        "packages/arcui/tests/test_session_replay_media.py",
    ),
    "forged, unauthorized and replayed control actions": (
        "packages/arcui/tests/test_workflow_routes.py",
        "packages/arcui/tests/test_approvals_route.py",
        "packages/arcgateway/tests/platform/slack/test_slack_socket_replay_dedup.py",
        "packages/arcgateway/tests/integration/test_end_to_end_federal_tier.py",
    ),
    "identity, key and cross-scope isolation": (
        "packages/arctrust/tests/test_trust_store_security.py",
        "packages/arcagent/tests/security/test_multi_agent_runtime_isolation.py",
        "packages/arcmemory/tests/security/test_no_read_up_real_paths.py",
        # H-024 connected-data explorer: chunks/tables bound to one connection's
        # source-scope — no cross-connection leak, no cross-agent pool read, no
        # count-inference of a hidden chunk, viewer is 403, vector degrades LOUD.
        "packages/arcmemory/tests/unit/test_operator_connected_explorer.py",
        "packages/arcui/tests/integration/test_connected_explorer_routes.py",
        # H-047 build_brain identity guard: zero-tolerance cross-agent isolation.
        "packages/arcmemory/tests/security/test_build_brain_isolation.py",
    ),
    "foreign harness enrollment and admission (H-040)": (
        "packages/arctrust/tests/test_enrollment_grant.py",
        "packages/arcteam/tests/security/test_enrollment_abuse.py",
        "packages/arcteam/tests/unit/test_harness_enrollment.py",
    ),
    "dashboard symlink deletion and key/control-plane exfiltration (H-018)": (
        "packages/arcui/tests/integration/test_file_delete_routes.py",
        "packages/arcui/tests/integration/test_path_traversal_e2e.py",
    ),
    "voice channel abuse — pairing, tier gate, spoken injection (SPEC-077)": (
        "packages/arcgateway/tests/security/test_voice_abuse.py",
    ),
    "standalone runtime and resource-containment boundaries": (
        "tests/architecture/test_no_arcrun_imports_arcagent.py",
        "packages/arcagent/tests/architecture/test_dependency_boundaries.py",
        "packages/arcrun/tests/security/test_resource_exhaustion.py",
        "packages/arcrun/tests/security/test_spawn_depth_bomb.py",
    ),
}


def targets() -> list[str]:
    """Return every scenario target once, preserving threat-model order."""
    ordered: list[str] = []
    for paths in SCENARIOS.values():
        for path in paths:
            if path not in ordered:
                ordered.append(path)
    return ordered


def main(argv: list[str] | None = None) -> int:
    """Run pytest over the curated battery and return its exact exit status."""
    missing = [path for path in targets() if not (ROOT / path).is_file()]
    if missing:
        sys.stderr.write("adversarial manifest references missing tests:\n")
        sys.stderr.writelines(f"  - {path}\n" for path in missing)
        return 2

    for threat, paths in SCENARIOS.items():
        sys.stdout.write(f"{threat}: {len(paths)} suite(s)\n")
    command = [sys.executable, "-m", "pytest", *targets(), *(argv or [])]
    with tempfile.TemporaryDirectory(prefix="arc-adversarial-") as isolated_home:
        environment = os.environ.copy()
        environment["HOME"] = isolated_home
        environment["ARC_CONFIG_DIR"] = str(Path(isolated_home) / ".arc")
        environment["ARC_TEAM_ROOT"] = str(Path(isolated_home) / "arc")
        return subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=environment,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
