# Shared agent-manifest reader for deploy scripts.
# SPEC-025 §TD-4 — extracted from deploy/aws/setup-vm.sh so deploy/azure
# (and any future cloud target) can source the same implementation
# without copy-paste.
#
# Usage:
#   source deploy/lib/agent-manifest.sh
#   ENABLED_AGENTS=$(_read_agent_manifest "${REPO_ROOT}/deploy/aws/agents.enabled")
#
# The manifest file is one agent dir name per line. Blank lines and
# `# comments` are ignored. Every entry must match `^[a-z0-9_]+_agent$`
# (SPEC-025 §M-3 — defends against path-traversal and shell-metachar
# smuggling into the rm -rf loop downstream).
#
# If the file is missing, fall back to the three NLIT demo agents and
# emit a [WARN] line. If the file is empty (after stripping comments),
# abort with exit 1 — that's a misconfiguration, not "disable everything".

_read_agent_manifest() {
  local manifest="${1:?path to agents.enabled required}"
  if [ ! -f "${manifest}" ]; then
    echo "[WARN] ${manifest} not found — falling back to default demo agents." >&2
    printf 'nlit_cora_agent\nnlit_soc_agent\nscap_isso_agent\n'
    return
  fi
  local enabled
  enabled=$(grep -v '^#' "${manifest}" | grep -v '^[[:space:]]*$' || true)
  if [ -z "${enabled}" ]; then
    echo "✗ ${manifest} exists but is empty — misconfiguration, aborting." >&2
    exit 1
  fi
  while IFS= read -r line; do
    if [[ ! "${line}" =~ ^[a-z0-9_]+_agent$ ]]; then
      echo "✗ ${manifest} contains malformed entry: '${line}' (must match ^[a-z0-9_]+_agent$)" >&2
      exit 1
    fi
  done <<< "${enabled}"
  printf '%s\n' "${enabled}"
}
