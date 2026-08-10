# nlit-scap-demo — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-369–D-383 (15 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## NLIT SCAP Demo — Build Decisions (2026-05-04)

**Phase**: build | **Status**: complete | **Total decisions**: 15 (3 user, 12 auto-applied)
**Priority framework**: simplicity > modularity > security > scalability
**Source**: `/Users/joshschultz/Desktop/arc-openscap-nlit-demo.md` (doubles as brainstorm — already covers WHY, audience, use cases, outcomes, principles, build plan)
**Feature scope**: SCAP extension at `~/.arc/capabilities/scap/` + skill at `~/.arc/skills/scap/`. 6 read-only tools. Real federal STIG scan data, hostnames rebranded. 9-min, 5-act NLIT pitch demo. ~13 hr build budget.

#### Summary

SCAP extension wraps OpenSCAP / SCC / STIG Viewer outputs into a queryable model the agent reasons over for ATO evidence assembly, baseline gap analysis, drift detection, and MITRE ATT&CK threat correlation. All 6 tools are `read_only` for the demo (remediation deferred). Dev-mode install (files dropped directly into `~/.arc/capabilities/scap/`) — bundle/signing/marketplace deferred to post-NLIT per source doc §11. Real data from 4 hosts (Palo NDM, NX-OS NDM, RHEL workstation, Windows Server 2019), sanitized at ingest with persistent reviewable mapping. WeasyPrint renders ATO control narratives so they look like real federal documents. Drift artifact for Act 4 produced by a programmatic generator script — reproducible, auditable, runs once.

#### User Decisions

| # | Decision | Choice | Priority | Rationale | Tier Notes |
|---|----------|--------|----------|-----------|------------|

#### Auto-Applied (Convention / Federal Mandate / Source Doc)

| # | Decision | Choice | Source |
|---|----------|--------|--------|

#### Implementation Impact

- **`~/.arc/capabilities/scap/sanitize.py`** owns deterministic redaction; emits `sanitize.mapping_written` audit event on first ingest; writes `data/sanitize_map.toml` with per-host `original → demo.local` mapping.
- **WeasyPrint dependency** added to extension's import surface — `pip install weasyprint` in dev environment; brew prereqs documented in extension `README` (post-demo bundle stage).
- **`scripts/synth_drift.py`** (one-off, repo-resident) reads `stig-wkstn01.ipa.local.xml`, flips configured sshd rules, adjusts XCCDF `<test-result>` start/end times back ~30 days, writes `linux-ws-01.t-30.xml`. Deterministic — same input → same output.
- **Ingest cache** is a module-level `_INGESTS: dict[str, IngestResult]` keyed by `host_alias`. `scap_query`, `scap_crosswalk`, `scap_baseline_compare`, `scap_attack_correlate`, `scap_evidence_pack` all read from it.
- **Tier policy** for demo: dev-mode install bypasses bundle verification entirely (per D-379). Production deployment (post-NLIT) inherits D-372 + signing pipeline from §11.

#### Out-of-Scope (Confirmed Deferred)

- Live `scap_run` (invoking openscap-scanner / scc binaries on the host)
- `scap_remediate` (state-modifying — needs sandbox + policy gate + approval flow)
- `scap_tailor` (XCCDF profile authoring)
- Bundle distribution: `arc ext install scap-1.0.0.tgz`, Sigstore sidecar, TOFU approval (post-NLIT, source doc §11)
- "Act 0" install flow in the pitch (source doc §11.5 — only after distribution lands)

Ready for `/specify`.

---

---
