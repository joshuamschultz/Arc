"""Storage backend implementations for arcteam.

The production substrate is :class:`arcteam.backends.nats.NatsBackend`
(NATS JetStream). ``MemoryBackend`` (in :mod:`arcteam.storage`) remains the
test backend.
"""

from arcteam.backends.nats import NatsBackend

__all__ = ["NatsBackend"]
