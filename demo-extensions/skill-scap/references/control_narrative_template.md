# Control Narrative Template

Conventions for writing 800-53 implementation narratives that read like a federal SSP excerpt.

## Voice

- Third-person, declarative, present tense.
- Citation-rich: every claim should be backed by a specific rule ID, CCI, or finding.
- Acknowledge gaps explicitly. Do not paper over failures with hedge words.

## Structure (per control)

```
{CONTROL-ID} — {Control Title}

Status: {Compliant | Partially Compliant | Non-Compliant — Remediation Required | Not Implemented}
Hosts in scope: {comma-separated host aliases}
Failing rules: {N}

Implementation Narrative:
  {3-6 sentences. Open with how the control IS implemented (what the orgs
   does to satisfy it). Then describe the current finding state, citing
   specific rule IDs. Close with what remediation has been planned (POA&M
   reference) or completed.}

Supporting Findings:
  {Markdown table: Rule ID | Severity | Status | Host}

Citations:
  CCIs: {comma-separated}
  References: {comma-separated rule IDs}
```

## Phrasing patterns

- **Compliant control with one minor finding**:
  > "AC-7 (Unsuccessful Logon Attempts) is implemented via PAM `faillock` on
  > the RHEL workstation; the firewall and switch enforce equivalent
  > behavior at the management plane. One rule (`accounts_passwords_pam_faillock_silent`)
  > does not yet meet the recommended baseline; remediation is tracked
  > under POAM-AC-009."

- **Non-compliant control with multiple failures**:
  > "AC-17 (Remote Access) is partially implemented. SSH is the sole
  > remote-administration channel and is restricted to known administrator
  > accounts; however, six SSH-hardening rules currently fail
  > (`sshd_disable_empty_passwords`, `sshd_disable_root_login`, ...).
  > These deficiencies are tracked under POAM-AC-001 with a 14-day target
  > completion."

- **Not yet implemented**:
  > "MA-4(6) (Cryptographic Protection of Maintenance Sessions) is not
  > implemented; current maintenance traffic relies on TLS 1.2 without the
  > cipher suite restrictions required by the High baseline. POAM-MA-002
  > tracks the planned migration to FIPS 140-3 validated modules."

## What to avoid

- Don't promise implementations the data doesn't support. If `scap_query`
  returns 14 failing sshd rules, the narrative says 14, not "minor gaps."
- Don't write generic boilerplate. The specifics — rule IDs, CCI numbers,
  host names — are what makes the document credible to an authorizer.
- Don't omit the citations section. Auditors read by control; the citations
  are how they validate the narrative against the source scan output.
