# AST validator — blocked patterns

The AST validator runs on every `.py` file under `workspace/.capabilities/` (untrusted scan root). If your source matches any pattern below, `create_tool` returns `Error: AST validation rejected source — <category>`.

## Blocked imports

`ctypes`, `subprocess`, `socket`, `os`, `sys`, `pickle`, `marshal`, `shelve`. Submodules of these (e.g. `os.path`) are also blocked.

If you need filesystem access: use the existing `read`, `write`, `edit`, `find`, `ls`, or `grep` tools by composing them — don't re-implement them in your tool.

## Blocked attribute accesses

Frame and class internals — these are sandbox escapes.

- `gi_frame`, `gi_code`, `gi_yieldfrom` (generator / coroutine frames)
- `tb_frame` (traceback frames)
- `f_back`, `f_builtins`, `f_globals`, `f_locals` (frame chain)
- `__class__`, `__bases__`, `__subclasses__`, `__mro__`, `__dict__` (class graph)
- `__init_subclass__`, `__class_getitem__` (subclass injection)
- `__reduce__`, `__reduce_ex__` (pickle escape)
- `__get__`, `__set__`, `__pos__`, `__neg__` (descriptor side-channels)
- `modules` on any object (e.g., `m.modules['os']`)

## Blocked calls

`compile`, `eval`, `exec`, `__import__`. Use them and the validator rejects.

## Blocked assignments

`__builtins__`, `__loader__`, `__spec__`. You cannot rebind these.

## Class definitions

- A class declaring `__init_subclass__` is rejected.
- A class using a metaclass that defines `__getitem__` is rejected.

## Exception handling

`except AttributeError as e:` and accessing `e.obj` or `e.name` is rejected (Python 3.10+ leak).

## Encoding

Source files must be UTF-8. A non-UTF-8 `coding:` declaration in the first two lines is rejected (codec attack).

## Restricted runtime

Even if the AST validator passes, the runtime exec namespace has a scrubbed `__builtins__`. Only the safe core (`print`, `len`, `range`, `str`, `int`, basic types and iterators) is available. Imports outside `arcagent.tools._decorator`, `typing`, `dataclasses`, `collections.abc` raise.

## What this means for you

Treat your tool as a tiny, pure function that uses the existing tool surface (read/write/edit/etc.) for any side effect. If your tool needs something the existing surface doesn't expose, that's a sign you're authoring at the wrong layer — request a runtime extension, not a workspace tool.
