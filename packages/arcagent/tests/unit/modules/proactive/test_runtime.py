"""Proactive module runtime configuration — leader-backend selection (R-048)."""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.modules.proactive import _runtime
from arcagent.modules.proactive.leader import NoOpLeaderElection
from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection
from arcagent.modules.proactive.leader_redis import RedisLockElection


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class TestLeaderBackendSelection:
    """``config['leader']`` selects the real backend — not always NoOp."""

    def test_default_is_noop(self, tmp_path: Any) -> None:
        _runtime.configure(config={}, workspace=tmp_path, agent_name="a")
        assert isinstance(_runtime.state().leader, NoOpLeaderElection)

    def test_redis_backend_selected_from_config(self, tmp_path: Any) -> None:
        pytest.importorskip("redis")  # optional infra dependency
        _runtime.configure(
            config={"leader": "redis", "redis_url": "redis://localhost:6379/0"},
            workspace=tmp_path,
            agent_name="a",
        )
        assert isinstance(_runtime.state().leader, RedisLockElection)

    def test_k8s_backend_selected_from_config(self, tmp_path: Any) -> None:
        _runtime.configure(
            config={
                "leader": "k8s",
                "k8s_namespace": "arc-prod",
                "k8s_lease_name": "arcagent-proactive",
            },
            workspace=tmp_path,
            agent_name="a",
        )
        assert isinstance(_runtime.state().leader, KubernetesLeaseElection)

    def test_redis_without_url_fails_loud(self, tmp_path: Any) -> None:
        # A misconfigured multi-instance deployment must not silently degrade
        # to NoOp (every replica self-elects and ticks — violates R-048).
        with pytest.raises(ValueError, match="redis_url"):
            _runtime.configure(
                config={"leader": "redis"}, workspace=tmp_path, agent_name="a"
            )

    def test_k8s_without_lease_params_fails_loud(self, tmp_path: Any) -> None:
        with pytest.raises(ValueError, match="k8s_namespace"):
            _runtime.configure(
                config={"leader": "k8s"}, workspace=tmp_path, agent_name="a"
            )

    def test_unknown_backend_fails_loud(self, tmp_path: Any) -> None:
        with pytest.raises(ValueError, match="unknown proactive leader backend"):
            _runtime.configure(
                config={"leader": "consul"}, workspace=tmp_path, agent_name="a"
            )
