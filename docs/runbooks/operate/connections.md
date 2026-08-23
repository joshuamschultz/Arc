# Connections — setup and durable activation

> **Runbooks** · Operate · **For** operators connecting agents to external systems

Connections are deployment-wide credentials with deny-by-default, per-agent
grants. ArcUI and ArcCLI call the same typed connector service using the
extension's canonical coordinate; display names are presentation only.

## Configure

```bash
arc connector list --agent <agent>
arc connector add <agent> <connector>
arc connector auth <agent> <connector>
arc connector probe <agent> <connector>
```

`add` validates that the target agent exists before prompting or writing.
`auth` passes credentials through the connector's secret boundary; secrets must
remain vault-backed and must not be written to manifests, logs or agent
workspaces. `probe` exercises the real native, CLI or MCP attachment.

## Grant and activation

A mutation first updates the durable grant snapshot and records a per-agent
reconciliation command. If the owning agent is in this process, Arc applies the
change immediately and acknowledges it. Otherwise the surface reports
`activation_pending`; the agent reconciles the snapshot at startup and on a
bounded periodic cycle. The queue only accelerates wakeup, so a crash between
the grant write and queue write cannot lose the change. Revocation/removal uses
the same path and withdraws owned tools.

## Troubleshooting

- **Unknown agent:** use an agent from the deployment roster; CLI and UI reject
  inert grants identically.
- **`activation_pending`:** verify the target agent is running and can reach
  ArcStore. It will converge without editing files or restarting solely to load
  the grant.
- **Probe failure:** verify the declared attachment kind and its external
  executable/MCP command, timeout and vault secret—not the display name.
- **ArcStore unavailable:** connector startup remains usable from its grant
  snapshot where possible and emits a degraded audit event; restore the store
  before relying on cross-process convergence.

Never edit a grant, manifest, installed skill/tool or generated connector file
to force activation. Every artifact is reverified and every mutation must pass
identity, authorization and audit boundaries.
