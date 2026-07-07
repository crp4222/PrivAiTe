# Security policy

## Supported versions

Only the latest released version of `privaite` (on PyPI) receives security fixes.
Please upgrade before reporting.

## Reporting a vulnerability

Do not open a public issue for security problems. Report privately through GitHub's
private vulnerability reporting:

https://github.com/crp4222/PrivAiTe/security/advisories/new

Include a description, affected version, and steps to reproduce. This is a
best-effort, single-maintainer project, so please allow time for a response.

## Scope

PrivAiTe performs reversible pseudonymization, not guaranteed anonymization, and
detection is best-effort. Missed PII from an imperfect detection is a known
limitation documented in the README threat model, not a vulnerability. Genuine
security issues include, for example: the reversible map leaking to logs or disk,
raw PII reaching a provider on a path that should have been scrubbed, or the
fail-closed behavior being bypassed.
