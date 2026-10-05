"""The ANN sidecar and the local embedder can share one process.

Production crash (Azure, x86_64 Linux): ``arc ui start`` segfaulted in a loop.
usearch's ``__init__`` loads NumKong's extension with ``RTLD_GLOBAL``, which puts
NumKong's bundled ``libgomp`` into the process-wide symbol scope. When torch
loaded afterwards (the embed worker importing sentence-transformers), torch's
own ``libgomp`` and ``libtorch_cpu`` bound their OpenMP symbols to NumKong's
copy: two half-wired OpenMP runtimes, SIGSEGV inside torch's import.

The fix is one load-order rule in ``arcmemory.index.ann``: torch (when it is
installed) loads before usearch. These tests run in a fresh interpreter because
load order is per-process and the crash kills the interpreter. The crash itself
only reproduces on ELF/glibc (x86_64 Linux CI); the order check runs everywhere.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from arcmemory.db import sqlite_vec_loadable

_REPO_ROOT = Path(__file__).resolve().parents[4]

pytestmark = [
    pytest.mark.skipif(not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"),
    pytest.mark.skipif(
        importlib.util.find_spec("torch") is None, reason="torch absent: no second OpenMP runtime"
    ),
]

# Builds a real sidecar through the backend a user's search goes through, so
# usearch loads the way it does in `arc ui`, then runs the rest of the script.
_BUILD_SIDECAR = textwrap.dedent(
    """
    import asyncio, sys, tempfile
    from pathlib import Path
    from packages.arcmemory.tests.conftest import bulk_load_vectors, low_rank_vectors
    from arcmemory.db import MemoryDB
    from arcmemory.index import ann
    from arcmemory.index.backend import open_index_backend

    SCOPE = "did:arc:agent-a:doc:pool"

    async def build():
        db = MemoryDB(Path(tempfile.mkdtemp()) / "ws", dims=16)
        db.connect()
        vectors = low_rank_vectors(200, 16, seed=1)
        bulk_load_vectors(db, SCOPE, vectors)
        await open_index_backend("sqlite", db=db).vec_search(SCOPE, vectors[0].tolist(), top_k=5)
        assert await ann.scope_ann(db, SCOPE).wait_built(timeout=60.0), "sidecar never built"
        assert "usearch.compiled" in sys.modules

    asyncio.run(build())
    """
)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))
    return subprocess.run(
        [sys.executable, "-c", _BUILD_SIDECAR + textwrap.dedent(script)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def test_torch_runtime_is_loaded_before_usearch() -> None:
    result = _run(
        """
        loaded = list(sys.modules)
        assert "torch" in sys.modules, "usearch loaded without torch's runtime first"
        assert loaded.index("torch") < loaded.index("usearch.compiled"), loaded
        print("ORDER-OK")
        """
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert "ORDER-OK" in result.stdout


@pytest.mark.skipif(
    importlib.util.find_spec("sentence_transformers") is None,
    reason="arcllm[local] extra absent: the embedder never loads torch",
)
def test_local_embedder_loads_on_its_thread_after_the_sidecar() -> None:
    result = _run(
        """
        from arcllm import DEFAULT_EMBED_MODEL, LocalEmbedder
        from arcllm.exceptions import ArcLLMEmbeddingUnavailableError

        async def embed():
            # The shared embed worker thread imports sentence-transformers ->
            # torch, the exact frame that segfaulted. A model missing from the
            # offline cache is a clean 'unavailable', not a crash.
            try:
                await LocalEmbedder(DEFAULT_EMBED_MODEL).embed(["hello"])
            except ArcLLMEmbeddingUnavailableError:
                pass

        asyncio.run(embed())
        print("EMBED-OK")
        """
    )
    assert result.returncode == 0, f"exit {result.returncode}: {result.stderr[-4000:]}"
    assert "EMBED-OK" in result.stdout
