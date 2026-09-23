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
    "browser media custody and MIME spoofing": (
        "packages/arcgateway/tests/security/test_media_store_refusals.py",
        "packages/arcui/tests/integration/test_attachment_session_identity.py",
    ),
    "standalone runtime and resource-containment boundaries": (
        "tests/architecture/test_no_arcrun_imports_arcagent.py",
        "packages/arcagent/tests/architecture/test_dependency_boundaries.py",
        "packages/arcrun/tests/security/test_resource_exhaustion.py",
        "packages/arcrun/tests/security/test_spawn_depth_bomb.py",
    ),
    # SPEC-081 open skill packages: a loose skill ZIP may carry any reviewable
    # subtree, but never an auto-run/opaque artifact, never load-time execution,
    # and a promoted script that is swapped or unsigned must not run at
    # enterprise/federal. Covers ASI04/ASI05/LLM03 for the new execute path.
    "open skill-package intake and tiered script execution (SPEC-081)": (
        "packages/arcagent/tests/security/capabilities/test_capability_import_binary_rejection_spec081.py",
        "packages/arcagent/tests/security/capabilities/test_capability_import_skill_no_load_execution_spec081.py",
        "packages/arcagent/tests/security/capabilities/test_capability_import_rich_skill_invalidation_spec081.py",
        "packages/arcagent/tests/security/capabilities/test_skill_script_runner_integrity_spec081.py",
        "packages/arcagent/tests/security/capabilities/test_skill_script_runner_skillname_jail_spec081.py",
    ),
    # SPEC-082 MCP door + connectors: an outside operator/agent drives Arc's own
    # tools through the same signed-authorized-audited envelope. Every hostile
    # inbound — forged/duplicated DID, replayed nonce, stale timestamp, unsigned
    # or tampered signature, a verb downgraded past the exposure allowlist, and a
    # signed-but-unenrolled caller at federal — must fail closed AND be audited.
    # A compromised Composio broker cannot smuggle a tool past the manifest
    # allowlist. Covers ASI02/ASI03/ASI04/ASI07 and LLM03/LLM06 on the new door.
    "MCP door abuse and connector-broker smuggling (SPEC-082)": (
        "packages/arcagent/tests/security/test_mcp_door_abuse_spec082.py",
        "packages/arcagent/tests/security/test_composio_broker_abuse_spec082.py",
    ),
    # SPEC-084 serves the door over the official ``mcp`` SDK wire. Every hostile
    # frame the wire can carry — a malformed / non-MCP body, an unsupported
    # protocol version, an unsigned ``tools/call``, a replayed envelope, and a
    # ``_meta`` signed over different content than the call — must fail closed
    # WITHOUT a crash and WITHOUT an un-audited pass-through, driven end to end by
    # a real SDK client (and a raw POST for the transport-frame attacks). The
    # SPEC-082 pipeline is unchanged; this proves it still holds at the real wire.
    # Covers LLM05/LLM10 and ASI02/ASI03/ASI07 on the SDK-served door.
    "MCP wire abuse over the real SDK transport (SPEC-084)": (
        "packages/arcagent/tests/security/test_mcp_wire_abuse_spec084.py",
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
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
