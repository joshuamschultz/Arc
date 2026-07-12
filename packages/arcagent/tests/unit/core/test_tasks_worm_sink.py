"""SPEC-056 SEC-F1 — the tasks module is a WORM audit-sink module.

``configure_module_runtimes`` only threads the real operator ``Signer`` into
modules listed in ``_WORM_SINK_MODULE_NAMES`` (shrinking the in-process
signing-authority attack surface). The tasks module builds a live messenger
whose ``AuditLogger`` signs the ``message.sent`` WORM chain, so it MUST be on
that allowlist — otherwise it falls back to a repudiable key (AU-9/10).
"""

from __future__ import annotations


def test_tasks_is_a_worm_sink_module() -> None:
    from arcagent.core.agent_lifecycle import _WORM_SINK_MODULE_NAMES

    assert "tasks" in _WORM_SINK_MODULE_NAMES
