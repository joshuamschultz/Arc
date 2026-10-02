"""Classifier drop-ins — folder-scanned, like arcrun strategies (ADR-032).

A classifier is a removable drop-in: any module or folder directly under
``arcllm/classifiers/`` that exports ``CLASSIFIER`` (a ``ClassifierProvider``
subclass constructible as ``CLASSIFIER(model)``, which also accepts the
keyword-only key coordinate ``api_key_env=`` / ``vault_path=``) and, optionally,
``API_KEY_ENV`` (the env var its key comes from by default). There is no central list
to edit. Delete a drop-in's folder and its name resolves to
``ArcLLMClassifierUnavailableError``; everything else keeps working.

The scan root is in-tree, so it ships and is signed with the release wheel.
Names are never dotted import paths from config and there are no setuptools
entry points (disabled at every tier, see ``arcrun/backends/policy.py``), so
resolution adds no untrusted-load surface.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable
from types import ModuleType
from typing import cast

from arcllm.classify import ArcLLMClassifierUnavailableError, ClassifierProvider
from arcllm.config import ProviderKey
from arcllm.exceptions import ArcLLMConfigError

# A drop-in name is a plain module name. Anything else ("os:system",
# "pkg.mod:Class", "no-such-classifier", "") is a config error, not a lookup.
_DROP_IN_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def _installed_names() -> frozenset[str]:
    """Drop-in names physically present in this folder, scanned per call."""
    return frozenset(
        info.name for info in pkgutil.iter_modules(__path__) if not info.name.startswith("_")
    )


def _import_drop_in(name: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{name}")


def load_classifier(name: str, model: str, **key_coordinate: str) -> ClassifierProvider:
    """Construct drop-in ``name`` for ``model``.

    ``key_coordinate`` (``api_key_env`` / ``vault_path``) is forwarded to the
    drop-in so the operator's configured key location reaches it.

    Raises:
        ArcLLMConfigError: ``name`` is malformed, or the drop-in does not
            export a ``ClassifierProvider`` subclass as ``CLASSIFIER``.
        ArcLLMClassifierUnavailableError: no drop-in named ``name`` is installed.
    """
    if not _DROP_IN_NAME.match(name):
        raise ArcLLMConfigError(
            f"Invalid classifier name {name!r}: use an installed drop-in name, not an import path."
        )
    if name not in _installed_names():
        raise ArcLLMClassifierUnavailableError(
            model, f"no classifier drop-in named '{name}' is installed"
        )
    cls = getattr(_import_drop_in(name), "CLASSIFIER", None)
    if not (isinstance(cls, type) and issubclass(cls, ClassifierProvider)):
        raise ArcLLMConfigError(
            f"Classifier drop-in '{name}' does not export a ClassifierProvider as CLASSIFIER"
        )
    # Drop-in contract (module docstring): CLASSIFIER(model, **key_coordinate).
    factory = cast("Callable[..., ClassifierProvider]", cls)
    return factory(model, **key_coordinate)


def list_classifier_models(name: str) -> tuple[str, ...]:
    """The pinned model names drop-in ``name`` declares as ``MODELS``.

    Raises:
        ArcLLMConfigError: ``name`` is malformed.
        ArcLLMClassifierUnavailableError: no drop-in named ``name`` is installed.
    """
    if not _DROP_IN_NAME.match(name):
        raise ArcLLMConfigError(
            f"Invalid classifier name {name!r}: use an installed drop-in name, not an import path."
        )
    if name not in _installed_names():
        raise ArcLLMClassifierUnavailableError(
            name, f"no classifier drop-in named '{name}' is installed"
        )
    models = getattr(_import_drop_in(name), "MODELS", ())
    return tuple(m for m in models if isinstance(m, str))


def list_classifier_keys() -> tuple[ProviderKey, ...]:
    """The key coordinate each installed drop-in declares, ordered by drop-in name.

    Same record shape as :func:`arcllm.list_provider_keys`, so one key store can
    allowlist LLM and classifier credentials alike without naming a vendor.
    ``required`` is always False: no classifier runs unless an operator turns on
    the feature that uses it, so a missing key is never a broken deployment.
    Drop-ins that declare no ``API_KEY_ENV`` contribute nothing.
    """
    keys: list[ProviderKey] = []
    for name in sorted(_installed_names()):
        env = getattr(_import_drop_in(name), "API_KEY_ENV", None)
        if isinstance(env, str) and env:
            keys.append(ProviderKey(provider=name, api_key_env=env, required=False))
    return tuple(keys)


def classifier_key_envs() -> frozenset[str]:
    """The ``API_KEY_ENV`` declared by each installed drop-in.

    Lets a key store know which env names are classifier credentials without
    naming any vendor. Drop-ins that declare none contribute nothing.
    """
    return frozenset(key.api_key_env for key in list_classifier_keys())
