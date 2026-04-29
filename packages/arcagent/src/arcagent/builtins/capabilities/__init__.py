"""Built-in capabilities — SPEC-021 C-009.

Each ``.py`` here exposes one ``@tool``-decorated async function. The
:class:`~arcagent.core.capability_loader.CapabilityLoader` finds them
via the ``_arc_capability_meta`` stamp; no explicit registration list.

Tools that need workspace context (read, write, edit, bash, etc.) read
it from :mod:`arcagent.builtins.capabilities._runtime`. The agent
calls :func:`arcagent.builtins.capabilities._runtime.configure` once
at startup; tools then call :func:`workspace` lazily at execution
time.
"""
