"""Weighted associative graph — Hebbian write, decay/salience, spreading activation.

This is the FERNme core (SDD R-5/R-8). One table (``edges``) carries the whole
learned "situation-shape -> pattern" mapping:

* **hebbian_bump** strengthens a co-active pair with a *saturating* update
  ``w <- w + alpha*m*(1 - w/W)`` -- it never exceeds ``W`` no matter how often a
  pair co-fires, which bounds any single caller's ability to dominate the graph.
* **decay** forgets unreinforced edges ``w*e^(-lambda*dt)`` but *slows* the decay
  for salient edges ``lambda_eff = lambda*(1 - beta*s)`` -- a rare-but-significant
  one-shot survives while neutral noise fades below the forget floor.
* **spreading_activation** retrieves by flowing activation over the (undirected)
  weighted graph with an ACT-R fan effect ``S - ln(fan)`` -- a high-degree cue
  contributes *less* (built-in anti-poison), hop-capped and deterministic.

Every edge is scoped: activation never crosses a scope boundary (LLM08).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB


def _parse_ts(ts: str | None) -> datetime:
    """Parse a stored ISO timestamp, defaulting to epoch-safe now on absence."""
    if not ts:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _canonical(a: str, b: str) -> tuple[str, str]:
    """Order a node pair so an undirected edge has one canonical row."""
    return (a, b) if a <= b else (b, a)


class WeightedGraph:
    """Hebbian/decay/spreading dynamics over the per-agent ``edges`` table."""

    def __init__(self, db: MemoryDB, config: MemoryConfig | None = None) -> None:
        self._db = db
        self._cfg = config or MemoryConfig()

    # -- write -------------------------------------------------------------

    def hebbian_bump(
        self,
        scope: str,
        a: str,
        b: str,
        *,
        kind: str = "assoc",
        m: float = 1.0,
        salience: float = 0.0,
        directed: bool = False,
        ts: str | None = None,
    ) -> float:
        """Strengthen the ``a``-``b`` edge; return the new (saturating) weight.

        ``w <- w + alpha*m*(1 - w/W)``. Undirected edges (the default) are stored
        canonically so a co-active pair has exactly one row. ``ts`` fixes the
        ``last_hit`` (pass the event timestamp) so a rebuild replay is byte-identical.
        """
        src, dst = (a, b) if directed else _canonical(a, b)
        conn = self._db.connect()
        row = conn.execute(
            "SELECT weight, salience, hits FROM edges "
            "WHERE scope=? AND src=? AND dst=? AND kind=?",
            (scope, src, dst, kind),
        ).fetchone()

        cur_w = float(row[0]) if row else 0.0
        cur_s = float(row[1]) if row else 0.0
        cur_hits = int(row[2]) if row else 0

        new_w = cur_w + self._cfg.alpha * m * (1.0 - cur_w / self._cfg.saturation)
        new_w = min(new_w, self._cfg.saturation)
        new_s = max(cur_s, salience)
        now = ts if ts is not None else datetime.now(UTC).isoformat()

        conn.execute(
            "INSERT INTO edges (scope, src, dst, kind, weight, salience, last_hit, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, src, dst, kind) DO UPDATE SET "
            "weight=excluded.weight, salience=excluded.salience, "
            "last_hit=excluded.last_hit, hits=edges.hits+1",
            (scope, src, dst, kind, new_w, new_s, now, cur_hits + 1),
        )
        conn.commit()
        return new_w

    def link(
        self,
        scope: str,
        src: str,
        dst: str,
        *,
        kind: str,
        weight: float = 1.0,
        ts: str | None = None,
    ) -> None:
        """Create/refresh a directed edge (wiki-link, insight->cue, insight->instance)."""
        now = ts if ts is not None else datetime.now(UTC).isoformat()
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO edges (scope, src, dst, kind, weight, salience, last_hit, hits) "
            "VALUES (?, ?, ?, ?, ?, 0.0, ?, 1) "
            "ON CONFLICT(scope, src, dst, kind) DO UPDATE SET "
            "weight=excluded.weight, last_hit=excluded.last_hit, hits=edges.hits+1",
            (scope, src, dst, kind, weight, now),
        )
        conn.commit()

    # -- read --------------------------------------------------------------

    def weight(self, scope: str, a: str, b: str, *, kind: str = "assoc") -> float:
        """Current stored weight of an edge (0.0 if absent). Undirected by default."""
        src, dst = _canonical(a, b)
        conn = self._db.connect()
        row = conn.execute(
            "SELECT weight FROM edges WHERE scope=? AND src=? AND dst=? AND kind=?",
            (scope, src, dst, kind),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def neighbors(self, scope: str, node: str) -> list[tuple[str, float]]:
        """Undirected neighbors of ``node`` with edge weights."""
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT dst, weight FROM edges WHERE scope=? AND src=? "
            "UNION ALL SELECT src, weight FROM edges WHERE scope=? AND dst=?",
            (scope, node, scope, node),
        ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def neighbor_edges(self, scope: str, node: str) -> list[tuple[str, str, float]]:
        """Undirected neighbors of ``node`` as ``(neighbor, kind, weight)`` triples.

        Carries the edge ``kind`` (``assoc`` co-occurrence vs ``link`` wiki-edge) so a
        caller can render *why* two nodes are linked, not just that they are.
        """
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT dst, kind, weight FROM edges WHERE scope=? AND src=? "
            "UNION ALL SELECT src, kind, weight FROM edges WHERE scope=? AND dst=?",
            (scope, node, scope, node),
        ).fetchall()
        return [(r[0], r[1], float(r[2])) for r in rows]

    def rename_node(self, scope: str, old: str, new: str) -> int:
        """Repoint every edge touching ``old`` onto ``new`` (cue-merge, T-054).

        Weights/hits of any edge that collides with an existing ``new`` edge are
        summed; self-loops created by the merge are dropped. Returns the number of
        edges repointed. This is how a merged cue's instance links follow it.
        """
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT src, dst, kind, weight, salience, last_hit, hits FROM edges "
            "WHERE scope=? AND (src=? OR dst=?)",
            (scope, old, old),
        ).fetchall()
        conn.execute("DELETE FROM edges WHERE scope=? AND (src=? OR dst=?)", (scope, old, old))
        for src, dst, kind, weight, salience, last_hit, hits in rows:
            new_src = new if src == old else src
            new_dst = new if dst == old else dst
            if new_src == new_dst:
                continue  # a merge that would create a self-loop is dropped
            conn.execute(
                "INSERT INTO edges (scope, src, dst, kind, weight, salience, last_hit, hits) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, src, dst, kind) DO UPDATE SET "
                "weight=edges.weight+excluded.weight, hits=edges.hits+excluded.hits",
                (scope, new_src, new_dst, kind, weight, salience, last_hit, hits),
            )
        conn.commit()
        return len(rows)

    # -- decay -------------------------------------------------------------

    def decay(self, scope: str, *, now: datetime | None = None, lam: float | None = None) -> int:
        """Decay every edge in ``scope``; forget those below the floor.

        ``lambda_eff = lam*(1 - beta*salience)`` so salient edges decay slower.
        Returns the number of edges forgotten (dropped below ``forget_floor``).
        """
        now = now or datetime.now(UTC)
        lam = self._cfg.lambda_fast if lam is None else lam
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT src, dst, kind, weight, salience, last_hit FROM edges WHERE scope=?",
            (scope,),
        ).fetchall()

        forgotten = 0
        for src, dst, kind, weight, salience, last_hit in rows:
            elapsed_days = (now - _parse_ts(last_hit)).total_seconds() / 86400.0
            lam_eff = lam * (1.0 - self._cfg.beta * float(salience))
            lam_eff = max(lam_eff, 0.0)
            new_w = float(weight) * math.exp(-lam_eff * elapsed_days)
            if new_w < self._cfg.forget_floor:
                conn.execute(
                    "DELETE FROM edges WHERE scope=? AND src=? AND dst=? AND kind=?",
                    (scope, src, dst, kind),
                )
                forgotten += 1
            else:
                conn.execute(
                    "UPDATE edges SET weight=?, last_hit=? "
                    "WHERE scope=? AND src=? AND dst=? AND kind=?",
                    (new_w, now.isoformat(), scope, src, dst, kind),
                )
        conn.commit()
        return forgotten

    # -- spreading activation ----------------------------------------------

    def spreading_activation(
        self,
        scope: str,
        sources: dict[str, float],
        *,
        max_hops: int | None = None,
    ) -> dict[str, float]:
        """Flow activation from ``sources`` over the weighted graph (ACT-R fan effect).

        Returns activation for every reached node (sources included). A node's
        contribution to a neighbor is ``act * weight * max(0, S - ln(fan))``,
        hop-capped so the walk is bounded regardless of graph size.
        """
        hops = self._cfg.max_hops if max_hops is None else max_hops
        activation: dict[str, float] = dict(sources)
        frontier: dict[str, float] = dict(sources)

        for _ in range(hops):
            contributions: dict[str, float] = {}
            for node, act in frontier.items():
                neigh = self.neighbors(scope, node)
                fan = len(neigh)
                if fan == 0:
                    continue
                spread = max(0.0, self._cfg.fan_strength - math.log(fan))
                for other, w in neigh:
                    delta = act * w * spread
                    if delta <= 0.0:
                        continue
                    contributions[other] = contributions.get(other, 0.0) + delta
            if not contributions:
                break
            frontier = {}
            for node, delta in contributions.items():
                activation[node] = activation.get(node, 0.0) + delta
                frontier[node] = delta
        return activation


__all__ = ["WeightedGraph"]
