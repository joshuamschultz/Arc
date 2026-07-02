"""Ollama adapter — OpenAI-compatible local inference server."""

from arcllm.adapters.openai import OpenaiAdapter


class OllamaAdapter(OpenaiAdapter):
    """Thin alias for Ollama's OpenAI-compatible API.

    Model residency (warm KV cache across turns) is NOT set here. Ollama
    ignores ``keep_alive`` on the ``/v1/chat/completions`` compat path this
    adapter uses (ollama/ollama#11458), and the shared OpenAI request body
    has no passthrough for it. Warm the model at the server instead:

        OLLAMA_KEEP_ALIVE=30m   # finite TTL on shared hosts
        OLLAMA_KEEP_ALIVE=-1    # pin forever — dedicated single-model hosts only
                                # (-1 disables idle eviction → OOM risk on shared boxes)

    Prefix/KV reuse is automatic while the model stays resident. Ollama
    returns no cached-token count, so cache reuse is unobservable from the
    response (infer from latency / ``ollama ps``). A native ``/api/chat``
    override for per-request ``keep_alive`` is a future option (SPEC-029 D-395),
    not needed today.
    """

    @property
    def name(self) -> str:
        return "ollama"
