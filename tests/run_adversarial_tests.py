#!/usr/bin/env python3
"""Run the cross-package zero-trust adversarial regression battery."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
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
        "packages/arcmemory/tests/security/test_per_folder_index_tamper.py",
        "packages/arcmemory/tests/unit/test_collection_index_pipeline.py",
        # Verify-seam review (item 62): a poisoned .dirty journal (absolute path,
        # symlinked folder, oversized) writes nothing outside memory/; a mixed
        # unlabeled + classified folder never lowers the routing chunk's label;
        # the walker never follows a symlink into connected/; a future-dated forged
        # index is never reused; one bad title never stops a drain; walker drops
        # are audited.
        "packages/arcmemory/tests/security/test_okf_verify_review.py",
        # Locator traversal (remote-controlled paths stay inside the source root,
        # never through a planted symlink) and log.md forgery (a forged log or
        # archive is discarded and never merged; reserved-file structure enforced).
        "packages/arcmemory/tests/unit/test_connected_layout.py",
        "packages/arcmemory/tests/unit/index/test_okf_log.py",
        "packages/arcokf/tests/test_log.py",
        # Item 62 F4/F6: index and log sidecars are agent-signed (.okf.seal). A forged
        # index or log with a recomputed sidecar, an older signed pair, a replayed
        # lower-generation seal, a seal from another collection or key, and unsigned
        # legacy sidecars all fail closed and heal from the documents; routing labels
        # come from the documents; a symlink swapped in between verify and read is
        # never served; a crash between the index and log writes loses no log line.
        "packages/arcmemory/tests/security/test_okf_signed_sidecars.py",
        "packages/arcokf/tests/test_safe_read.py",
        # The operator's repository-index view walks folders, verified deep, and
        # refuses a folder argument outside the source.
        "packages/arcui/tests/integration/test_doc_repo_index_route.py",
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
        "packages/arcrun/tests/test_causal_binding.py",
        "packages/arctrust/tests/test_audit_signer_verifier.py",
        "packages/arcstore/tests/integration/test_ingest_verify.py",
        "packages/arcui/tests/unit/test_audit_causality.py",
        "packages/arcagent/tests/unit/core/test_tool_dispatch_causality.py",
        "packages/arcagent/tests/unit/extension/test_connection_actor.py",
        "packages/arcagent/tests/unit/utils/test_spawn_background_detached.py",
        "packages/arcagent/tests/security/test_causal_turn_binding.py",
        "packages/arcagent/tests/unit/modules/tasks/test_task_causality.py",
        "packages/arcagent/tests/unit/extension/test_connector_call_causality.py",
        "packages/arcagent/tests/unit/core/test_durable_security_audit.py",
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
    "cross-channel read and classification laundering via a channel (alpha-2 item 58)": (
        # A channel member reads only its own channels; a message above the
        # reader's clearance stays withheld even when the channel was later
        # lowered; a channel above clearance is denied outright; other agents'
        # un-addressed posts stay out of context; every read is audited.
        "packages/arcagent/tests/unit/modules/messaging/test_read_channel.py",
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
    # Workflow runner state: a stale executor or a forged attempt key cannot
    # complete or fail a newer attempt; a stale revision cannot overwrite the
    # node snapshot; a replayed or concurrent gate resolution decides once; an
    # operator retry of a still-running run, a node that did not fail, or a
    # non-idempotent tool without operator OK is refused; test runs never run
    # state-modifying tools or unsigned scripts.
    "workflow runner attempt forgery, stale writers and replayed operator actions": (
        "packages/arcagent/tests/unit/modules/tasks/test_attempt_keys.py",
        "packages/arcagent/tests/unit/modules/tasks/test_attempt_safety.py",
        "packages/arcagent/tests/unit/modules/tasks/test_workflow_test_mode.py",
        "packages/arcstore/tests/unit/test_runs_node_states.py",
        "packages/arcteam/tests/unit/workflow/test_gate_resolution.py",
        "packages/arcteam/tests/unit/workflow/test_control_plane_retry.py",
        "packages/arcteam/tests/unit/workflow/test_run_create_idempotence.py",
        "packages/arcteam/tests/unit/workflow/test_runner_occurrence.py",
    ),
    # Workflow gate approvers (alpha-2 #67): an unpaired user never reaches a
    # gate; a paired non-approver is denied and audited; a role typed into the
    # message or forged into Telegram callback data is ignored (roles come from
    # the team registry for the authenticated DID only); approvers written onto
    # the mutable row grant nothing; a replayed approve is refused; a gate naming
    # an undeclared role refuses the run at start.
    "workflow gate approver forgery, role injection and replayed decisions": (
        "packages/arcteam/tests/unit/workflow/test_gate_approvers.py",
        "packages/arcteam/tests/unit/workflow/test_registry_role_roster.py",
        "packages/arcgateway/tests/unit/test_gate_authorization.py",
        "packages/arcgateway/tests/platform/telegram/test_telegram_gate_buttons.py",
        "packages/arccli/tests/test_gate_cli.py",
    ),
    # Workflow node retry (alpha-2 #68/#69/#71): a repeat of a non-idempotent side
    # effect runs only when an operator says so (viewers and non-boolean accepts are
    # refused, a retried row is a re-run from its first claim, the accept is audited);
    # two racing first starts announce one run; a test stub that cannot satisfy its
    # output_schema fails the test run instead of passing on an echo.
    "workflow retry accept forgery, racing first start and unsound test stubs": (
        "packages/arcteam/tests/unit/workflow/test_control_plane_retry_accept.py",
        "packages/arcteam/tests/unit/workflow/test_runner_occurrence.py",
        "packages/arcagent/tests/unit/modules/tasks/test_attempt_safety_operator_retry.py",
        "packages/arcagent/tests/unit/modules/tasks/test_workflow_test_mode_schema.py",
        "packages/arcui/tests/test_workflow_routes.py",
        "packages/arccli/tests/test_workflow_command.py",
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
    # Alpha-2 item 16: one durable decision per card. A demoted card is never
    # re-sent or re-published (not by the classifier, not by hand, not after an
    # edit, not after the tombstone is deleted); a forged/unsigned operator ledger
    # row is never a decision; a tombstone signed by any key but the anchored
    # operator, impersonating the operator DID, or replayed onto another document
    # is not a demotion; a tombstone naming a file outside the retired set is never
    # read; no contributor re-promotes into a demoted identifier; a second operator
    # key cannot demote; forged provenance is dropped; the secret gate has no
    # operator override; an operator share names a decider the browser cannot
    # supply, is refused at federal and is audited. Revoked bytes are retired,
    # never erased (AU-9/AU-11).
    "shared knowledge demote, sticky decisions and operator share (alpha-2 item 16)": (
        "packages/arcteam/tests/security/test_shared_knowledge_demote_abuse.py",
        "packages/arcmemory/tests/promotion/test_ledger.py",
        "packages/arcmemory/tests/promotion/test_sweep.py",
        "packages/arcui/tests/integration/test_knowledge_shared_routes.py",
        "packages/arcui/tests/test_memory_share_routes.py",
    ),
    # Alpha-2 item 16 follow-up: a card shares at its OWN stored label when that is
    # below the agent's clearance (declassified-at-source), and nowhere else. A
    # label above the clearance is refused; a missing or unknown label is the
    # clearance (fail upward); a caller-named or unattested lower label, an
    # undecided write and a direct backend save are all still no-write-down.
    # Entity de-dup folds one card's facts into another. A series lookalike name,
    # a hostile confirmer naming cards across levels, the model-callable merge
    # primitive aimed across levels, and an unknown label at federal all fold
    # nothing: a merge never moves a fact across a classification level.
    "classification laundering via entity merge (de-dup across levels)": (
        "packages/arcmemory/tests/security/test_entity_merge_abuse.py",
        "packages/arcmemory/tests/unit/test_entity_dedup.py",
    ),
    "classification laundering via a forged lower shared label (alpha-2 item 16)": (
        "packages/arcteam/tests/security/test_declassified_share.py",
        "packages/arcagent/tests/modules/memory/test_promotion_bridge.py",
        "packages/arcmemory/tests/unit/test_memory_export.py",
        "packages/arcmemory/tests/promotion/test_sweep.py",
    ),
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
    "browser leaving kills a long run, or an unwatched run never ends (DGX 2026-10-03)": (
        # The last socket leaving past every grace window never cancels the run; its
        # answer lands in session history; a run with no observer is still capped by
        # max_turns; only an explicit operator cancel (RunHandle.cancel) stops it.
        "packages/arcgateway/tests/unit/test_run_outlives_browser.py",
        "packages/arcrun/tests/security/test_unobserved_run_bounded.py",
        "packages/arcrun/tests/test_cancel_attribution.py",
    ),
    "stuck sync blocking revocation or chat (DGX 2026-10-03)": (
        # A six-hour sync holding a source lease never blocks operator removal
        # or a grant change; the in-process fast path is bounded; a turn that
        # cannot start fails visibly instead of freezing the channel.
        "packages/arcagent/tests/unit/extension/test_source_catalog.py",
        "packages/arcagent/tests/integration/test_connection_grants.py",
        "packages/arcagent/tests/unit/core/test_turn_start_bound.py",
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
    "unapproved, edited or replayed pulse check approval (P47)": (
        # A pulse check runs only from an operator-approved revision of its exact
        # text: unapproved and post-edit checks never fire, a forged approval line
        # is inert, a viewer cannot approve, and a stale or replayed approval
        # request is refused. Every outcome is audited.
        "packages/arcagent/tests/unit/modules/pulse/test_pulse_approval.py",
        "packages/arcui/tests/integration/test_pulse_approval_routes.py",
        "packages/arccli/tests/test_pulse_command.py",
        "tests/journeys/test_journey_pulse_approval.py",
    ),
    "viewer or agent adds, edits or removes a pulse check": (
        # pulse.md is operator-only: a viewer or unauthenticated caller is refused
        # (and audited) before anything is written, an agent can only file a
        # proposal (never write pulse.md, whose files its tools cannot touch),
        # field-marker and heading injection in the action text is refused or
        # flattened, and a write never approves: the check runs only after the
        # operator approves the exact text.
        "packages/arcagent/tests/unit/modules/pulse/test_pulse_editing.py",
        "packages/arcagent/tests/unit/modules/pulse/test_pulse_proposals.py",
        "packages/arcagent/tests/unit/modules/pulse/test_pulse_propose_tool.py",
        "packages/arcagent/tests/unit/tools/test_protected_paths.py",
        "packages/arcui/tests/integration/test_pulse_edit_routes.py",
        "packages/arccli/tests/test_pulse_command.py",
        "tests/journeys/test_journey_pulse_approval.py",
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
    # P14-B: a workflow node runs exactly once per attempt key. A replayed
    # attempt runs no second side effect or agent turn; a forged attempt key or
    # a reclaimed (stale) attempt cannot record a result; a writer holding a
    # stale node-state revision cannot overwrite the run's snapshot; a crash at
    # any durable write resumes without a duplicate row, journal entry or run.
    "workflow attempt replay and node-state forgery (P14-B)": (
        "packages/arcstore/tests/unit/test_task_attempts.py",
        "packages/arcstore/tests/unit/test_runs_node_states.py",
        "packages/arcagent/tests/unit/modules/tasks/test_attempt_keys.py",
        "packages/arcteam/tests/unit/workflow/test_runner_resume.py",
        "packages/arcteam/tests/unit/workflow/test_runner_infra_failures.py",
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
    # Native OAuth connect (alpha-2 P18-3): the callback address and the code in
    # it are untrusted input, and the account behind a consent is not believed. A
    # forged state, an attacker's state+code completed in the victim's session
    # (login swap), a replayed code, a PKCE verifier swapped between sign-ins, a
    # redirect steered by a Host header, a consent as another mailbox, a consent
    # that drops scopes, a viewer driving any verb, the app secret read back,
    # provider HTML in an error, and oversized / control-character / duplicated
    # pasted addresses must all fail closed with NOTHING stored (a refused grant is
    # revoked at the provider), audited without a code, token or email. Driven
    # through the real Connections seam and the real arcui routes against a fake
    # provider token endpoint. Covers LLM01/LLM02/LLM05/LLM10 and ASI02/ASI03/ASI07.
    (
        "native OAuth abuse — state forgery, login swap, redirect tampering, code replay "
        "(alpha-2 P18-3)"
    ): (
        "packages/arcagent/tests/unit/extension/test_oauth_flow.py",
        "packages/arcagent/tests/security/test_oauth_abuse.py",
        "packages/arcui/tests/security/test_oauth_routes_abuse.py",
        "tests/architecture/test_oauth_redirect_is_config_derived.py",
        "tests/architecture/test_no_vendor_cli_for_oauth_providers.py",
    ),
    # Microsoft 365 over Entra ID (GCC): a state/PKCE mismatch, an id_token from
    # another tenant/app/user, a slot swapped between begin and complete, a tenant-A
    # refresh token presented for slot B, an app slot whose cloud or tenant could
    # point a token at any host, a viewer connecting or reading the secret, the
    # refresh token in logs/audit/errors, Graph paging links off the pinned host,
    # and injected instructions in Graph data. LLM01/LLM02/LLM05, ASI02/ASI03.
    (
        "Microsoft 365 native OAuth abuse — tenant binding, cloud pinning, Graph egress "
        "(alpha-2 P18-3.M)"
    ): (
        "packages/arcagent/tests/unit/extension/test_oauth_tenant_cloud.py",
        "packages/arcagent/tests/security/test_microsoft365_oauth_abuse.py",
        "packages/arcui/tests/security/test_microsoft365_oauth_routes_abuse.py",
        "extensions/tests/test_microsoft365_native.py",
        "extensions/tests/test_mail_source_recurring.py",
    ),
    # One tool name, several granted accounts (Google, native REST): the account a
    # call acts as is resolved against THIS agent's grants, never believed. An
    # agent granted one account naming another (exact, case/space/Unicode
    # variants, aliases, connection names), concurrent calls for two accounts, a
    # read-only connection asked to write, header injection in outgoing mail,
    # oversized pages/answers and attachment path escapes must all fail closed,
    # audited with the REAL connection. Covers ASI02/ASI03/LLM06/LLM10.
    "multi-account routing — confused deputy across granted connections": (
        "packages/arcagent/tests/unit/modules/connectors/test_routing.py",
        "packages/arcagent/tests/unit/extension/test_cli_attachment_bounds.py",
        "packages/arcagent/tests/integration/test_google_account_routing.py",
    ),
    # Atlassian 3LO (alpha-2 P18-3, 3.T): Atlassian ROTATES refresh tokens, so a
    # race between two processes, a replayed old token, a consent for another
    # organisation's site (or a site id that changed under a reconnect), a
    # two-site account that never chose, and an issue key that climbs the REST
    # path must each fail closed with nothing stored and no token spent twice.
    # Issue text an attacker authored is framed untrusted. Covers ASI02/ASI03/LLM01.
    "Atlassian 3LO abuse — rotation race, reused refresh token, site rebinding (alpha-2 P18-3)": (
        "packages/arcagent/tests/integration/test_atlassian_connect.py",
        "extensions/tests/test_atlassian_native_tools.py",
    ),
    # GitHub token (alpha-2 P18-3, 3.H): the vaulted token is placed as GH_TOKEN
    # per spawn and nowhere else. It must never reach disk, a log, a card or a
    # tool result (the child may echo it); gh must never read the operator's own
    # config; a manifest's fixed environment may never name a credential, PATH or
    # a loader variable; a token inside seven days of expiry is needs_you.
    "GitHub token injection — env placement, isolated config, expiry (alpha-2 P18-3)": (
        "packages/arcagent/tests/integration/test_github_token.py",
    ),
    # Item 52: the production local schedule authority. A forged or hand-edited
    # approval, a stale or replayed registration, a proof from a key that is not
    # the actor's, an edited journal, a revoked head, a replayed occurrence and an
    # unsigned or forged row planted in schedules.json are all refused; the
    # journey drives the real loader with no stand-in authority.
    "forged schedule, replayed registration and unsigned schedule file": (
        "packages/arctrust/tests/test_control_authority.py",
        "packages/arcui/tests/integration/test_schedule_write_routes.py",
        "packages/arccli/tests/test_serve_control_authority.py",
        "tests/journeys/test_journey_schedules.py",
    ),
    # Item 61: a deleted schedule is revoked (its old approval and a re-planted
    # row never fire), an admitted occurrence stays admitted across a process
    # restart with a torn or planted-junk log, and every entry point
    # (`python -m arcagent`, arcui serve) carries the authority fail-closed.
    "revoked schedule, persisted occurrence replay and unwired entry points": (
        "packages/arctrust/tests/test_control_authority.py",
        "packages/arcagent/tests/unit/modules/scheduler/test_scheduler_capabilities.py",
        "packages/arcagent/tests/unit/test_serve_cli.py",
        "packages/arcui/tests/test_serve_control_authority.py",
        "packages/arccli/tests/test_serve_control_authority.py",
        "tests/journeys/test_journey_schedules.py",
    ),
    # P18-1: a connection's health record is what the operator trusts to decide
    # whether to reconnect an account, so it must not be forgeable by an agent tool
    # or a viewer, floodable into a notice storm, injectable through provider error
    # text, leaky about credentials, or silently believed after a row is tampered
    # with. The architecture test keeps the authority the only writer.
    "connection health forgery, notice flooding and status-read leakage (alpha-2 P18-1)": (
        "packages/arcagent/tests/security/test_connection_health_abuse.py",
        "packages/arcui/tests/security/test_connections_card_abuse.py",
        "packages/arcagent/tests/unit/extension/test_connection_health_cas.py",
        "tests/architecture/test_connection_health_single_writer.py",
    ),
    # P20-5: the audit trail is evidence, so a read must not write to it (a viewer
    # could otherwise flood the signed chain), a forged causal header must not choose
    # the actor, filter values are opaque ids (never query syntax), and only an
    # operator may ask the ledger to re-verify itself.
    "audit trail: read writes no chain row, forged actor, filter injection (alpha-2 P20-5)": (
        "packages/arcui/tests/unit/test_audit_trail_api.py",
        "packages/arcui/tests/unit/test_audit_causality.py",
    ),
    # P20-7: the causal-audit contract on the real path. A channel tool call's signed
    # row names its run, tool call, model call, agent and requesting user; two users
    # at once (Barrier-forced) and a background job spawned mid-run never borrow each
    # other's ids; a timed probe never borrows the page view in flight; a viewer's
    # refused change is a denial row on any route while a page view writes nothing;
    # one flipped byte breaks that row and every later one with one marker (also
    # across a restart); a rotated chain verifies without duplicates; and every audit
    # API filter returns exactly the rows the raw chain says it should.
    "audit causality contract: attribution, isolation, refusals, chain verify (alpha-2 P20-7)": (
        "tests/journeys/test_journey_audit.py",
        "packages/arcui/tests/unit/test_ui_mutation_audit.py",
    ),
    # P12: an operator-added MCP server is third-party code on a wire or a child process.
    # The generated bundle is the trust boundary: stdio commands launch only an allowed,
    # absolute, shell-free executable; http is https-only with an SSRF guard (link-local,
    # metadata and numeric-trick hosts refused); tools are namespaced so none shadows a
    # built-in and classified by the operator, never by the server's annotations; a server
    # that rewrites an approved tool is suspended and one that grows new tools exposes
    # none; a swapped origin, an edited or added file, a symlinked bundle directory and a
    # name collision are all refused; and the credential never reaches the spec, the
    # bundle, argv, a log, an audit event, an error or a response.
    "operator-added MCP server: launch, egress, shadowing, rug-pull, leakage (alpha-2 P12)": (
        "packages/arcagent/tests/modules/connectors/test_mcp_bundle.py",
        "packages/arcagent/tests/integration/test_add_mcp_server_contract.py",
        "packages/arcagent/tests/security/test_mcp_server_abuse.py",
        "packages/arcui/tests/test_mcp_server_routes.py",
        "packages/arccli/tests/test_cli_connector_add_mcp.py",
    ),
    # P12: a hostname judged safe when the operator added the server can be re-pointed
    # afterwards (DNS rebinding). The http client resolves the host itself on every
    # connect, judges EVERY address (link-local, metadata, multicast, unspecified always;
    # loopback and private above personal unless allowlisted), connects to the address it
    # judged while keeping the name for Host and TLS SNI, and audits the refusal: a public
    # first answer then 169.254.169.254 on the second connect never reaches the network.
    "operator-added MCP server: DNS rebinding pinned to the validated address (alpha-2 P12)": (
        "packages/arcagent/tests/security/test_mcp_dns_rebinding.py",
    ),
    # P18-2: connector credentials live in sealed custody rows. A replayed or stalled
    # renewal commit is refused by the fenced lease; a ciphertext copied between
    # connections fails to open (AAD) and never reaches the provider; a handle dies
    # with its grant; no agent reads another's token; no refresh token, client secret
    # or access token reaches a log, audit event or tool result; a DB reader without
    # the operator key learns nothing; a forged lease cannot block renewal; a
    # symlinked legacy file is refused; a restored old row ends honestly in needs_you.
    "connector credential custody — replayed refresh, rotate TOCTOU, stale handle, "
    "cross-agent read (alpha-2 P18-2)": (
        "packages/arcagent/tests/security/test_credential_custody_abuse.py",
        "packages/arctrust/tests/test_connector_cipher.py",
        "packages/arcagent/tests/unit/extension/test_renewal_planner.py",
        "tests/architecture/test_no_connector_secret_on_disk.py",
    ),
    # Alpha-2 hotfix (DGX 9c280994 lost nine values): the legacy credential-file
    # migration never silently drops a credential. An undeclared value stops it (file
    # kept, nothing written); only an explicit --drop-undeclared drops one, audited
    # per key; a stale or planted file never overwrites custody or an app slot.
    "legacy credential-file migration never silently drops a credential (alpha-2 hotfix)": (
        "packages/arcagent/tests/security/test_migration_never_drops_credentials.py",
        "packages/arcagent/tests/unit/extension/test_custody_migrate_no_loss.py",
        "packages/arcui/tests/test_startup_migrates_connections_env.py",
    ),
    # Vault Transit adapter: a look-alike Vault (other CA) or a redirect never
    # receives the secret_id; a key swapped behind a pinned name cannot sign for the
    # operator; a weakened (exportable/derived/backup) key is never used; a
    # transplanted credential does not open; an outage never falls back; no token
    # or secret lands in the deployment tree; env cannot repoint the transit; an
    # audit sink that signs through the transit cannot deadlock it.
    "HashiCorp Vault Transit custody — rogue Vault, redirect, key swap, weakened "
    "key, transplant, outage fallback, env repoint (alpha-2 P18-2F Vault)": (
        "packages/arctrust/tests/test_vault_transit_abuse.py",
        "packages/arctrust/tests/test_transit_contract.py",
        "packages/arcagent/tests/unit/core/test_security_vault_config.py",
    ),
    # P18-2F: under vault_transit the custody key never enters the process. The old
    # in-process seed opens nothing; a transplanted or downgraded (seed-planted xc1)
    # value is refused; an outage fails closed with no in-process fallback; a
    # poisoned PYTHONPATH never reaches the notary child; AAD swap and tamper fail;
    # a reseal is one verified CAS per row, idempotent and crash-safe.
    "connector credentials in Vault Transit — stolen seed, transplant, downgrade, "
    "outage fallback, child env poisoning (alpha-2 P18-2F)": (
        "packages/arcagent/tests/security/test_transit_custody_abuse.py",
        "packages/arctrust/tests/test_connector_transit_cipher.py",
        "packages/arcagent/tests/unit/extension/test_custody_reseal.py",
    ),
    # J1-3: the Install button puts a helper on the operator's machine. A download
    # whose digest is not the pinned one installs nothing; an npm tarball with a
    # traversal entry is refused before npm sees it; a postinstall script never runs
    # (--ignore-scripts, proven against real npm); federal never fetches an
    # un-allowlisted digest; the install record is found by path, not PATH.
    "host helper install — swapped download, tarball traversal, postinstall, "
    "federal fetch (alpha-2 J1-3)": (
        "packages/arcagent/tests/security/test_host_install_abuse.py",
        "packages/arcagent/tests/unit/extension/test_host_install.py",
    ),
    # P18-2: nothing executes from the operator tree. A code-bearing bundle planted
    # in ~/arc/extensions is refused by name (audited) and one in
    # ~/arc/state/extensions is not on the search path, at every tier; a config-only
    # MCP bundle there must verify; install-bundle verifies before copying into
    # ~/.arc/extensions and the loader verifies again, so a post-install edit fails.
    "planted extension code in the operator tree (alpha-2 P18-2)": (
        "packages/arcagent/tests/security/test_operator_tree_extensions.py",
    ),
    # P18-4: one store per connection, shared by every agent granted it. A read is
    # the reading agent's own (another DID is refused and audited), only through its
    # own subscription (never granted, not yet approved or revoked reads nothing,
    # even naming the pool id outright); a writer whose subscription is gone or was
    # verified against another approval writes nothing; a reader's port writes
    # nothing; a store embedded one way is never read with another embedder.
    "connection-scoped knowledge — cross-agent read, revoked or forged writer (alpha-2 P18-4)": (
        "packages/arcagent/tests/security/test_shared_knowledge_isolation.py",
        "packages/arcmemory/tests/unit/test_connected_shared_store.py",
        # The automatic move of an agent's own copy into the store needs that agent's
        # own approved shareable mapping; a failed read-back keeps the own copy.
        "packages/arcagent/tests/security/test_auto_migration_requires_grant.py",
        "tests/journeys/test_journey_shared_knowledge.py",
    ),
    # SPEC-035 OQ-3 (Josh, 2026-10-03): an operator's "Always allow" on a trifecta
    # approval stands for that agent. A grant for combination A never satisfies a
    # wider combination B; a grant for agent X never satisfies agent Y; a grant
    # for destination D1 (or one verb) never covers D2 (or another verb, or an
    # added recipient); a revoked grant prompts again on the next call; a forged,
    # foreign-key, agent-self-signed, edited or unsigned grant row is ignored; a
    # stored standing grant is refused at federal and never offered there; a
    # viewer can neither grant nor revoke; an interactive grant never stands in
    # for an automated driver.
    "standing trifecta approvals — widened, travelling, revoked or forged grants": (
        "packages/arctrust/tests/test_interactive_grant.py",
        "packages/arcstore/tests/unit/test_standing_grants.py",
        "packages/arcagent/tests/security/test_trifecta_repeat_prompts.py",
        "packages/arcui/tests/test_standing_grants_route.py",
        "packages/arccli/tests/test_approve_standing.py",
        "tests/journeys/test_journey_always_allow.py",
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


def _fail(message: str) -> None:
    sys.stderr.write(f"ADVERSARIAL BATTERY FAILED: {message}\n")
    sys.stderr.flush()


def _describe_exit(code: int) -> str:
    """A negative code is a signal (a crashed interpreter), not a pytest verdict."""
    if code < 0:
        return f"was killed by signal {-code}"
    return f"exited {code}"


def _failing_cases(report: Path) -> list[str]:
    """``file::test: first line of the reason`` for every failed or errored case."""
    lines: list[str] = []
    for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314 - our own pytest report
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        reason = (problem.get("message") or problem.text or "").strip().splitlines()
        shown = reason[0] if reason else "no message"
        lines.append(f"{case.get('classname', '')}::{case.get('name', '')}: {shown}")
    return lines


def _tests_run(report: Path) -> int:
    return sum(
        int(suite.get("tests", "0"))
        for suite in ET.parse(report).getroot().iter("testsuite")  # noqa: S314
    )


def verdict(report: Path, returncode: int) -> int:
    """Print what happened, on stderr, and return the exit status to use.

    The status is never zero unless pytest exited zero AND at least one test ran, and a
    non-zero status always comes with the failing suite and the reason.
    """
    if not report.is_file() or report.stat().st_size == 0:
        _fail(
            f"pytest {_describe_exit(returncode)} and wrote no test report; the run died "
            "before or outside the tests (collection crash, interpreter crash, or an "
            "exit in a conftest). Re-run the pytest command above directly to see why."
        )
        return returncode or 1
    failures = _failing_cases(report)
    for line in failures:
        _fail(line)
    if returncode != 0:
        if not failures:
            _fail(f"pytest {_describe_exit(returncode)} with no failing test in the report")
        return returncode
    if _tests_run(report) == 0:
        _fail("pytest exited 0 but ran no tests; the battery is not guarding anything")
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run pytest over the curated battery and return its exact exit status."""
    missing = [path for path in targets() if not (ROOT / path).is_file()]
    if missing:
        sys.stderr.write("adversarial manifest references missing tests:\n")
        sys.stderr.writelines(f"  - {path}\n" for path in missing)
        return 2

    for threat, paths in SCENARIOS.items():
        sys.stdout.write(f"{threat}: {len(paths)} suite(s)\n")
    with tempfile.TemporaryDirectory(prefix="arc-adversarial-") as isolated_home:
        report = Path(isolated_home) / "report.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            f"--junitxml={report}",
            *targets(),
            *(argv or []),
        ]
        environment = os.environ.copy()
        environment["HOME"] = isolated_home
        environment["ARC_CONFIG_DIR"] = str(Path(isolated_home) / ".arc")
        environment["ARC_TEAM_ROOT"] = str(Path(isolated_home) / "arc")
        returncode = subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode
        return verdict(report, returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
