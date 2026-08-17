# SPEC-067 — Docker deploy parity with the split Arc home

**Status:** PENDING (placeholder — no PRD/SDD/PLAN yet)
**Raised:** 2026-08-16
**Blocked by:** the source-install layout fix on `fix/disposable-runtime-and-fleet-layout`

## Why this exists

The source-install deploy path is being fixed so the deployment finally matches
the lifecycle split that `arctrust.paths` has documented all along: the runtime
becomes disposable and versioned, the operator's fleet becomes durable.

**The Docker path was not touched.** Every future container install still ships
the old assumptions. This spec is the reminder to bring it to parity before
anyone builds a new image expecting the two paths to behave alike.

Do not start this until the layout fix has landed and been applied to the two
live boxes — the target layout is not settled until then.

## What changed on the source path

| Root | Holds | On update |
|------|-------|-----------|
| `~/.arc/runtime/<version>/` | framework code, venv, modules | replaced wholesale |
| `~/.arc/runtime/current` | **real symlink** — update is an atomic flip, rollback flips back | — |
| `~/.arc/config/` | `arcagent.toml`, `gateway.toml`, `arc.env` | preserved |
| `~/.arc/state/` | operator key, identity, trust store, arcstore, NATS, bundles | never touched |
| `~/arc/team/` | per-agent traces, sessions, memory, workspace | never touched |

`~/arc` holds source only. Nothing runs from it.

## Known defects in the container path

All three were found while deploying the Azure VM on 2026-08-16 and were
deliberately left alone, because each needs an in-container migration plus a
coordinated deploy rather than a code edit.

1. **`gateway.toml` is read from a path Arc never writes.**
   `deploy/entrypoint.sh` reads `$ARC_CONFIG_DIR/gateway.toml`, but `arc init`
   writes `$ARC_CONFIG_DIR/config/gateway.toml`. The overlay script then creates
   the file Arc never wrote, so there are two, and the running gateway reads the
   wrong one. Same resolver-split family as the bug this whole effort came from.

2. **The container never runs `arc install`, so it boots with zero modules.**
   The source path guards against this: `deploy/systemd/arc.service` has
   `ExecStartPre=… arc install`, precisely because modules are separately signed
   bundles that do not ship in the wheel. The entrypoint has no equivalent. The
   failure is silent in the worst way — agents boot, answer chat, and report
   healthy while their schedulers never fire and their tasks never dispatch.

3. **`ARC_TEAM_ROOT` means two different things.**
   `entrypoint.sh` treats it as the team directory itself. `arctrust.paths`
   treats it as the *parent* of `team/`. Nothing is broken today only because no
   compose file sets it. Both meanings must not survive; pick the `arctrust` one
   and make the entrypoint agree.

## Scope when this is picked up

- Bring `deploy/entrypoint.sh` and `deploy/cloud/docker-compose.yml` in line with
  the four roots above.
- Decide what `runtime/<version>/` + `current` mean inside an image, where the
  code already arrives as an immutable layer. The atomic-flip design may be
  redundant in a container, or may be the right way to hold modules — that is a
  genuine design question, not a mechanical port.
- Keep the durable roots (`config/`, `state/`, `team/`) on the mounted volume and
  the disposable runtime in the image layer.
- Add whatever test would have caught defect 2. A container that boots with no
  modules must fail loudly, the way the systemd unit already does.

## Files

- `deploy/entrypoint.sh`
- `deploy/cloud/docker-compose.yml`, `deploy/cloud/cloud-init.yaml`
- `scripts/deploy-azure.sh`, `scripts/publish-image.sh`
- `Dockerfile`
- Reference for intended behavior: `deploy/systemd/arc.service`, `scripts/deploy-node.sh`

## Context

- The Azure VM (`ssh brad`, `104.41.138.83`) was migrated OFF the container path
  to a systemd source install on 2026-08-16, so no live deployment currently
  depends on the container path. That is why this is not urgent — and also why
  it will go unnoticed until someone builds a fresh image.
- Related fix already merged to `develop` and `main`: `159331e5`, TOML
  array-of-tables emission, which unblocked `arc blueprint apply` on any agent
  that had ever approved a capability.
