# Security policy

Hal runs a local model that this project treats as untrusted, and gives it tools. Security
reports are welcome and taken seriously.

See the [user security guide](docs/user/security.md) for what Hal protects you from and the
lockdown and paranoia settings.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` and the latest `0.3.x` release | yes |
| `0.2.x`, `0.1.x`, earlier commits and the pre-monorepo launchers | no |

## Report a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Please do not open a public issue or discussion for a security
problem.

Status: unverified that private reporting is switched on for `NotEoin/Hal`. Check under
Settings, Code security, before the release; if it is off, turn it on or replace this section
with a contact address.

Include:

- the version or commit, and your OS and container runtime;
- what you did, what you expected and what happened, with the smallest reproduction you can;
- what an attacker gains (read a file, run a command, leave the sandbox, skip an approval).

This is a hobby-scale project run by one person. Expect an acknowledgement within a week and a
fix or a written assessment as soon as the work allows. There is no bounty. Credit in the
changelog is given unless you ask for none.

## What is in scope

Anything that breaks a guarantee in [the threat model](docs/design/01-security.md), such as:

- an agent or tool getting out of the sandbox, or reaching the network, the user's home
  directory or a secret;
- a tool call running without the approval policy requires, or an approval being bound to
  different arguments than the user saw;
- a secret reaching a log, the audit file, a box or a model;
- a listener that accepts a foreign `Host` or `Origin`, binds beyond loopback or skips its token;
- a supply-chain gap in how Hal installs or pins its own dependencies.

## What is out of scope

The residual risks are documented, not hidden: see "Residual risks" in
[docs/design/01-security.md](docs/design/01-security.md). In short, Hal does not defend against
a compromised host kernel, GPU driver, container daemon or user account, and a harness that
runs unsandboxed on the host (`hal connect`) can reach what the user can reach. Findings that
need one of those are not vulnerabilities in Hal, though a clearer warning is welcome as an
ordinary issue.

## Handling secrets in reports

Do not send real tokens, keys or personal data. Redact them; the audit log already redacts
secrets, so a log excerpt is safe to attach unless you changed `audit.content`.
