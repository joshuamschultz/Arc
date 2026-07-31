# Firecracker Deployment Guide

Operator reference for deploying the Arc Skills Hub Firecracker microVM sandbox on Linux.

This file documents all steps that require privileged Linux access, real hardware (KVM), and
operator-supplied image artifacts.  The wrapper code in `arcskill/hub/dry_run.py` is
production-complete; the following steps are what an operator must supply before the code
can actually run.

---

## What the Code Does vs. What You Must Provide

| Component | Status | Where |
|-----------|--------|-------|
| `FirecrackerSandbox` class | Complete | `arcskill/hub/dry_run.py` |
| `is_firecracker_available()` | Complete | `arcskill/hub/dry_run.py` |
| Fallback chain (federal/enterprise/personal) | Complete | `arcskill/hub/dry_run.py` |
| `SandboxRequired` fail-closed enforcement | Complete | `arcskill/hub/errors.py` |
| Linux KVM kernel module | Operator | Host OS |
| `firecracker` binary (v1.7+) | Operator | Install on PATH |
| `jailer` binary | Operator | Install on PATH |
| Linux kernel image (`vmlinux.bin`) | Operator | Build or download |
| Root filesystem image (`rootfs.ext4`) | Operator | Build (see below) |
| KVM device permissions | Operator | `chown` or udev rule |

---

## Prerequisites

### 1. Hardware and Kernel

Firecracker requires hardware virtualisation.  Verify it is enabled:

```bash
# Confirm KVM module is loaded
lsmod | grep kvm

# Confirm /dev/kvm is present
ls -la /dev/kvm

# Expected output:
# crw-rw---- 1 root kvm 10, 232 ...  /dev/kvm
```

If `/dev/kvm` is absent, either:
- Enable VT-x / AMD-V in BIOS/UEFI firmware settings
- Load the KVM kernel module: `sudo modprobe kvm_intel` or `sudo modprobe kvm_amd`

### 2. Install Firecracker and Jailer

Download the latest release from https://github.com/firecracker-microvm/firecracker/releases
(v1.7 or later recommended):

```bash
# Example for x86_64 — adjust for arm64 if needed
FC_VERSION="v1.7.0"
ARCH="x86_64"
RELEASE_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}"

# Download the release archive
curl -L "${RELEASE_URL}/firecracker-${FC_VERSION}-${ARCH}.tgz" -o /tmp/firecracker.tgz
tar -xzf /tmp/firecracker.tgz -C /tmp

# Install binaries to PATH
sudo install -o root -g root -m 0755 \
    /tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH} \
    /usr/local/bin/firecracker

sudo install -o root -g root -m 0755 \
    /tmp/release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH} \
    /usr/local/bin/jailer

# Verify
firecracker --version
jailer --version
```

### 3. KVM Permissions

The Arc process must be able to access `/dev/kvm`.  The recommended approach is
a udev rule that grants access to the `kvm` group:

```bash
# Add the arc service user to the kvm group
sudo usermod -aG kvm arc-agent

# Or create a udev rule for a dedicated service user
cat > /etc/udev/rules.d/99-kvm.rules << 'EOF'
KERNEL=="kvm", GROUP="kvm", MODE="0660", TAG+="uaccess"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger
```

The jailer binary itself requires `CAP_SYS_ADMIN` to set up the chroot and
network namespace.  Grant it via file capabilities (preferred over setuid):

```bash
sudo setcap cap_sys_admin+ep /usr/local/bin/jailer
```

---

## Building the Guest Kernel Image

The microVM needs a Linux kernel configured for Firecracker.  Arc provides
a recommended minimal configuration.  Build with:

```bash
# Clone the Firecracker guest kernel config
git clone https://github.com/firecracker-microvm/firecracker.git /tmp/firecracker-src
cd /tmp/firecracker-src

# Use the recommended guest kernel config for x86_64
KERNEL_VERSION="5.10"  # adjust to your target
./resources/guest_configs/make_kernel_config.sh microvm x86_64 ${KERNEL_VERSION}

# Build the kernel (requires build-essential, flex, bison, etc.)
make -j$(nproc) vmlinux

# Install
sudo install -m 0644 vmlinux /var/lib/arc/vmlinux.bin
```

For air-gapped environments, the Arc Foundation provides a pre-built kernel image
in the `arc-federal-base` artifact bundle (contact your Arc support representative).

---

## Building the Root Filesystem Image

The rootfs is an ext4 image containing Python 3.11+ and a minimal init script
that handles the vsock command protocol used by `FirecrackerSandbox`.

### Using the Arc build script (recommended)

```bash
# Run from the arc repository root
./scripts/build-firecracker-rootfs.sh \
    --output /var/lib/arc/rootfs.ext4 \
    --size-mib 512 \
    --python-version 3.11
```

### Manual build

```bash
# Create an empty ext4 image
dd if=/dev/zero bs=1M count=512 | sudo tee /tmp/rootfs.ext4 > /dev/null
mkfs.ext4 /tmp/rootfs.ext4

# Mount and populate
mkdir /tmp/rootfs-mnt
sudo mount /tmp/rootfs.ext4 /tmp/rootfs-mnt

# Install a minimal Python environment (e.g. using debootstrap or Alpine)
# ... (distribution-specific steps) ...

# Install the Arc vsock init daemon
# This is a minimal Go binary that reads {"cmd": "..."} from vsock CID 3
# and responds with {"stdout": "...", "stderr": "...", "exit_code": N}
sudo cp ./scripts/arc-vsock-init /tmp/rootfs-mnt/sbin/init

sudo umount /tmp/rootfs-mnt
sudo install -m 0644 /tmp/rootfs.ext4 /var/lib/arc/rootfs.ext4
```

The vsock init daemon source is in `scripts/arc-vsock-init/` in this repository.

---

## Seccomp Profile

The jailer accepts a seccomp filter JSON file.  The Firecracker project provides
a default profile.  For Arc deployments, use the strict production profile:

```bash
# Copy the upstream default seccomp profile
cp ./resources/seccomp/x86_64-unknown-linux-musl.json \
    /etc/arc/firecracker-seccomp.json

# The jailer will apply this automatically when --seccomp-level=2 is passed.
# FirecrackerSandbox uses the default profile; to override, set:
# ARC_FC_SECCOMP_PROFILE=/path/to/profile.json
```

---

## Environment Variables

Configure the Arc Skills Hub Firecracker paths via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ARC_FC_KERNEL` | `/var/lib/arc/vmlinux.bin` | Path to kernel image |
| `ARC_FC_ROOTFS` | `/var/lib/arc/rootfs.ext4` | Path to rootfs image |
| `ARC_FC_SECCOMP_PROFILE` | upstream default | Path to seccomp filter JSON |

These can be set in the Arc service environment file (`/etc/arc/env`) or in the
systemd unit override.

---

## Verification

Once installed, verify the setup with:

```bash
# Check availability
python3 -c "from arcskill.hub.dry_run import is_firecracker_available; print(is_firecracker_available())"
# Expected: True

# Run the integration test (requires a valid kernel + rootfs)
ARC_FC_KERNEL=/var/lib/arc/vmlinux.bin \
ARC_FC_ROOTFS=/var/lib/arc/rootfs.ext4 \
uv run pytest packages/arcskill/tests/unit/hub/test_dry_run_firecracker.py \
    -k test_real_firecracker_execute -v
```

---

## Jailer Chroot Layout

The jailer creates the following directory structure per VM under `/srv/jailer/`:

```
/srv/jailer/firecracker/<vm_id>/
    root/
        v.sock          ← vsock Unix socket (created by Firecracker)
        skill/          ← skill directory bind-mounted read-only
        vm-config.json  ← Firecracker config (written by FirecrackerSandbox)
```

All directories under `/srv/jailer/` are cleaned up by `FirecrackerSandbox._cleanup()`
after each dry-run.  Ensure the service user has write access to `/srv/jailer/`.

```bash
sudo mkdir -p /srv/jailer
sudo chown arc-agent:arc-agent /srv/jailer
sudo chmod 700 /srv/jailer
```

---

## Security Notes

- The rootfs image is mounted **read-only** (`is_read_only: true` in the drive config).
- The skill directory is bind-mounted **read-only** into the chroot.
- **Networking is disabled by default** (`network_interface="none"`); no outbound
  connectivity is possible from the skill code.
- The jailer drops **all Linux capabilities** before exec-ing Firecracker.
- A seccomp-BPF filter restricts the system calls available to the Firecracker process.
- Each VM gets a unique UUID; there is no shared state between dry-run invocations.
- The chroot directory is removed on every exit, including timeout and error paths.

These controls directly mitigate:
- **ASI05 (RCE)**: Firecracker hardware boundary prevents guest-to-host escape
- **LLM03 / ASI04 (Supply Chain)**: Untrusted skill code cannot reach the network
- **LLM06 (Excessive Agency)**: Read-only mounts prevent skill from modifying its source

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `SandboxRequired: /dev/kvm not found` | KVM not enabled | Enable VT-x in BIOS or load kvm module |
| `SandboxRequired: jailer binary not found` | jailer not on PATH | Install jailer to `/usr/local/bin/jailer` |
| `SandboxRequired: kernel not found` | ARC_FC_KERNEL path wrong | Set `ARC_FC_KERNEL` env var |
| `mount: Permission denied` | Missing CAP_SYS_ADMIN | `setcap cap_sys_admin+ep /usr/local/bin/jailer` |
| VM does not boot within timeout | Slow disk / wrong kernel | Check kernel config; increase boot timeout |
| `[DRY-RUN TIMEOUT]` in result | Skill hangs | Fix skill test_fixture; reduce complexity |
