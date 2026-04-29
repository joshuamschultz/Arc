"""ProfileStore — atomic markdown read/write for per-user profiles.

Design decisions:
- Writes are atomic: write to a temp file in the same directory, then
  os.replace().  A kill between write and rename leaves a .tmp file;
  the original is intact.
- The 2 KB body cap is enforced BEFORE writing.  When the cap would be
  exceeded the store raises BodyOverflow and emits a
  ``user_profile.overflow`` event via the telemetry bus so the caller
  can spill excess content to the episodic store.
- File permissions are set to 0o600 (owner read/write only) on every
  new profile file — same pattern as Hermes pairing codes.
- No silent truncation ever.  See GDPR obligations in SDD §3.6.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.errors import BodyOverflow, ProfileNotFound
from arcagent.modules.user_profile.models import ACL, DurableFact, UserProfile

_logger = logging.getLogger("arcagent.modules.user_profile.store")


class ProfileStore:
    """Read/write per-user profile markdown files atomically.

    Parameters:
        workspace:  Root workspace directory for the agent.
        config:     UserProfileConfig (body cap, directory names, etc.)
        telemetry:  Optional telemetry object; must have an
                    ``emit_event(name, data)`` method.  When ``None``
                    overflow events are logged but not emitted.
    """

    def __init__(
        self,
        workspace: Path,
        config: UserProfileConfig | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self._config = config or UserProfileConfig()
        self._workspace = workspace
        self._profile_dir = workspace / self._config.profile_dir
        self._telemetry = telemetry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def profile_path(self, user_did: str) -> Path:
        """Return the file path for a given user DID (does not check existence)."""
        safe = _did_to_filename(user_did)
        return self._profile_dir / f"{safe}.md"

    def exists(self, user_did: str) -> bool:
        """Return True if a profile file exists for the user."""
        return self.profile_path(user_did).is_file()

    def read(self, user_did: str) -> UserProfile:
        """Read and parse the profile for *user_did*.

        Raises:
            ProfileNotFound: if no profile file exists.
        """
        path = self.profile_path(user_did)
        if not path.is_file():
            raise ProfileNotFound(user_did)
        text = path.read_text(encoding="utf-8")
        profile = UserProfile.from_markdown(text)
        _logger.debug("user_profile.read user_did=%s", user_did)
        return profile

    def write(self, profile: UserProfile) -> None:
        """Atomically write *profile* to disk.

        Enforces the 2 KB body cap.  If the rendered body exceeds
        ``config.body_cap_bytes`` this method raises :class:`BodyOverflow`
        and emits the ``user_profile.overflow`` event.  The caller is
        responsible for spilling excess content to the episodic store.

        Raises:
            BodyOverflow: body exceeds cap; no write performed.
        """
        text = profile.to_markdown()
        body_size = _body_size(text)

        if body_size > self._config.body_cap_bytes:
            self._emit_overflow(profile.user_did, body_size)
            raise BodyOverflow(body_size, self._config.body_cap_bytes)

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.profile_path(profile.user_did), text)
        _logger.debug(
            "user_profile.write user_did=%s body_bytes=%d",
            profile.user_did,
            body_size,
        )

    def create_default(
        self,
        user_did: str,
        *,
        classification: str = "unclassified",
        agent_read: bool = True,
        cross_user_shareable: bool = False,
    ) -> UserProfile:
        """Create and persist a blank profile for *user_did*.

        Returns the newly created :class:`UserProfile`.
        """
        profile = UserProfile(
            user_did=user_did,
            created=datetime.now(tz=UTC),
            classification=classification,
            acl=ACL(
                owner=user_did,
                agent_read=agent_read,
                cross_user_shareable=cross_user_shareable,
            ),
        )
        self.write(profile)
        return profile

    def append_durable_fact(
        self,
        user_did: str,
        *,
        content: str,
        source_session_id: str,
        ts: datetime | None = None,
    ) -> UserProfile:
        """Append a durable fact to an existing profile.

        Creates the profile if it doesn't exist.  Fact is appended —
        existing facts are never removed or modified.

        Raises:
            BodyOverflow: if appending would exceed the 2 KB cap.
        """
        if not self.exists(user_did):
            profile = self.create_default(user_did)
        else:
            profile = self.read(user_did)

        fact = DurableFact(
            content=content,
            source_session_id=source_session_id,
            ts=ts or datetime.now(tz=UTC),
        )
        # Build a new profile with the fact appended (profiles are not frozen)
        profile.durable_facts = [*profile.durable_facts, fact]
        self.write(profile)
        return profile

    def delete(self, user_did: str) -> bool:
        """Delete the profile file for *user_did*.

        Returns True if the file existed and was deleted, False otherwise.
        This is the destructive half of the GDPR tombstone; tombstone.py
        orchestrates the full erasure workflow.

        Also deletes the agents/ annotation subdirectory if it exists, so
        per-agent annotations for this user are erased together with the
        base profile (TD-019 tombstone extension).
        """
        path = self.profile_path(user_did)
        deleted = False
        if path.is_file():
            path.unlink()
            _logger.info("user_profile.deleted user_did=%s", user_did)
            deleted = True

        # Sweep agents/ annotation subdirectory (TD-019).
        agents_dir = self._agent_annotations_dir(user_did)
        if agents_dir.is_dir():
            shutil.rmtree(agents_dir, ignore_errors=True)
            _logger.info(
                "user_profile.agents_dir_deleted user_did=%s agents_dir=%s",
                user_did,
                agents_dir,
            )

        return deleted

    # ------------------------------------------------------------------
    # Per-agent annotation API (SDD §3.6 Federation, TD-019)
    # ------------------------------------------------------------------

    def agent_annotation_path(self, user_did: str, agent_did: str) -> Path:
        """Return the file path for a per-agent annotation (no existence check).

        Layout: {profile_dir}/{user_did_safe}/agents/{agent_did_safe}.md

        Keeping the annotation inside a sub-directory of the user's profile
        preserves the 1-file-per-user namespace for the base profile while
        letting each agent maintain isolated notes about the user.
        """
        user_safe = _did_to_filename(user_did)
        agent_safe = _did_to_filename(agent_did)
        return self._profile_dir / user_safe / "agents" / f"{agent_safe}.md"

    def _agent_annotations_dir(self, user_did: str) -> Path:
        """Return the agents/ sub-directory for a user (no existence check)."""
        user_safe = _did_to_filename(user_did)
        return self._profile_dir / user_safe / "agents"

    def write_agent_annotation(
        self,
        user_did: str,
        agent_did: str,
        section: str,
        content: str,
    ) -> None:
        """Write (or overwrite) a per-agent annotation section to disk.

        Annotations are stored at:
        {profile_dir}/{user_did}/agents/{agent_did}.md

        The file is a simple markdown document keyed by *section* header.
        Each call replaces the entire file with the provided *content* under
        the given *section* heading.  For multi-section annotation files,
        callers must supply the full desired content on each write (no
        partial-section patching to keep the implementation simple and the
        GDPR tombstone correct).

        Args:
            user_did:   User DID whose annotation is being written.
            agent_did:  Agent DID writing the annotation.
            section:    Markdown heading for the annotation section (e.g.
                        "Preferences", "Observations").
            content:    Annotation body text.

        Raises:
            BodyOverflow: if the rendered annotation exceeds body_cap_bytes.
        """
        annotation_text = "# " + section + "\n\n" + content + "\n"
        body_size = len(annotation_text.encode("utf-8"))

        if body_size > self._config.body_cap_bytes:
            self._emit_overflow(user_did, body_size)
            raise BodyOverflow(body_size, self._config.body_cap_bytes)

        path = self.agent_annotation_path(user_did, agent_did)
        _atomic_write(path, annotation_text)
        _logger.debug(
            "user_profile.agent_annotation_written user_did=%s agent_did=%s section=%r",
            user_did,
            agent_did,
            section,
        )

    def read_user_profile(
        self,
        user_did: str,
        agent_did: str | None = None,
    ) -> str:
        """Read the base profile, optionally merged with a per-agent annotation.

        Returns the raw markdown string so callers can inject it into the
        agent's context window directly.

        Merge semantics:
        - Base profile is always the canonical record.
        - When *agent_did* is provided and an annotation file exists, its
          content is appended after the base profile under an
          ## Agent Annotations heading.  If the annotation file does not
          exist, only the base profile is returned.

        Args:
            user_did:   User whose profile to read.
            agent_did:  Optional agent DID; when provided the annotation for
                        this agent is merged in.

        Raises:
            ProfileNotFound: if no base profile file exists for user_did.
        """
        base_path = self.profile_path(user_did)
        if not base_path.is_file():
            raise ProfileNotFound(user_did)

        base_text = base_path.read_text(encoding="utf-8")
        _logger.debug(
            "user_profile.read_user_profile user_did=%s agent_did=%s",
            user_did,
            agent_did,
        )

        if agent_did is None:
            return base_text

        # Attempt to merge per-agent annotation overlay.
        annotation_path = self.agent_annotation_path(user_did, agent_did)
        if not annotation_path.is_file():
            return base_text

        annotation_text = annotation_path.read_text(encoding="utf-8")
        merged = (
            base_text.rstrip() + "\n\n## Agent Annotations\n\n" + annotation_text.strip() + "\n"
        )
        _logger.debug(
            "user_profile.merged_annotation user_did=%s agent_did=%s",
            user_did,
            agent_did,
        )
        return merged

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _emit_overflow(self, user_did: str, body_size: int) -> None:
        """Emit overflow event and log a warning.

        The event signals downstream code to spill content to the
        episodic store.  It carries a pointer (profile path) so the
        episodic store knows where to look.
        """
        pointer = str(self.profile_path(user_did))
        _logger.warning(
            "user_profile.overflow user_did=%s body_bytes=%d cap_bytes=%d pointer=%s",
            user_did,
            body_size,
            self._config.body_cap_bytes,
            pointer,
        )
        if self._telemetry is not None:
            try:
                self._telemetry.emit_event(
                    "user_profile.overflow",
                    {
                        "user_did": user_did,
                        "body_size": body_size,
                        "cap_bytes": self._config.body_cap_bytes,
                        "episodic_pointer": pointer,
                    },
                )
            except Exception:
                _logger.exception("Failed to emit user_profile.overflow event")


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------


def _did_to_filename(user_did: str) -> str:
    """Convert a DID to a filesystem-safe filename component.

    Replaces characters that are illegal on Windows/macOS/Linux with
    underscores.  The result is deterministic and reversible only to
    the degree needed — we don't need to reverse it (we use the DID
    stored in frontmatter as the canonical identifier).
    """
    return user_did.replace("/", "_").replace(":", "_").replace("\\", "_")


def _body_size(markdown_text: str) -> int:
    """Return byte size of the body (everything after closing frontmatter ``---``)."""
    # Split on the second '---' fence; the body is the third element.
    parts = markdown_text.split("---\n", 2)
    if len(parts) < 3:
        return len(markdown_text.encode("utf-8"))
    return len(parts[2].encode("utf-8"))


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically using a temp file + os.replace().

    If the process is killed between the write() and replace() calls
    the original file at *path* is left intact; only the temp file is
    orphaned.  Temp files are written to the same directory as *path* so
    that os.replace() is guaranteed to be within the same filesystem.
    """
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix=path.stem + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # Secure permissions before making the file world-accessible via rename
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the temp file so we don't leave garbage behind
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
