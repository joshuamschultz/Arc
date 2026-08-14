#!/usr/bin/env bash
# scripts/install-nats.sh — put a verified `nats-server` binary on this box.
#
# arcteam spawns the broker itself but cannot install it, and without the binary
# every agent silently loses team messaging and task dispatch while the process
# stays healthy. So this is a prerequisite of a working deployment, not an
# optional extra.
#
# Idempotent: a nats-server already resolvable is left alone and the script
# exits 0. Installs to ~/.local/bin, which is where `arcteam.find_nats_server`
# looks when $PATH does not carry it — a non-interactive `ssh host '...'` and a
# systemd unit both routinely have a PATH that does not.
#
# The download is checksum-verified against the release's published SHA256SUMS
# and refuses to install an unverified archive when sums are published but the
# entry is missing (LLM03 supply chain). Both scripts/install.sh and
# scripts/deploy-node.sh call this file rather than carrying their own copy —
# two copies of a verification routine is one copy that stops being verified.

set -euo pipefail

log()  { echo "→ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*" >&2; exit 1; }

# Same reach as arcteam.find_nats_server, so this script and the runtime agree
# about whether the box already has a broker binary.
for candidate in \
  "$(command -v nats-server 2>/dev/null || true)" \
  "$HOME/.local/bin/nats-server" \
  /usr/local/bin/nats-server \
  /opt/homebrew/bin/nats-server \
  /usr/bin/nats-server
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    ok "nats-server already installed: $candidate"
    exit 0
  fi
done

case "$(uname -s)" in
  Darwin)
    command -v brew >/dev/null 2>&1 \
      || fail "install Homebrew, or fetch nats-server from https://github.com/nats-io/nats-server/releases"
    log "Installing nats-server via brew..."
    brew install nats-server
    ok "nats-server: $(nats-server --version)"
    exit 0
    ;;
  Linux) ;;
  *) fail "unsupported OS $(uname -s) — install nats-server manually onto PATH" ;;
esac

case "$(uname -m)" in
  aarch64|arm64) NATS_ARCH="arm64" ;;
  x86_64)        NATS_ARCH="amd64" ;;
  *) fail "unsupported arch $(uname -m) — install nats-server manually to ~/.local/bin" ;;
esac

log "Installing nats-server (linux-${NATS_ARCH})..."
RELEASE_JSON="$(curl -s https://api.github.com/repos/nats-io/nats-server/releases/latest)"
NATS_URL="$(echo "$RELEASE_JSON" \
  | grep -oE "\"browser_download_url\": *\"[^\"]*linux-${NATS_ARCH}\.tar\.gz\"" \
  | cut -d'"' -f4)"
[ -n "$NATS_URL" ] || fail "could not resolve latest nats-server release for linux-${NATS_ARCH}"
SHASUMS_URL="$(echo "$RELEASE_JSON" \
  | grep -oE '"browser_download_url": *"[^"]*/SHA256SUMS"' \
  | cut -d'"' -f4)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
TGZ_NAME="$(basename "$NATS_URL")"
curl -LsSf -o "$TMP_DIR/$TGZ_NAME" "$NATS_URL"

if [ -n "$SHASUMS_URL" ]; then
  EXPECTED_SUM="$(curl -sL "$SHASUMS_URL" | grep "  ${TGZ_NAME}\$" | awk '{print $1}')"
  ACTUAL_SUM="$(sha256sum "$TMP_DIR/$TGZ_NAME" | awk '{print $1}')"
  [ -n "$EXPECTED_SUM" ] || fail "SHA256SUMS published but no entry for $TGZ_NAME — refusing to install unverified"
  [ "$EXPECTED_SUM" = "$ACTUAL_SUM" ] || fail "nats-server checksum mismatch: expected $EXPECTED_SUM got $ACTUAL_SUM"
  ok "nats-server checksum verified ($ACTUAL_SUM)"
else
  echo "  ! release did not publish SHA256SUMS — proceeding unverified (was verified against v2.14.3 at script-write time)"
fi

tar xzf "$TMP_DIR/$TGZ_NAME" -C "$TMP_DIR"
mkdir -p "$HOME/.local/bin"
mv "$TMP_DIR"/nats-server-*/nats-server "$HOME/.local/bin/nats-server"
chmod +x "$HOME/.local/bin/nats-server"
ok "nats-server $("$HOME/.local/bin/nats-server" --version) installed to ~/.local/bin"
