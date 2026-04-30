"""SPEC-022 Acceptance Criterion 20 — cold start delta < 100ms vs main.

Background: Phase 1-7 add filesystem watchers (lazy, ref-counted) and many
new HTTP routes, but the watcher tasks must not start until at least one
client subscribes. ``create_app`` should remain cheap.

Strategy:
  - Time a synthetic ``create_app`` cold start a handful of times after
    one warmup pass.
  - Assert the median is under a generous absolute threshold (300ms on
    CI hardware). This is *not* a strict A/B against main — git-aware
    A/B comparison would require a checkout in the test, which is
    beyond unit-test scope. Instead we encode the contract "cold start
    is fast enough that nothing measurable broke."

If the median exceeds the budget, the failure message names the most
common cause (eager watcher, eager fs scan, missing lazy import).
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from arcui.auth import AuthConfig
from arcui.server import create_app

_ITERATIONS = 5
_BUDGET_MS = 300.0
_HINT = (
    "cold start exceeded budget — common causes: eager fs_watcher start, "
    "eager team scan, top-level imports of heavy libs (watchfiles already "
    "guarded behind lazy import)."
)


def _make_team(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    (root / "alpha_agent").mkdir()
    (root / "alpha_agent" / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n', encoding="utf-8"
    )
    (root / "alpha_agent" / "workspace").mkdir()
    return root


class TestColdStartDelta:
    def test_create_app_cold_start_is_fast(self, tmp_path: Path) -> None:
        team_root = _make_team(tmp_path)
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})

        # Warmup so we measure steady-state cold start, not import overhead.
        create_app(auth_config=auth, team_root=team_root)

        timings: list[float] = []
        for _ in range(_ITERATIONS):
            t0 = time.perf_counter()
            create_app(auth_config=auth, team_root=team_root)
            timings.append((time.perf_counter() - t0) * 1000.0)

        median_ms = statistics.median(timings)
        assert median_ms < _BUDGET_MS, (
            f"create_app median={median_ms:.1f}ms (budget={_BUDGET_MS}ms). "
            f"all={[f'{x:.1f}' for x in timings]}. {_HINT}"
        )
