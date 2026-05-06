# Reference Data Provenance

Per SPEC-024 D-380, D-382, D-383. All reference data is bundled at build time;
the demo runs offline.

## `nist_800_53_rev5.json`

- **Source**: NIST SP 800-53 Rev 5 (https://csrc.nist.gov/publications/detail/sp/800-53/rev-5/final)
- **Authoritative format**: NIST OSCAL JSON catalog at https://github.com/usnistgov/oscal-content/tree/main/nist.gov/SP800-53/rev5/json
- **What's bundled**: curated subset (~75 control IDs) covering controls referenced by the demo source data — AC, AU, CM, IA, MA, SC, SI families with their relevant enhancements.
- **What's not bundled**: full control text, assessment guidance, related-controls graph, parameter declarations.
- **Production note**: the post-NLIT productized version should load the full OSCAL catalog at install time and ship it under `data/oscal/` keyed by version.

## `fedramp_baselines.json`

- **Source**: FedRAMP Rev 5 Baselines (https://www.fedramp.gov/rev5/baselines/)
- **Authoritative format**: GSA FedRAMP automation OSCAL profiles at https://github.com/GSA/fedramp-automation/tree/master/dist/content/rev5/baselines/json
- **What's bundled**: control-ID membership lists for Low / Moderate / High baselines.
- **What's not bundled**: parameter overrides, organization-defined-parameter (ODP) values, status assertions, profile metadata.
- **Production note**: the post-NLIT version should resolve the FedRAMP profiles dynamically from the OSCAL catalog rather than keep a flat list.

## `attack_to_800_53.json`

- **Source**: MITRE Center for Threat-Informed Defense (CTID) NIST 800-53 → ATT&CK mapping (https://center-for-threat-informed-defense.github.io/mappings-explorer/)
- **Authoritative format**: published CTID mapping JSON at https://github.com/center-for-threat-informed-defense/mappings-explorer/tree/main/mappings
- **What's bundled**: curated control → ATT&CK technique map covering ~30 demo-relevant controls, plus a `narratives` section with plain-language threat framing per control.
- **What's not bundled**: full enterprise ATT&CK matrix, sub-techniques outside the demo's narrative, ICS/Mobile mappings, mapping confidence scores.
- **Production note**: the threat narratives are author-provided framing — they should be replaced with CTID-published rationale text when bundling the full mapping.

## Demo data integrity

Reference data files are read-only at runtime. No tool in the SCAP extension
writes back to `data/` (except `sanitize.py`, which writes only `sanitize_map.toml`).
