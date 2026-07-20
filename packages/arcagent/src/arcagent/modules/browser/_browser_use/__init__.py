"""browser-use integration (optional ``browser-use`` package).

Everything that imports the third-party ``browser_use`` package lives
here, under an underscore-prefixed subpackage the capability loader never
scans. The public ``browser_task`` tool (``modules/browser/agentic.py``)
imports this lazily at call time, so a missing dependency degrades loudly
at invocation instead of breaking module load.

``browser-use`` is installed into the deployment venv separately (not a
workspace extra — its aiohttp pin conflicts with the mattermost/slack CVE
floor): ``pip install browser-use && browser-use install``.
"""
