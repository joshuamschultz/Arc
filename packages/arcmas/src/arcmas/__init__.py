"""arcmas — one install for the Arc core runtime stack.

Install with: pip install arcmas

This meta-package declares two direct dependencies — ``arccmd`` (the ``arc``
CLI, import ``arccli``) and ``arcmemory`` (the scaffold-default Brain). The rest
of the core stack arrives transitively through the CLI's dependency graph:
arctrust, arcllm, arcstore, arcprompt, arcrun, arc-agent (import ``arcagent``),
arcbundle, and arcteam.

The messaging gateway (``arcgateway``), web dashboard (``arcui``), and skill
verification (``arcskill``) surfaces are separate installs — arcmas does not
pull them in.

No product runtime API lives here; this package only pins the install surface.
"""

__version__ = "0.4.0"
