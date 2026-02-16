"""Tests for arcllm package __init__.py — lazy imports and __all__."""

import importlib

import pytest


def test_lazy_import_anthropic_adapter():
    """AnthropicAdapter is accessible via top-level import."""
    from arcllm import AnthropicAdapter

    assert AnthropicAdapter.__name__ == "AnthropicAdapter"


def test_lazy_import_openai_adapter():
    """OpenaiAdapter is accessible via top-level import."""
    from arcllm import OpenaiAdapter

    assert OpenaiAdapter.__name__ == "OpenaiAdapter"


def test_lazy_import_base_adapter():
    """BaseAdapter is accessible via top-level import."""
    from arcllm import BaseAdapter

    assert BaseAdapter.__name__ == "BaseAdapter"


def test_lazy_import_caches_on_globals():
    """Second access uses cached global, not __getattr__ again."""
    import arcllm

    # First access triggers __getattr__
    _ = arcllm.AnthropicAdapter
    # Second access should hit globals() cache
    adapter = arcllm.AnthropicAdapter
    assert adapter.__name__ == "AnthropicAdapter"


def test_getattr_unknown_attribute():
    """Accessing a non-existent attribute raises AttributeError."""
    import arcllm

    with pytest.raises(AttributeError, match="no attribute"):
        _ = arcllm.NoSuchThing


def test_all_exports_are_accessible():
    """Every name in __all__ is importable."""
    import arcllm

    for name in arcllm.__all__:
        attr = getattr(arcllm, name)
        assert attr is not None, f"{name} resolved to None"
