"""SQLite connector package.

The import is RELATIVE on purpose. An installed bundle is copied to
``<extensions>/sqlite/arc_ext_sqlite/`` with only that directory's parent on
``sys.path``, so there is no ``extensions`` package to import from — reaching for
the repository path worked in this checkout and failed on every real deployment
with "No module named 'extensions'".
"""

from .source import SQLiteAttachment, build_native_attachment

__all__ = ["SQLiteAttachment", "build_native_attachment"]
