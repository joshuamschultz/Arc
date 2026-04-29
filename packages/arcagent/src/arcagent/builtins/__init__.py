"""SPEC-021 — built-in capabilities shipped with the agent.

This package is the first scan root walked by :class:`CapabilityLoader`
(SPEC-021 R-001). Everything inside ``capabilities/`` is loaded
unconditionally: the 7 file/exec tools (read/write/edit/bash/grep/find/ls),
the 5 self-mod tools (reload/create_tool/create_skill/update_tool/update_skill),
and the 4 self-mod skill folders that teach the LLM the convention.
"""
