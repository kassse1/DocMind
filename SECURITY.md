# Security policy

DocMind is a portfolio project. It is not offered as a hosted service and is not
production-ready; the current assessment and residual risks are in
[docs/phase-12-audit.md](docs/phase-12-audit.md).

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately to the
repository owner (GitHub user `kassse1`), through GitHub's private vulnerability reporting
for this repository if it is enabled, with steps to reproduce and the affected commit.
There is no bug bounty and no guaranteed response time.

## Scope

In scope: the code in this repository (API, worker, web BFF, Compose files, scripts).
Out of scope: deployments run by others, and the known limitations documented in the audit
(no TLS in the app, shared rate-limit budget behind the BFF, requests waiting on a frozen
database, open registration without email verification or MFA).

## Handling secrets

Configuration and secrets come from environment variables or git-ignored `.env` files;
the committed `*.env.example` files contain placeholders only. If you find a credential in
the repository or its history, report it as above.
