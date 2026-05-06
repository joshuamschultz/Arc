#!/usr/bin/env bash
# Create the scap_isso demo agent on a fresh laptop.
#
# Idempotent: if the agent already exists, only re-applies the identity
# and ui_reporter config (the parts that matter for the demo).
#
# Run from repo root:
#   ./scripts/bootstrap-scap-agent.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_ROOT/demo-extensions/agent-template"
AGENT_DIR="$REPO_ROOT/team/scap_isso"
ARC_BIN="${ARC_BIN:-$REPO_ROOT/.venv/bin/arc}"

if [[ ! -x "$ARC_BIN" ]]; then
  echo "✗ arc binary not found at $ARC_BIN — set ARC_BIN or run uv sync first."
  exit 1
fi

if [[ ! -d "$AGENT_DIR" ]]; then
  echo "→ Creating agent scap_isso..."
  "$ARC_BIN" agent create scap_isso \
    --dir "$REPO_ROOT/team" \
    --model anthropic/claude-sonnet-4-5-20250929
else
  echo "→ Agent scap_isso already exists at $AGENT_DIR"
fi

echo "→ Installing identity.md (Cora ISSO assistant persona)"
cp "$TEMPLATE/identity.md" "$AGENT_DIR/workspace/identity.md"

# Append ui_reporter block if not already present
if ! grep -q "^\[modules.ui_reporter\]" "$AGENT_DIR/arcagent.toml"; then
  echo "→ Appending [modules.ui_reporter] to arcagent.toml"
  cat "$TEMPLATE/ui-reporter-snippet.toml" >> "$AGENT_DIR/arcagent.toml"
else
  echo "→ [modules.ui_reporter] already configured"
fi

# Validate
"$ARC_BIN" agent build "$AGENT_DIR" --check >/dev/null 2>&1 \
  && echo "✓ Agent config validated" \
  || { echo "✗ arc agent build --check failed:"; "$ARC_BIN" agent build "$AGENT_DIR" --check; exit 1; }

echo
echo "Done. Next steps:"
echo "  ./scripts/install-scap-extension.sh   # install ~/.arc/capabilities/"
echo "  ./scripts/arc-stack.sh start scap_isso # start UI + agent"
