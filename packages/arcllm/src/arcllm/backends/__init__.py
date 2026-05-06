"""ArcLLM vault backend implementations.

Concrete classes implementing arcllm.vault.VaultBackend. Each backend is
deliberately small — credential discovery, network handling, and retries
are deferred to the underlying SDK and to VaultResolver's TTL cache.
"""
