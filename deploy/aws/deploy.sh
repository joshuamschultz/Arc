#!/usr/bin/env bash
# ============================================================================
# Arc demo deploy — AWS Lightsail + Caddy
# ============================================================================
#
# Mirrors deploy/azure/deploy.sh. Provisions infra via CloudFormation, downloads
# the Lightsail SSH key, prints the next-steps runbook. After this finishes you
# rsync the codebase up, ssh in, run setup-vm.sh, fill in .env, start.
#
# Prerequisites:
#   - aws CLI configured  (aws sts get-caller-identity)
#   - jq                  (brew install jq)
#
# Usage:
#   cd deploy/aws
#   ./deploy.sh
#
# ============================================================================
set -euo pipefail

# --- Configuration ---
STACK_NAME="${STACK_NAME:-arc-demo}"
REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/main.yaml"
PARAMS="${SCRIPT_DIR}/parameters.json"
KEY_PATH="${HOME}/.ssh/lightsail-${REGION}.pem"

echo "=== Arc Demo AWS Deployment ==="
echo "Stack:    ${STACK_NAME}"
echo "Region:   ${REGION}"
echo "Template: ${TEMPLATE}"
echo ""

# --- Step 1: Verify AWS auth ---
echo "[1/6] Verifying AWS auth..."
if ! ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
  echo "  ERROR: aws CLI not configured. Run: aws configure"
  exit 1
fi
echo "  Account: ${ACCOUNT_ID}"

# --- Step 2: Detect your IP for SSH restriction (optional) ---
echo "[2/6] Detecting your public IP (for optional SSH lockdown)..."
MY_IP=$(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || true)
if [ -n "${MY_IP}" ]; then
  echo "  Your IP: ${MY_IP}"
  echo "  (parameters.json keeps SSH open by default; tighten to ${MY_IP}/32 if you want)"
else
  echo "  WARNING: Could not detect IP. SSH will be open per parameters.json."
fi

# --- Step 3: Validate template ---
echo "[3/6] Validating CloudFormation template..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE}" \
  --region "${REGION}" \
  --output text \
  --query "Description" >/dev/null
echo "  Template OK"

# --- Step 4: Deploy stack (create or update, idempotent) ---
echo "[4/6] Deploying stack ${STACK_NAME} (3-5 minutes)..."
PARAM_OVERRIDES=$(jq -r '.[] | "\(.ParameterKey)=\(.ParameterValue)"' "${PARAMS}" | tr '\n' ' ')

# shellcheck disable=SC2086
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE}" \
  --parameter-overrides ${PARAM_OVERRIDES} \
  --tags Project=arc-demo ManagedBy=cloudformation \
  --region "${REGION}" \
  --no-fail-on-empty-changeset

# --- Step 5: Pull stack outputs ---
echo "[5/6] Reading stack outputs..."
OUTPUTS=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query "Stacks[0].Outputs" \
  --output json)

VM_IP=$(echo "${OUTPUTS}" | jq -r '.[] | select(.OutputKey=="StaticIp").OutputValue')
INSTANCE_NAME=$(echo "${OUTPUTS}" | jq -r '.[] | select(.OutputKey=="InstanceName").OutputValue')

# --- Step 6: Download default SSH key (Lightsail-managed) ---
echo "[6/6] Downloading Lightsail default SSH key..."
mkdir -p "$(dirname "${KEY_PATH}")"
if [ ! -f "${KEY_PATH}" ]; then
  aws lightsail download-default-key-pair \
    --region "${REGION}" \
    --query "privateKeyBase64" \
    --output text > "${KEY_PATH}"
  chmod 600 "${KEY_PATH}"
  echo "  Saved: ${KEY_PATH}"
else
  echo "  Already present: ${KEY_PATH}"
fi

# --- Wait for instance to be reachable on SSH (Lightsail boots ~60s) ---
echo "  Waiting for SSH on ${VM_IP}:22 ..."
for i in $(seq 1 30); do
  if nc -z -w 2 "${VM_IP}" 22 2>/dev/null; then
    echo "  SSH is up"
    break
  fi
  sleep 4
done

# --- Summary ---
SSH_CMD="ssh -i ${KEY_PATH} -o StrictHostKeyChecking=accept-new ubuntu@${VM_IP}"

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE"
echo "============================================"
echo ""
echo "Instance:     ${INSTANCE_NAME}"
echo "Static IP:    ${VM_IP}"
echo "SSH key:      ${KEY_PATH}"
echo "SSH command:  ${SSH_CMD}"
echo ""
echo "--- Next Steps (run from REPO_ROOT, ${REPO_ROOT}) ---"
echo ""
echo "1. Point DNS at the static IP:"
echo "     A   demo.blackarcsystems.com   →   ${VM_IP}"
echo "   Verify:  dig +short demo.blackarcsystems.com"
echo ""
echo "2. Push the codebase to the VM (rsync — ~60-90s):"
cat <<EOF
     rsync -avz --delete \\
       --exclude='.git' \\
       --exclude='.venv' \\
       --exclude='__pycache__' \\
       --exclude='.arc-logs' \\
       --exclude='node_modules' \\
       --exclude='*.pyc' \\
       -e "ssh -i ${KEY_PATH} -o StrictHostKeyChecking=accept-new" \\
       ${REPO_ROOT}/ ubuntu@${VM_IP}:/home/ubuntu/arc/
EOF
echo ""
echo "3. SSH in, fill in .env, run setup-vm.sh:"
echo "     ${SSH_CMD}"
echo "     nano ~/arc/.env                                 # ANTHROPIC_API_KEY=..."
echo "     bash ~/arc/deploy/aws/setup-vm.sh demo.blackarcsystems.com my_agent"
echo ""
echo "4. The setup script prints a demo URL — open it."
echo ""
echo "--- Teardown when demo is over ---"
echo "     aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
echo ""
