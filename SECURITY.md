# Security Policy

FAB processes financial documents, accounting records, and provider credentials.
Please do not report a suspected vulnerability in a public issue, discussion,
pull request, or email that contains exploit details or personal/financial data.

## Reporting a Vulnerability

Use GitHub's **Report a vulnerability** action on the repository's Security
page to submit a private vulnerability report. This is the preferred channel and
keeps the report visible only to repository maintainers. Include the affected
commit or release, impact, prerequisites, and a minimal reproduction that uses
synthetic data. Do not attach real receipts, bank statements, account numbers,
OAuth tokens, passwords, encryption keys, or customer data.

If private reporting is unavailable, contact the repository owner through the
contact method listed on the owner's GitHub profile and ask for a secure channel
before sending technical details. Do not assume that a public issue is private.

## Response and Disclosure

The project is maintained on a best-effort basis. No guaranteed response or
fixing time is currently offered. The maintainer will acknowledge and assess a
valid report when available, coordinate a fix and disclosure where practical,
and may request clarification using the private report. Please allow a
reasonable opportunity to investigate and remediate before public disclosure.

## Supported Versions

| Version | Security fixes |
| --- | --- |
| `v0.1.0-preview.1` | Best-effort review; update to the newest preview. |
| Latest `main` | Best-effort review; not a stable release. |

FAB is an early development preview and no service-level response or remediation time is offered. Users should update to the newest release and rotate any credential that may have been exposed.

## Scope and Limitations

This policy does not promise that FAB is penetration-tested, certified, or
compliant with financial, tax, or privacy regulations. Never send live provider
credentials or production financial data as part of a security report.
