"""arcui — Arc LLM telemetry dashboard.

One-liner usage::

    from arcui import serve
    serve(llm=model, trace_store=store)
"""

from arcui.server import attach_llm, create_app, serve

__version__ = "0.1.0"
__all__ = ["__version__", "attach_llm", "create_app", "serve"]
