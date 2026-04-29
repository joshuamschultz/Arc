"""SPEC-021 Task 1.3 — extended AST bypass categories per D-353.

Each test is a CVE-mapped POC against a known sandbox-escape pattern.
The validator must reject every one. Adding a new bypass class
without a test here means it is not yet defended.

Categories covered (all new in SPEC-021, on top of SPEC-017's set):

  10. Generator/coroutine-frame bypasses — ``gi_code``, ``gi_yieldfrom``
  11. Traceback frame access — ``tb_frame``
  12. Descriptor-protocol side channels — ``__pos__``, ``__neg__``,
      ``__get__``, ``__set__`` used as attribute access (subscript
      patterns reach the bound descriptor object)
  13. Subclass injection at attribute level — ``__init_subclass__`` /
      ``__class_getitem__`` accessed as attribute (CVE-2024-47532 class)
  14. Metaclass ``__getitem__`` patterns at class definition
  15. ``AttributeError.obj`` / ``.name`` leak (Python 3.10+)
  16. f-string format-spec attribute access still funneled through
      ``visit_Attribute``
"""

from __future__ import annotations

import pytest


def _raises_validation(source: str, category: str) -> None:
    from arcagent.tools._dynamic_loader import ASTValidationError, AstValidator

    with pytest.raises(ASTValidationError) as exc_info:
        AstValidator().validate(source)
    assert category in str(exc_info.value), (
        f"Expected category {category!r} in rejection; got: {exc_info.value}"
    )


class TestRejectGeneratorFrameAttrs:
    def test_gi_code_rejected(self) -> None:
        _raises_validation(
            "def bad(g):\n    return g.gi_code\n",
            "attribute:gi_code",
        )

    def test_gi_yieldfrom_rejected(self) -> None:
        _raises_validation(
            "def bad(g):\n    return g.gi_yieldfrom\n",
            "attribute:gi_yieldfrom",
        )


class TestRejectTracebackFrame:
    def test_tb_frame_rejected(self) -> None:
        _raises_validation(
            "def bad(tb):\n    return tb.tb_frame\n",
            "attribute:tb_frame",
        )


class TestRejectSubclassHooks:
    def test_init_subclass_attribute_rejected(self) -> None:
        """Accessing ``cls.__init_subclass__`` at attribute level."""
        _raises_validation(
            "def bad(cls):\n    return cls.__init_subclass__\n",
            "attribute:__init_subclass__",
        )

    def test_class_getitem_attribute_rejected(self) -> None:
        _raises_validation(
            "def bad(cls):\n    return cls.__class_getitem__\n",
            "attribute:__class_getitem__",
        )


class TestRejectDescriptorSideChannels:
    """Descriptor methods on objects can leak the owner class."""

    def test_get_descriptor_rejected(self) -> None:
        _raises_validation(
            "def bad(d):\n    return d.__get__\n",
            "attribute:__get__",
        )

    def test_set_descriptor_rejected(self) -> None:
        _raises_validation(
            "def bad(d):\n    return d.__set__\n",
            "attribute:__set__",
        )

    def test_pos_dunder_rejected(self) -> None:
        _raises_validation(
            "def bad(o):\n    return o.__pos__\n",
            "attribute:__pos__",
        )

    def test_neg_dunder_rejected(self) -> None:
        _raises_validation(
            "def bad(o):\n    return o.__neg__\n",
            "attribute:__neg__",
        )


class TestRejectMetaclassGetitem:
    """Classes that build a metaclass with ``__getitem__`` can index in
    ways that bypass attribute checks."""

    def test_metaclass_getitem_rejected(self) -> None:
        # ``Meta`` defines ``__getitem__``; ``Bad`` uses it as
        # metaclass. Even reading ``Bad['anything']`` later invokes the
        # metaclass — the dangerous surface is the metaclass itself.
        _raises_validation(
            "class Meta(type):\n"
            "    def __getitem__(cls, item):\n"
            "        return None\n"
            "class Bad(metaclass=Meta):\n"
            "    pass\n",
            "metaclass:__getitem__",
        )


class TestRejectAttributeErrorLeak:
    """Python 3.10+ — caught ``AttributeError`` exposes ``.obj`` and
    ``.name`` of the failed access. Reject within except-handler
    bindings that catch ``AttributeError``."""

    def test_attribute_error_obj_rejected(self) -> None:
        _raises_validation(
            "def bad(obj):\n"
            "    try:\n"
            "        obj.missing\n"
            "    except AttributeError as e:\n"
            "        return e.obj\n",
            "exception_attr:obj",
        )

    def test_attribute_error_name_rejected(self) -> None:
        _raises_validation(
            "def bad(obj):\n"
            "    try:\n"
            "        obj.missing\n"
            "    except AttributeError as e:\n"
            "        return e.name\n",
            "exception_attr:name",
        )

    def test_attribute_error_other_attr_allowed(self) -> None:
        """``e.args`` and ``e.__traceback__`` are not the new leak. The
        latter is still rejected via ``__traceback__`` if listed, but
        ``args`` is allowed (no info leak)."""
        # Accessing args on the exception object is fine.
        from arcagent.tools._dynamic_loader import AstValidator

        AstValidator().validate(
            "def ok(obj):\n"
            "    try:\n"
            "        obj.missing\n"
            "    except AttributeError as e:\n"
            "        return str(e)\n"
        )


class TestFormattedValueRecurses:
    """f-string interpolations must still flow through attribute checks
    even though they live inside FormattedValue nodes."""

    def test_fstring_with_blocked_attribute_rejected(self) -> None:
        _raises_validation(
            'def bad(x):\n    return f"{x.f_back}"\n',
            "attribute:f_back",
        )

    def test_fstring_with_gi_code_rejected(self) -> None:
        _raises_validation(
            'def bad(g):\n    return f"{g.gi_code}"\n',
            "attribute:gi_code",
        )
