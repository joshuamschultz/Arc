# Software Bill of Materials (SBOM) & Supply-Chain Analysis

**Project:** Arc — Federal-grade agentic platform (`arcagent` / `arcllm` / `arcrun` and supporting packages)
**Report version:** 1.0
**Generated:** 2026-07-06
**Source revision:** `e63f3a8` (branch `hotfix/cve-ci-security-gate`)
**Audience:** Enterprise security teams, Federal ATO/RMF assessors, supply-chain risk officers

> **Handling:** UNCLASSIFIED // For Official Use Only (FOUO) once populated with
> deployment context. This document and the machine-readable SBOMs in `sbom/`
> together satisfy the SBOM delivery requirement of **EO 14028 §4** and the
> **NTIA Minimum Elements for a Software Bill of Materials**.

---

## 1. Executive Summary

Arc is a 18-package Python monorepo (uv workspace) plus one TypeScript/React
web frontend (`arcui/web`). This analysis inventories every first- and third-party
component, scores known vulnerabilities against live advisory databases (PyPA / OSV /
GitHub Advisory + npm registry), and evaluates license and provenance posture against
federal supply-chain controls.

### Posture at a glance

| Dimension | Result |
|---|---|
| First-party (workspace) packages | **18** |
| Third-party Python components (resolved, all groups) | **114** |
| Third-party Python components (deployed **runtime** only) | **79** |
| Third-party JS/npm components (incl. transitive) | **411** (216 prod) |
| Python packages with known vulnerabilities | **3** (1 in runtime) |
| Total Python vulnerability findings | **5** (1 runtime / 4 dev-build) |
| Distinct Python advisories | **60** |
| JS/npm vulnerabilities | **1** (0 critical / 1 high / 0 moderate / 0 low) |
| License posture | Permissive-dominant; **0** strong-copyleft (GPL/AGPL); 4 weak-copyleft (MPL/LGPL) |

### Key findings

1. **CRITICAL patched in the signing path.** `cryptography` was bumped `46.0.5 -> 46.0.7`,
   closing **PYSEC-2026-36 (CVE-2026-39892, CVSS 9.8)**. This library underpins Arc's
   "Sign" and "Identity" pillars (artifact verification, keypairs). One residual HIGH
   (GHSA-537c-gmf6-5ccf, statically-linked OpenSSL) needs `48.0.1`, held back by the
   `<47.0` FIPS-attribute cap in the `trace-encryption` extra.
2. **Runtime attack surface is materially smaller than raw counts suggest.** Of
   3 vulnerable packages, only **1** ship in the
   deployed runtime. The 4 dev/build-only findings (from `pip-audit`'s own
   `pip`/`msgpack` chain) are not part of the production attack surface, and the tutorial
   Jupyter tooling has been moved out of the runtime closure entirely.
3. **No copyleft contamination risk.** Zero GPL/AGPL. The 4 weak-copyleft
   components (MPL-2.0, LGPL-3.0) are file-/library-level licenses compatible with
   proprietary federal distribution when used unmodified.
4. **Reproducible, hash-pinned builds.** `uv.lock` pins every transitive dependency with
   SHA-256 hashes, satisfying build-integrity expectations for NIST SR-3 / SR-4.

---

## 2. Scope & Methodology

### What was analyzed
- **Python:** the entire uv workspace resolved from `uv.lock` (114 third-party
  components across all dependency groups; 79 in the non-dev runtime set).
- **JavaScript:** `packages/arcui/web` resolved from `package-lock.json`
  (411 components).
- **First-party:** all 18 workspace packages (these are the *subject* of the
  SBOM, audited by source review — they are not published to PyPI and so are correctly
  skipped by advisory-DB scanners).

### Tooling & data sources

| Tool | Version | Purpose |
|---|---|---|
| uv | uv 0.11.17 | Dependency resolution, hash-pinned export |
| pip-audit | 2.10.1 | Python vulnerability scan (PyPA Advisory DB + OSV) |
| CycloneDX (`cyclonedx-py`) | 7.3.0 | CycloneDX 1.6 SBOM generation |
| OSV.dev API | live | CVSS v3 base-score enrichment |
| npm audit | npm 11.16.0 | JS vulnerability scan (GitHub Advisory DB) |
| Python | 3.13.13 | Scoring + report generation |

### NTIA Minimum Elements coverage

| NTIA element | Provided by |
|---|---|
| Supplier name | Component authors / PyPI & npm registries |
| Component name | §5 inventory + CycloneDX `components[].name` |
| Version of component | Pinned in `uv.lock` / `package-lock.json` |
| Other unique identifiers | CycloneDX `purl` (Package URL) per component |
| Dependency relationships | CycloneDX `dependencies` graph; direct/transitive flags §6 |
| Author of SBOM data | This automated pipeline (`sbom/_gen_report.py`) |
| Timestamp | 2026-07-06 |

---

## 3. Machine-Readable SBOM Artifacts

The authoritative, tool-ingestible SBOMs live alongside this report in `sbom/`:

| File | Format | Contents |
|---|---|---|
| `arc-python-env.cdx.json` | CycloneDX 1.6 JSON | Python components **with license + version metadata** (223 components) |
| `arc-python.cdx.json` | CycloneDX 1.6 JSON | Python components from pinned requirements (131 components) |
| `pip-audit.json` | pip-audit JSON | Full Python vulnerability findings + fix versions |
| `npm-audit.json` | npm audit JSON | JS vulnerability findings |
| `osv-severity.json` | JSON | CVSS v3 base scores per advisory (OSV-derived) |
| `requirements-all.txt` | pip requirements (hashed) | Reproducible, SHA-256-pinned full dependency set |
| `requirements-runtime.txt` | pip requirements | Deployed runtime dependency set (no dev groups) |

**Regenerate** with the commands in §11.

---

## 4. Vulnerability Analysis

### 4.1 Severity distribution (Python, distinct advisories)

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 5 |
| Medium | 12 |
| Low | 1 |
| None | 1 |
| Unscored | 40 |

> **On "Unscored":** 40 advisories (largely recent GHSA-only
> entries, e.g. the November-2026 `aiohttp` batch) lack a published CVSS vector in OSV at
> report time. *Unscored ≠ low risk.* They are triaged by scope (runtime vs dev) and by
> fix availability below; all have vendor fixes available.

### 4.2 Runtime vs build-time exposure

Federal risk acceptance hinges on whether a vulnerable component is reachable in the
deployed artifact. Arc's deployed runtime contains **79** of the 114
resolved Python components.

| Exposure | Vulnerable packages | Findings |
|---|---|---|
| **Runtime (deployed attack surface)** | 1 | 1 |
| Dev / build / CI only | 2 | 4 |

### 4.3 Vulnerable Python packages (prioritized)

Sorted by max CVSS, then finding count. **D** = direct dependency, **T** = transitive.

| Package | Installed | Scope | D/T | Findings | Max CVSS | Upgrade to |
|---|---|---|---|---|---|---|
| `cryptography` | 46.0.7 | **Runtime** | T | 1 | High (7.5) | `48.0.1` |
| `msgpack` | 1.1.2 | Dev/Build | T | 1 | High (7.5) | `1.2.1` |
| `pip` | 26.0.1 | Dev/Build | T | 3 | Medium (5.5) | `26.1.2` |

### 4.4 CRITICAL & HIGH detail (Python)

| Severity | Package | Advisory | Aliases | Fix |
|---|---|---|---|---|
| High (7.5) | `cryptography` 46.0.7 | GHSA-537c-gmf6-5ccf | — | `48.0.1` |
| High (7.5) | `msgpack` 1.1.2 | GHSA-6v7p-g79w-8964 | — | `1.2.1` |

### 4.5 JavaScript (`arcui/web`)

npm audit reports **1** vulnerability/-ies
(1 high, 0 moderate,
0 low) across 411 resolved packages.

| Package | Severity | Vulnerable range | Advisory | Fix available |
|---|---|---|---|---|
| `vite` | High | `8.0.0 - 8.0.15` | launch-editor: NTLMv2 hash disclosure via UNC path handling on Windows | Yes — `npm audit fix` |

The single high item is a build-tool advisory (`vite`/`launch-editor`, Windows-only UNC
path handling). It is a **developer-workstation** exposure, not a production-server one,
and a fix is available.

---

## 5. Remediation Status

The residual runtime CVE cluster has been remediated. The blocking CI security
gate now audits the **harness runtime closure only** (workspace packages +
product extras, excluding the `dev`, `test`, and `tutorial` groups) and honors
the POA&M in `sbom/security-suppressions.txt`.

| # | Action | Status |
|---|---|---|
| 1 | **Runtime deps bumped to fixed versions**: `aiohttp→3.14.1`, `pyjwt→2.13.0`, `starlette→1.3.1`, `python-multipart→0.0.32`, `requests→2.34.2`, `urllib3→2.7.0`, `idna→3.18`, `pydantic-settings→2.14.2`, `pygments→2.20.0`, `python-dotenv→1.2.2` | ✅ DONE — constraints raised in owning `pyproject.toml` / workspace `constraint-dependencies`; `uv lock` regenerated |
| 2 | **`cryptography` GHSA-537c-gmf6-5ccf** (fix 48.0.1) | ⏸️ ACCEPTED w/ POA&M — deliberate `<47.0` FIPS 140-3 cap; affected PKCS7/SMIME-decrypt path is unreachable in Arc (Ed25519/X.509 only). See `sbom/security-suppressions.txt`. |
| 3 | **Tutorial/help-docs tooling** (`jupyterlab`, `jupyter-server`, `tornado`, `bleach`) | ✅ EXCLUDED — moved to the `tutorial` dependency group; not part of the deployed runtime, so out of the security gate's scope |
| 4 | **Dev tooling** (`pip`, `msgpack` via `pip-audit`'s filecache) | ✅ EXCLUDED — build-time only, not in the runtime closure |
| 5 | **`npm audit fix`** in `packages/arcui/web` | ◻️ PENDING — `vite` developer-workstation advisory; unchanged by this Python-dependency work |
| 6 | **Blocking CI gate** | ✅ WIRED — `security` job exports the runtime closure and runs `pip-audit` against it; any new unignored runtime vuln fails the build |

---

## 6. Component Inventory

### 6.1 First-party (workspace) packages — the SBOM subject

| Package | Version | Manifest |
|---|---|---|
| `arc` | 0.2.0 | `pyproject.toml` |
| `arc-agent` | 0.4.0 | `packages/arcagent/pyproject.toml` |
| `arccmd` | 0.4.0 | `packages/arccli/pyproject.toml` |
| `arcgateway-mattermost` | 0.1.0 | `packages/arcgateway-mattermost/pyproject.toml` |
| `arcgateway-slack` | 0.1.0 | `packages/arcgateway-slack/pyproject.toml` |
| `arcgateway-telegram` | 0.1.0 | `packages/arcgateway-telegram/pyproject.toml` |
| `arcgateway` | 0.2.0 | `packages/arcgateway/pyproject.toml` |
| `arcllm` | 0.5.1 | `packages/arcllm/pyproject.toml` |
| `arcmas` | 0.3.0 | `packages/arcmas/pyproject.toml` |
| `arcmodel` | 0.0.2 | `packages/arcmodel/pyproject.toml` |
| `arcprompt` | 0.0.2 | `packages/arcprompt/pyproject.toml` |
| `arcrun` | 0.5.0 | `packages/arcrun/pyproject.toml` |
| `arcskill` | 0.1.0 | `packages/arcskill/pyproject.toml` |
| `arcstore` | 0.1.0 | `packages/arcstore/pyproject.toml` |
| `arcteam` | 0.3.0 | `packages/arcteam/pyproject.toml` |
| `arctrust` | 0.3.0 | `packages/arctrust/pyproject.toml` |
| `arctui` | 0.1.0 | `packages/arctui/pyproject.toml` |
| `arcui` | 0.2.0 | `packages/arcui/pyproject.toml` |

> These 18 packages are **not published to public registries**; advisory-DB
> scanners correctly skip them. Their security is established by source review, the
> in-repo test suite, and the Four-Pillars controls (Identity/Sign/Authorize/Audit)
> described in the project standards. Their *dependencies* are fully covered above.

### 6.2 Third-party totals

| Ecosystem | All groups | Runtime only |
|---|---|---|
| Python (PyPI) | 114 | 79 |
| JavaScript (npm) | 411 | 216 (prod) |

Full per-component lists with Package URLs (purl) are in the CycloneDX artifacts (§3).

---

## 7. License Compliance Analysis

License metadata extracted from installed distribution metadata
(223 components; 8 without declared license metadata).

### 7.1 Distribution by license family

| License family | Components |
|---|---|
| MIT | 82 |
| Apache-2.0 | 73 |
| BSD | 70 |
| PSF/Python | 4 |
| MPL-2.0 | 4 |
| ISC | 3 |
| LGPL | 1 |

### 7.2 Copyleft / weak-copyleft components (federal review items)

| Component | Version | License |
|---|---|---|
| `certifi` | 2026.1.4 | MPL-2.0, License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| `fqdn` | 1.5.1 | License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| `pathspec` | 1.0.4 | License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| `python-telegram-bot` | 21.11.1 | LGPL-3.0-only |

**Assessment for federal/enterprise distribution:**
- **No strong copyleft (GPL/AGPL)** — no source-disclosure obligation is triggered by
  distributing Arc.
- **MPL-2.0** (`certifi`, `fqdn`, `pathspec`) and **LGPL-3.0-only**
  (`python-telegram-bot`) are *weak* copyleft. Used **unmodified** and **dynamically
  linked / imported** (as Arc does), they impose no obligation on Arc's own source.
  `python-telegram-bot` is confined to the optional Telegram gateway extension
  (`arcgateway-telegram`); federal deployments that exclude that extension carry no LGPL
  component at all.
- **8 components lack declared license metadata** — resolve before a formal ATO
  package by inspecting upstream `LICENSE` files (typically permissive; absence here is a
  metadata gap, not necessarily a restrictive license).

---

## 8. Supply-Chain Integrity & Provenance

| Control | Status | Evidence / Gap |
|---|---|---|
| Reproducible builds | ✅ | `uv.lock` pins all 114 components with SHA-256 hashes |
| Pinned transitive deps | ✅ | Single workspace lock; `requirements-all.txt` is fully hashed |
| Vulnerability scanning | ✅ | This pipeline (pip-audit + npm audit + OSV) |
| SBOM generation | ✅ | CycloneDX 1.6 artifacts in `sbom/` |
| Dependency provenance attestation | ⚠️ Gap | No Sigstore/SLSA attestation captured for third-party wheels yet |
| Signed first-party artifacts | ↔ In-design | Project standards mandate Sigstore + Rekor signing of loaded modules (Pillar "Sign"); enforce in release pipeline |

**Recommendations for ATO readiness:**
1. Capture SLSA provenance / Sigstore attestations for the runtime wheel set and store
   alongside these SBOMs.
2. Wire this `sbom/` regeneration into CI and **fail the build** on any new
   runtime-scope HIGH/CRITICAL (RA-5 continuous monitoring).
3. Generate a delta SBOM per release and retain for the audit trail (AU family).

---

## 9. Compliance Mapping

| Framework / control | Requirement | How this report supports it |
|---|---|---|
| **EO 14028 §4** | Provide an SBOM to the purchaser | CycloneDX artifacts (§3) + this report |
| **NTIA Minimum Elements** | 7 baseline data fields | Coverage table §2 |
| **NIST 800-53 SA-22** | Unsupported components | §6 inventory surfaces versions/EOL risk |
| **NIST 800-53 SR-3 / SR-4** | Supply-chain controls & provenance | Hash-pinned lock §8 |
| **NIST 800-53 RA-5** | Vulnerability scanning | §4 findings; §8 CI gating recommendation |
| **NIST 800-53 SI-2** | Flaw remediation | §5 prioritized plan with fix versions |
| **NIST 800-53 CM-8** | System component inventory | §5–§6 |
| **CISA SBOM guidance (2024)** | Machine-readable, NTIA-aligned | CycloneDX 1.6 §3 |
| **FedRAMP / CMMC** | Inventory + vuln mgmt + SI-2 evidence | This document as a control artifact |

---

## 10. Guidance by Audience

### Enterprise security teams
- Adopt §5 as a sprint backlog; the 1 runtime packages are the only
  production-facing items. Dev/build findings can ride the normal dependency-bump cadence.
- License posture is clean for commercial redistribution — no GPL/AGPL, weak-copyleft is
  import-only.

### Federal ATO / RMF assessors
- The `cryptography` CRITICAL (CVE-2026-39892, CVSS 9.8) is **remediated** at 46.0.7; the
  residual HIGH (GHSA-537c-gmf6-5ccf) remains a POA&M item, as it is a **runtime**
  cryptographic component (relevant to SC-12/SC-13 and the platform's Sign pillar).
- The 4 dev/build-only findings should be documented as **not in the
  authorization boundary** of the deployed runtime, but **within the CI/CD boundary**
  (address per your pipeline's risk posture).
- Exclude `arcgateway-telegram` from the build to drop the only LGPL component if
  Telegram is not a mission requirement.

---

## 11. Reproducing This Analysis

```bash
# from repo root
mkdir -p sbom
# 1. Export pinned + runtime dependency sets
uv export --frozen --no-emit-project --no-hashes --all-packages \
    --format requirements-txt > sbom/requirements-nohash.txt
uv export --frozen --no-emit-project --no-hashes --no-dev --all-packages \
    --format requirements-txt > sbom/requirements-runtime.txt
uv export --frozen --no-emit-project --all-packages \
    --format requirements-txt > sbom/requirements-all.txt   # hashed

# 2. CycloneDX SBOMs
uv tool run --from cyclonedx-bom cyclonedx-py environment .venv \
    --of JSON -o sbom/arc-python-env.cdx.json
uv tool run --from cyclonedx-bom cyclonedx-py requirements \
    sbom/requirements-nohash.txt --of JSON -o sbom/arc-python.cdx.json

# 3. Vulnerability scans
uv tool run pip-audit -r sbom/requirements-nohash.txt --format json \
    > sbom/pip-audit.json
( cd packages/arcui/web && npm audit --json ) > sbom/npm-audit.json

# 4. Regenerate this report
python3 sbom/_gen_report.py
```

**Recommended cadence:** on every dependency change, weekly in CI, and as a release gate.

---

## 12. Appendix — Caveats

- CVSS enrichment uses OSV-published CVSS v3 vectors; advisories without a vector are
  marked *Unscored* (not absent). Cross-reference NVD for authoritative scores during ATO.
- Severity bands: Critical ≥9.0, High 7.0–8.9, Medium 4.0–6.9, Low 0.1–3.9 (CVSS v3).
- The dev/build-only classification reflects uv dependency groups (`--no-dev` runtime set);
  confirm against your actual packaging/deployment manifest before relying on it for an
  authorization boundary.
- Findings are point-in-time as of **2026-07-06** against `e63f3a8`. Advisory
  databases update continuously — regenerate before any submission.

*Generated by `sbom/_gen_report.py` from live scan artifacts. Do not edit by hand; edit the
generator and re-run so the report stays consistent with the data.*

