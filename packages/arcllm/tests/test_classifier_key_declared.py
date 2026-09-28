"""SPEC-083 T-1216 (COMP-026) — the Jev drop-in declares its key coordinate.

Decision #12: one fleet-wide ``TYPESAFE_API_KEY`` lives in the existing
write-only key store. That store accepts only names *some* arcllm component
declares, so the declaration has to come from the classifier drop-in itself —
never from a vendor name hard-coded in arcagent, arcrun or a UI.

Decision #13: the drop-in is removable. With the folder physically absent the
coordinate must disappear from the declaration, and nothing else may break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import arcllm
import arcllm.classifiers as classifiers

JEV_KEY = "TYPESAFE_API_KEY"


def test_classifier_key_envs_declares_the_jev_key() -> None:
    assert JEV_KEY in classifiers.classifier_key_envs()


def test_classifier_key_envs_is_reachable_from_the_arcllm_root() -> None:
    # One root import, qualified names: a key store reaches this through `arcllm`.
    assert JEV_KEY in arcllm.classifier_key_envs()


def test_classifier_key_envs_is_immutable() -> None:
    envs = classifiers.classifier_key_envs()
    assert isinstance(envs, frozenset)


def test_classifier_key_envs_holds_only_plain_env_var_names() -> None:
    # Every declared name ends up as a line in arc.env; nothing but an env-var
    # identifier may ever get there.
    for env in classifiers.classifier_key_envs():
        assert env.isidentifier(), env
        assert env == env.upper(), env


def test_a_removed_jev_drop_in_declares_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate "delete the folder": the scan root holds no drop-ins at all.
    empty_root = tmp_path / "classifiers"
    empty_root.mkdir()
    monkeypatch.setattr(classifiers, "__path__", [str(empty_root)])

    assert JEV_KEY not in classifiers.classifier_key_envs()
    assert classifiers.classifier_key_envs() == frozenset()
