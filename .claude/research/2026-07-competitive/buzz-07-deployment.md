# Buzz vs Arc — Deployment, Operations, Scale

## (a) Time-to-first-value

**Buzz — dev path** (README.md:136-153): `git clone` → `. ./bin/activate-hermit` → `just setup && just build` → `just dev`. 4 commands, but `just setup` pulls Rust 1.88+/Node 24+/pnpm 10+ via Hermit, boots Postgres+Redis+MinIO via the root `docker-compose.yml`, and runs migrations. Non-trivial first build (Rust workspace, `Cargo.lock` 255KB, 24 crates). No prebuilt relay image required for dev, but the dev stack ≠ prod stack.

**Buzz — prod path** (`deploy/compose/README.md:8-13,44-56`): `cd deploy/compose && cp .env.example .env` → edit secrets → `./run.sh start`. 4 commands + manual secret editing, requires Docker Compose ≥2.24.4, ships Postgres+Redis+MinIO+optional Caddy in the bundle. `BUZZ_AUTO_MIGRATE` is opt-in — cold DB needs `buzz-admin migrate` run explicitly. This is a real multi-service stack, not a single container.

**Arc**: `Dockerfile:1-14` — one image, `docker compose up -d` after `cp .env.example .env` + API key (per `docker-compose.yml:2-4`). 2 commands, zero external services (no Postgres/Redis/object storage — SQLite + embedded NATS binary baked into the image, `Dockerfile:75-86`). Model weights baked in for air-gap (`Dockerfile:44-50`). This is a materially lower floor for a single agent/workspace than either Buzz path — Arc's thesis that its single-image install is faster for the *individual/dev* case holds.

**Verdict on (a):** Arc wins solo-instance time-to-value by a wide margin. Buzz wins nothing here — it's honest that the compose bundle is "single-node/VPS," not one-container.

## (b) Deployment target matrix

| Target | Buzz | Arc |
|---|---|---|
| Local dev | `just dev` (native, Hermit-pinned toolchain) | `docker compose up` |
| Single VPS/node | `deploy/compose/` (Postgres+Redis+MinIO+Caddy, TLS via Let's Encrypt override) | `docker-compose.yml` (one container, one volume) |
| Kubernetes | `deploy/charts/buzz/` — full Helm chart: HPA (`templates/hpa.yaml`), PDB, ServiceMonitor (Prometheus Operator), NetworkPolicy, HTTPRoute (Gateway API), ArgoCD example (`examples/argocd-app.yaml`), Flux HelmRelease example (`examples/flux-helmrelease.yaml`), cert-manager example, secret templates, `values.schema.json` validation, and a **second** chart `deploy/charts/buzz-push-gateway/` with its own migration Job + NetworkPolicy | None. `packages/arcgateway` has no k8s manifests. |
| Multi-instance / HA | `replicaCount` param, `autoscaling.minReplicas: 5 / maxReplicas: 15` (`values.yaml:37,45-46`), CPU + custom websocket-connection-count HPA metric | Explicitly unsupported: `executor_nats.py:32-36` raises `NotImplementedError("multi-instance NATS-based scaling is deferred... no ETA")`; `pairing_postgres.py:11-25` is an unimplemented stub gating multi-instance pairing state |
| Backing store | Postgres (events+FTS), Redis (pubsub/presence), S3/MinIO (media) — all real, all with migration tooling (`migrations/`) | SQLite only (`arcstore/backends/sqlite.py`); `base.py:60-64` comments "future network backends (Postgres/cloud)" — aspirational, not built |
| Federation/mesh | `crates/buzz-relay-mesh/` — gossip.rs, membership.rs, registry.rs, wire.rs: a real relay-to-relay mesh/membership protocol | None |

**Verdict on (b):** Buzz has a full production deployment ladder (dev → VPS → HA k8s with GitOps examples). Arc has exactly one rung: single container, single node, single tenant. This gap is real and unaddressed by the quick-deploy branch.

## (c) Multi-tenancy & scale — real numbers

Buzz's `docs/multi-tenant-relay.md` is a **formally verified** (TLA+ model-checked exhaustively — 472M states generated, 16.2M distinct, depth 13; Tamarin: 32 lemmas green in ~12s) row-level-security multi-tenancy spec: N stateless relay processes share one Postgres, M communities are isolated by `community_id`, onboarding a new community is **an INSERT, never DDL** (multi-tenant-relay.md:74). This is the literal "new workspace = a DB write" claim, proven, not just asserted.

`perf/RELAY_BUS_SCALING.md:20-32` gives measured numbers: at 64 communities × 100 events/s, the community-scoped Redis pub/sub delivers a **64.0× reduction** in irrelevant per-pod ingress vs. a naive global bus (6,400/s → 100/s per pod), holding flat across 1/2/4 pods — i.e., the fan-out cost doesn't scale with tenant count, it scales with subscribed interest.

Arc has no multi-tenancy story at all — one deployment = one operator's fleet, on one SQLite file, on one box.

## (d) Verdict on "nobody has fleet tooling"

**Partially false as stated, but the roadmap's real target survives on a narrower claim.** Buzz has genuinely first-party, production-grade fleet tooling for **hosting many workspaces/communities** (Helm HPA, formally-proven multi-tenant isolation, GitOps examples) — that half of "nobody has this" is wrong; Buzz has it and Arc doesn't.

But Buzz has **no equivalent for orchestrating many *agent processes***. `buzz-agent` caps at 8 concurrent sessions per process (`VISION_AGENT.md:41,65`); "ten agents in parallel behind Buzz" (`VISION_AGENT.md:66`) means an operator manually runs ten processes — there is no `buzz fleet up --agents 20`, no k8s Deployment for `buzz-agent` workers in the Helm chart (the chart deploys `buzz-relay`, not agent workers), and the only N-agent *provisioner* found (`benchmarks/harbor-buzz-orchestra/`) is a benchmark-only Harbor adapter, not a shipped ops tool. So the narrower claim — "nobody has first-party tooling to spin up/manage a fleet of *agent* processes" — still stands, and it's the claim that matters for Arc's `arc fleet up` bet. Rewrite the roadmap language to say "nobody has fleet-of-*agents* tooling," not "nobody has fleet tooling," because Buzz's workspace/tenant fleet tooling is real and better than Arc's.

## (e) What Arc must build

1. A Postgres (or equivalent) backend for `arcstore` — the sqlite-only backend is a hard ceiling on multi-instance/HA before anything else matters.
2. Implement or delete `executor_nats.py` / `pairing_postgres.py` — two stubs currently block any horizontal scaling story; right now `arc fleet up --agents 20` is not possible even in principle behind a load balancer.
3. A Helm chart / k8s manifests, at minimum matching Buzz's shape: HPA, PDB, ServiceMonitor, NetworkPolicy — Arc has zero k8s presence today.
4. An actual agent-fleet controller: process supervision, scaling, and placement for N `arcagent` instances, since this is the one place Buzz is also weak — first-mover space is real here, not imaginary.
5. A migration/upgrade tool matching Buzz's `migrations/` + `buzz-admin migrate` pattern; Arc's single-container model has no documented upgrade path beyond "pull new image."

## (f) Where Arc's architecture is genuinely better for federal/air-gapped

- Air-gap-complete image: model weights baked in (`Dockerfile:44-50`), `HF_HUB_OFFLINE=1`, no runtime pulls. Buzz's compose bundle needs Postgres/Redis/MinIO provisioned and reachable, and its NIP-98 replay-protection has a documented per-pod-scope gap requiring a shared seen-set (Redis) across replicas for HA correctness (`multi-tenant-relay.md:676-696`) — i.e., Buzz's HA path has an open correctness caveat that must be manually closed by the operator.
- Minimal external attack surface / dependency count: no Postgres, no Redis, no S3/MinIO, no NATS cluster to secure — smaller blast radius per the Four Pillars model, versus Buzz's multi-service stack with three stateful backing services.
- Single-tenant-by-construction: Arc's per-agent DID/identity model (CLAUDE.md's Four Pillars) doesn't need a formally-proven tenant-isolation layer because there's only ever one tenant per deployment — Buzz needed 1,100+ lines of TLA+/Tamarin proof specifically because it *chose* shared infrastructure; Arc's federal posture avoids that entire class of risk by not sharing infrastructure across trust boundaries at all, which is arguably the more defensible federal default (no shared Postgres row-level-security trust boundary to audit).
- Signed/verified supply chain at the binary level baked into the image build (NATS server SHA256SUMS check, `Dockerfile:78-84`) — Buzz's compose bundle pins `ghcr.io/block/buzz:main` for "early testing" and explicitly tells operators to pin to a SHA/semver themselves for production (`deploy/compose/README.md:30`).
