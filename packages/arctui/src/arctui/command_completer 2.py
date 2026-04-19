"""Slash-command autocomplete for ArcTUI.

Reads from ``arccli.commands.registry`` via the public render helper
``autocomplete_dict()`` so that all six downstream consumers stay in sync
(SDD §3.11).

Design notes:
- Completion is triggered only when input starts with ``/``.  Plain text
  without a leading slash returns an empty completion list.
- Argument-level completion (e.g. ``/agent <subcommand>``) is handled by
  returning the args_hint as a placeholder string after the command name.
- No mutable global state; the completer is constructed fresh per ``ArcTUI``
  instance so there are no cross-session leaks.
- The ``resolve`` method is the hot path; it is called on every keystroke
  inside the InputComposer.  It must complete in < 1 ms.
"""

from __future__ import annotations

from dataclasses import dataclass

from arccli.commands.registry import COMMAND_REGISTRY, CommandDef, resolve_command
from arccli.commands.render import commands_by_category


@dataclass(frozen=True)
class Completion:
    """A single completion suggestion.

    Attributes
    ----------
    text:
        The full command text to substitute (no leading slash).
    description:
        Short one-line description from the registry.
    args_hint:
        Argument placeholder shown after the command name (may be empty).
    is_alias:
        True if ``text`` is an alias rather than the canonical name.
    """

    text: str
    description: str
    args_hint: str = ""
    is_alias: bool = False


class SlashCommandCompleter:
    """Autocomplete provider backed by ``arccli.commands.registry``.

    Parameters
    ----------
    include_cli_only:
        If True, ``cli_only`` commands are included in completions.
        Defaults to True because arctui is a CLI surface (D-15).

    Usage::

        completer = SlashCommandCompleter()
        suggestions = completer.resolve("/hel")
        # -> [Completion(text="help", description="Show available commands…")]
    """

    def __init__(self, *, include_cli_only: bool = True) -> None:
        self._include_cli_only = include_cli_only

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, partial: str) -> list[Completion]:
        """Return completions for the current input *partial*.

        Parameters
        ----------
        partial:
            The text typed so far.  MUST start with ``/`` to trigger
            completion; plain text without a leading slash returns an
            empty list.  Comparison is case-insensitive.

        Returns
        -------
        list[Completion]
            Matching completions, sorted alphabetically by ``text``.
            Returns an empty list if *partial* does not start with ``/``,
            if *partial* is just ``/`` (need at least one char after),
            or if the registry contains no matching entries.
        """
        # Require a leading slash — this is a slash-command completer.
        if not partial or not partial.startswith("/"):
            return []

        stripped = partial[1:].lower()  # strip exactly one leading slash

        # Empty after the slash → no completions shown
        if not stripped:
            return []

        results: list[Completion] = []
        seen: set[str] = set()

        for cmd in COMMAND_REGISTRY:
            if not self._include_cli_only and cmd.cli_only:
                continue

            # Canonical name match
            if cmd.name.lower().startswith(stripped) and cmd.name not in seen:
                seen.add(cmd.name)
                results.append(
                    Completion(
                        text=cmd.name,
                        description=cmd.description,
                        args_hint=cmd.args_hint,
                        is_alias=False,
                    )
                )

            # Alias match
            for alias in cmd.aliases:
                if alias.lower().startswith(stripped) and alias not in seen:
                    seen.add(alias)
                    results.append(
                        Completion(
                            text=alias,
                            description=cmd.description,
                            args_hint=cmd.args_hint,
                            is_alias=True,
                        )
                    )

        results.sort(key=lambda c: c.text)
        return results

    def resolve_exact(self, name: str) -> CommandDef | None:
        """Delegate to ``resolve_command`` for exact alias-aware lookup.

        Convenience wrapper used by ``InputComposer`` to dispatch commands.
        """
        return resolve_command(name)

    def all_commands_by_category(self) -> dict[str, list[CommandDef]]:
        """Return all commands grouped by category.

        Wraps ``arccli.commands.render.commands_by_category()`` for
        consumers that want the structured view rather than the flat
        autocomplete list.
        """
        return commands_by_category()

    def autocomplete_dict(self) -> dict[str, str]:
        """Return ``{name_or_alias: description}`` for all commands.

        Includes aliases.  Gateway-only commands are included because
        arctui is a CLI surface.
        """
        result: dict[str, str] = {}
        for cmd in COMMAND_REGISTRY:
            if not self._include_cli_only and cmd.cli_only:
                continue
            result[cmd.name] = cmd.description
            for alias in cmd.aliases:
                result[alias] = cmd.description
        return result
