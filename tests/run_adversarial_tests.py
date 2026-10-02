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
        "packages/arcagent/tests/unit/capabilities/test_capability_inventory.py",
        "packages/arcagent/tests/unit/capabilities/test_capability_gating.py",
        "packages/arcagent/tests/security/test_module_capability_trust.py",
        "packages/arcagent/tests/security/capabilities/test_capability_import_drift.py",
        # Disapprove revokes the WHOLE skill pack (every sidecar + pin, including a
        # leftover sidecar whose file was deleted); the agent key never signs under
        # the agent-root capabilities/ tree.
        "packages/arcui/tests/test_trust_route.py",
        "packages/arccli/tests/test_cli_trust.py",
        "packages/arccli/tests/test_capability_signing_authority.py",
        # `arc skill evals promote/edit` on an installed skill commit an operator-signed
        # anchored revision, or stop with "activation unavailable" — never an unsigned
        # eval file the improver gate would score.
        "packages/arccli/tests/test_skill_evals_activation.py",
        # Federal strict skill sections are enforced at import REVIEW, not after promote.
        "packages/arcui/tests/test_capability_import_routes.py",
        "packages/arcagent/tests/unit/capabilities/test_capability_import_strict_sections.py",
        "tests/architecture/test_agent_key_never_signs_capabilities.py",
    ),
    "connected-source routing index tampering (memory poisoning)": (
        # A forged-but-canonical index.md is rebuilt from the documents, never
        # trusted by the once-per-run incremental refresh; a non-canonical one
        # is refused on read and by the operator view.
        "packages/arcmemory/tests/security/test_connected_index_tampering.py",
        "packages/arcmemory/tests/unit/test_collection_index_pipeline.py",
    ),
    "prompt replacement and instruction-boundary attacks": (
        "packages/arcprompt/tests/unit/test_verifier.py",
        "packages/arcagent/tests/security/test_prompt_overlay_isolation.py",
        "packages/arcrun/tests/security/test_prompt_injection.py",
        "packages/arcrun/tests/security/test_steering_injection.py",
        "packages/arcui/tests/test_prompt_overlay_status.py",
        "tests/architecture/test_no_stock_prompt_bypass.py",
        # Tampered override fails closed; an override written mid-run (after the
        # run's snapshot froze) never changes that run — TOCTOU on the prompt set.
        "tests/integration/test_prompt_edit_conformance.py",
    ),
    "control-plane document tampering (identity.md, pinned policy rules)": (
        # identity.md and policy_pinned.md are operator-signed: an on-disk edit, a
        # self-signed replacement, a replayed signature and an agent rewrite of a
        # pinned rule are all refused; the curator never re-scores or prunes a
        # pinned rule; a tampered identity.md refuses the whole run.
        "packages/arcprompt/tests/unit/test_signed_files.py",
        "packages/arcagent/tests/security/test_signed_workspace_documents.py",
        "packages/arcui/tests/integration/test_file_write_routes.py",
        "tests/journeys/test_journey_prompts.py",
    ),
    "standing-instruction planting via agent-writable pulse.md": (
        # pulse.md auto-runs as agent prompts; the agent's own write/edit/bash
        # must be denied so an injected turn cannot schedule instructions.
        "packages/arcagent/tests/unit/tools/test_protected_paths.py",
        "tests/journeys/test_journey_prompts.py",
    ),
    "log disclosure and audit tampering": (
        "packages/arcagent/tests/security/test_audit_at_rest.py",
        "packages/arcgateway/tests/unit/test_fs_audit_events.py",
        "packages/arcgateway/tests/unit/test_fs_reader.py",
        "packages/arcagent/tests/unit/core/test_checkpoint_sink.py",
        "packages/arcagent/tests/unit/core/test_spec053_hardening.py",
        "packages/arcllm/tests/test_trace_store.py",
        "packages/arctrust/tests/test_machine_rekey.py",
        "packages/arctrust/tests/test_hosted_rekey.py",
        "packages/arcrun/tests/security/test_event_tampering.py",
        "packages/arcui/tests/test_session_replay_media.py",
        "packages/arcgateway/tests/unit/test_broker_bootstrap.py",
    ),
    # Item 20 audit causality: the actor on a record is the real initiator and
    # cannot be claimed. A causal context forged in tool arguments or an HTTP
    # header is ignored; a forged/stripped WORM ``signer`` or an edited causal
    # chain breaks the record hash; a replayed record breaks the chain and every
    # row after it; a cancelled request's binding never leaks into the next;
    # background work never inherits a request; the operator key never stands
    # in for a UI session, a scheduler or an unbound caller.
    "forged audit attribution, forged signer and replayed audit records": (
        "packages/arctrust/tests/test_causal.py",
        "packages/arctrust/tests/test_audit_signer_verifier.py",
        "packages/arcstore/tests/integration/test_ingest_verify.py",
        "packages/arcui/tests/unit/test_audit_causality.py",
        "packages/arcagent/tests/unit/core/test_tool_dispatch_causality.py",
        "packages/arcagent/tests/unit/extension/test_connection_actor.py",
        "packages/arcagent/tests/unit/utils/test_spawn_background_detached.py",
    ),
    "forged, unauthorized and replayed control actions": (
        "packages/arcui/tests/test_workflow_routes.py",
        # An agent writes then runs its own workflow: unsigned runs are refused
        # for agent/scheduler at every tier; operator draft test is personal-only.
        "packages/arcteam/tests/security/test_agent_cannot_run_unsigned_workflow.py",
        "packages/arcteam/tests/unit/workflow/test_runner_unsigned_initiator.py",
        "packages/arcui/tests/test_approvals_route.py",
        "packages/arcgateway/tests/platform/slack/test_slack_socket_replay_dedup.py",
        "packages/arcgateway/tests/integration/test_end_to_end_federal_tier.py",
    ),
    "identity, key and cross-scope isolation": (
        "packages/arctrust/tests/test_trust_store_security.py",
        "packages/arcagent/tests/security/test_multi_agent_runtime_isolation.py",
        "packages/arcagent/tests/unit/modules/messaging/test_delivery.py",
        "packages/arcagent/tests/unit/modules/messaging/test_signed_delivery.py",
        "packages/arcmemory/tests/security/test_no_read_up_real_paths.py",
        # H-024 connected-data explorer: chunks/tables bound to one connection's
        # source-scope — no cross-connection leak, no cross-agent pool read, no
        # count-inference of a hidden chunk, viewer is 403, vector degrades LOUD.
        "packages/arcmemory/tests/unit/test_operator_connected_explorer.py",
        "packages/arcui/tests/integration/test_connected_explorer_routes.py",
        # H-047 build_brain identity guard: zero-tolerance cross-agent isolation.
        "packages/arcmemory/tests/security/test_build_brain_isolation.py",
    ),
    "agent mail abuse — unsigned, replayed, forged second reply (alpha-2 item 3)": (
        # Unsigned mail never wakes; a replayed idempotency key projects once; a
        # forged or racing third message is refused before the agent wakes; a
        # worker never drains (and re-signs) another identity's envelope.
        "packages/arcteam/tests/security/test_mail_abuse.py",
        "packages/arcteam/tests/unit/test_mail_one_reply.py",
        "packages/arcagent/tests/unit/modules/messaging/test_mail_turn.py",
        "packages/arcstore/tests/unit/test_mail_outbox.py",
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
    "report read authority and provenance refusal": (
        "packages/arcui/tests/test_report_preview.py",
    ),
    "voice channel abuse — pairing, tier gate, spoken injection (SPEC-077)": (
        "packages/arcgateway/tests/security/test_voice_abuse.py",
    ),
    "browser media custody and MIME spoofing": (
        "packages/arcgateway/tests/security/test_media_store_refusals.py",
        "packages/arcagent/tests/security/test_run_media_custody.py",
        "packages/arcui/tests/integration/test_attachment_session_identity.py",
    ),
    "LLM queue rollback, stale owner and resource exhaustion": (
        "packages/arcllm/tests/test_queue_control.py",
        "packages/arctrust/tests/test_vault_anchor.py",
        "packages/arctrust/tests/test_broker_queue.py",
        "packages/arctrust/tests/test_broker_queue_client.py",
        "packages/arctrust/tests/test_vault_record_cipher.py",
        "packages/arccli/tests/test_queue_runtime.py",
    ),
    # SPEC-083 promotion: an automated promotion without a durable decision record,
    # a malformed decision, or a failed chain append is refused before any shared
    # write; a write that may have landed is never reported as a refusal (so it is
    # never retried into a double share). Any module can emit on the shared bus, so
    # a forged knowledge:shared_attached/_detached carrying a capturing port must
    # change nothing and be audited; the core emitter cannot be claimed twice.
    # T-1212 abuse battery: a secret never reaches the classifier; an OOD
    # ``unclear``@0.99 or inconsistent verdict stays private; an outage mid-batch
    # publishes nothing; federal cannot be enabled; a forged origin DID is refused
    # by the exporter, publisher and shared backend; a stale/replayed/tampered/
    # unsigned ledger row is never a ``promote`` verdict; a shared entity block
    # copied to another entity or contributor slot is refused; a refused promote
    # leaves no ``allow`` record; the classifier key never reaches audit, ledger
    # or logs; item count/bytes and a hung classifier stay bounded. T-1213: the
    # vendor SDK is confined to the removable Jev drop-in and its absence is a
    # typed ``classifier_unavailable``. Covers LLM02/LLM03/LLM09/LLM10 and
    # ASI02/ASI03/ASI04/ASI06/ASI07.
    "memory promotion decision integrity and forged shared-knowledge ports": (
        "packages/arcteam/tests/test_promotion_audit_and_revoke.py",
        "packages/arcteam/tests/test_promotion_error_typing.py",
        "packages/arcagent/tests/unit/core/test_module_bus_core_emitter.py",
        "packages/arcagent/tests/modules/memory/test_shared_knowledge_late_bind.py",
        "packages/arcagent/tests/integration/test_promotion_fleet_journey.py",
        "packages/arcmemory/tests/security/test_memory_promotion_abuse.py",
        "packages/arcteam/tests/security/test_memory_promotion_abuse_shared.py",
        "packages/arcagent/tests/modules/memory/test_promotion_bridge.py",
        "packages/arcllm/tests/architecture/test_jev_import_confined.py",
        "packages/arcmemory/tests/architecture/test_promotion_plugin_absent.py",
        "packages/arcmemory/tests/promotion/test_sweep_classifier_preflight.py",
        "packages/arcagent/tests/modules/memory/test_memory_runtime_tier.py",
        # T-1227: an unsigned, tampered, attacker-signed or malformed override of
        # the Jev question (arcmemory/promotion_classify) sends nothing, never
        # falls back to stock, and is audited once as question_invalid.
        "packages/arcmemory/tests/security/test_promotion_question_override_abuse.py",
        # T-1225 "Run now": an overlapping or double-clicked manual run never
        # re-sends an item (one per-agent lock with the nightly sweep); a bad or
        # oversized cap is refused before egress; a viewer, a forged/unknown
        # agent, a non-object or extra-field body is refused AND audited
        # ``denied``; a crash leaks no content or path; the CLI refuses remote
        # plain HTTP, credential flags and a path/query-injecting agent name
        # before any request.
        "packages/arcmemory/tests/promotion/test_manual_run.py",
        "packages/arcui/tests/test_memory_promotion_run_route.py",
        "packages/arccli/tests/test_agent_promotion_run.py",
    ),
    "standalone runtime and resource-containment boundaries": (
        "tests/architecture/test_no_arcrun_imports_arcagent.py",
        "packages/arcagent/tests/architecture/test_dependency_boundaries.py",
        "packages/arcrun/tests/security/test_resource_exhaustion.py",
        "packages/arcrun/tests/security/test_spawn_depth_bomb.py",
        "packages/arcgateway/tests/unit/test_workflow_runner_host.py",
        "packages/arcteam/tests/unit/workflow/test_runner_escalation.py",
        "packages/arctrust/tests/test_deployment_grant.py",
        "packages/arcui/tests/test_health.py",
    ),
    "accepted run and intent ledger refusal": (
        "packages/arcagent/tests/architecture/test_run_owner_optional_absence.py",
        "packages/arcstore/tests/unit/test_accepted_runs.py",
        "packages/arcagent/tests/unit/modules/run_intents/test_ledger.py",
        "packages/arcagent/tests/unit/modules/run_intents/test_collected.py",
        "packages/arcagent/tests/unit/modules/run_intents/test_reply_outbox.py",
        "packages/arcagent/tests/unit/core/test_accepted_stream.py",
        "packages/arcgateway/tests/unit/test_signed_delivery.py",
        "packages/arcteam/tests/unit/test_messenger_signing.py",
    ),
    "scheduled control revision and occurrence replay refusal": (
        "packages/arcagent/tests/unit/core/test_control_contract.py",
        "packages/arcagent/tests/unit/modules/scheduler/test_signed_dispatch.py",
        "packages/arcagent/tests/unit/modules/scheduler/test_pending_recovery.py",
        "packages/arcagent/tests/unit/modules/scheduler/test_scheduler_capabilities.py",
        "packages/arcagent/tests/unit/modules/pulse/test_signed_pulse.py",
        "packages/arcagent/tests/integration/test_workflow_trigger_wiring.py",
        "packages/arcui/tests/integration/test_schedule_write_routes.py",
        "packages/arcteam/tests/unit/workflow/test_run_create_idempotence.py",
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
    # J4 B3-B5: an operator-approved skill pack must cover what the model reads
    # and runs. A reference or script changed after signing, re-signed with the
    # agent key, reached by ../ or through a symlink, or swapped between the
    # check and the run is refused; generic write/edit/bash never touch a bundle.
    "skill-pack tamper, agent-key laundering and script swap (J4)": (
        "packages/arcagent/tests/unit/capabilities/test_skill_files_j4.py",
        "packages/arcagent/tests/unit/builtins/test_skill_file_tools_j4.py",
        "packages/arcagent/tests/unit/tools/test_skill_tree_protection_j4.py",
        "packages/arcagent/tests/unit/builtins/test_resign_on_mutation.py",
        "packages/arcagent/tests/unit/capabilities/test_capability_import_archive_shapes_j4.py",
        "packages/arcui/tests/test_trust_route.py",
        "tests/journeys/test_j4_skill_packs.py",
        # One signing authority: revisions (scripts included) and every improver
        # write are operator-anchored; tamper/symlink/traversal refused per file.
        "packages/arcagent/tests/unit/capabilities/test_skill_revision_chain_w0.py",
        "packages/arcagent/tests/integration/test_improver_operator_revisions_w0.py",
        "tests/journeys/test_j4_update_then_rollback.py",
    ),
    "skill outcome bridge replay, cancellation and credential isolation": (
        "packages/arcagent/tests/integration/test_skill_tool_outcome_bridge.py",
        "packages/arcagent/tests/unit/modules/skills/test_sweep_and_args_wiring.py",
        "packages/arcagent/tests/unit/modules/policy/test_policy_tool_activity.py",
        "packages/arcagent/tests/unit/modules/memory/test_memory_wiring.py",
        "packages/arcrun/tests/test_executor.py",
        "packages/arcrun/tests/test_run_context.py",
        "packages/arcrun/tests/reliability/test_tool_ledger.py",
        "packages/arcrun/tests/reliability/test_tool_outcome_unknown.py",
    ),
    "hosted claim and journal refusal": (
        "packages/arctrust/tests/test_hosted_claim.py",
        "packages/arctrust/tests/test_hosted_journal.py",
        "packages/arctrust/tests/test_hosted_optional_import.py",
        "packages/arcui/tests/test_hosted_setup.py",
    ),
    # alpha-2 P5 skill revision authority: the zero-config local journal refuses
    # an edited line, a truncated or restored-older journal (rollback/downgrade),
    # a deleted seal, a replayed or cross-scope entry, a journal forged with
    # another key, and a symlinked journal; concurrent writers serialize. An
    # empty head beside signed revision evidence is a reset, never a fallback;
    # federal without an external anchor keeps every anchored route closed.
    # Covers ASI04/ASI06/LLM03 on the skill update and rollback path.
    "skill revision journal tampering, downgrade and forged signer": (
        "packages/arctrust/tests/test_file_journal_anchor.py",
        "packages/arcagent/tests/unit/capabilities/test_capability_import_revision.py",
        "packages/arcagent/tests/unit/capabilities/test_skill_revision_update_path.py",
        "packages/arcagent/tests/unit/core/test_skill_revision_anchor_config.py",
        "packages/arccli/tests/test_skill_revision_authority.py",
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
    # Browser sign-in for host binaries (Google accounts via gog --remote): the
    # pasted callback address is untrusted input that becomes one argv value of a
    # program holding an OAuth client. A lookalike consent host, a non-loopback or
    # wrong-path callback, flag text in the account or the address, a complete
    # with no begin or for another connection, a second begin over a waiting one,
    # a replayed spent code, and a hung binary must all fail closed — most before
    # any process runs — be audited, and never write the single-use code anywhere
    # Arc writes. Driven through the real routes and runner against a fake gog.
    # Covers LLM01/LLM05/LLM10 and ASI02/ASI03/ASI05.
    "browser sign-in abuse — pasted callback, argv injection, out-of-order steps": (
        "packages/arcagent/tests/unit/extension/test_remote_login.py",
        "packages/arcagent/tests/unit/extension/test_host_remote_login.py",
        "packages/arcagent/tests/integration/test_connector_remote_login.py",
        "packages/arcui/tests/integration/test_google_remote_sign_in_e2e.py",
    ),
    # One tool name, several granted accounts (Google via gog): the account a call
    # acts as is resolved against THIS agent's grants, never believed. An agent
    # granted one account naming another (exact, case/space/Unicode variants,
    # aliases, connection names), flags smuggled into other arguments
    # (--account, -a, --client, --home, GOG_*=), concurrent calls for two
    # accounts, a read-only sign-in asked to write, oversized pages/answers and
    # attachment path escapes must all fail closed, audited with the REAL
    # connection. Covers ASI02/ASI03/LLM06/LLM10.
    "multi-account routing — confused deputy across granted connections": (
        "packages/arcagent/tests/unit/modules/connectors/test_routing.py",
        "packages/arcagent/tests/unit/extension/test_cli_attachment_bounds.py",
        "packages/arcagent/tests/integration/test_google_account_routing.py",
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
