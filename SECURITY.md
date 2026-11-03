# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting:

<https://github.com/lingmao233/wechat-agent-linux/security/advisories/new>

Include the affected version, a minimal reproduction, and the impact. We will
acknowledge the report and coordinate a fix before public disclosure.

## Account and credential safety

This project automates a real WeChat client. Use a dedicated test account, not
your primary account. Real sends are gated by `WECHAT_ALLOW_SEND`; platform
writes default to dry-run.

The repository ignores `.env`, databases, logs, browser sessions, screenshots,
and local state. If a credential is exposed, rotate the VM password, model API
keys, and the test WeChat session; then remove the secret from Git history
before publishing a fork.

## Supported versions

Only the latest release on `main` is supported with security fixes.
