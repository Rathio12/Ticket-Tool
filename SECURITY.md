# Security Policy

## Supported Versions

Ticket Tool doesn't currently ship tagged releases — only the `main` branch is
maintained and receives security fixes. If you're running a fork or an older
pulled commit, update to the latest `main` before reporting an issue that may
already be fixed.

| Branch | Supported          |
| ------ | ------------------ |
| main   | :white_check_mark: |

## Reporting a Vulnerability

Please **do not** open a public Issue or Discussion for security
vulnerabilities.

Instead, use GitHub's private reporting:

1. Go to the **Security** tab of this repository
2. Click **Report a vulnerability**
3. Describe the issue, how to reproduce it, and its impact

You should get an initial response within a few days. If the report is
confirmed, a fix will be prepared before any public disclosure; if it's
declined (not reproducible, out of scope, etc.), you'll get an explanation
why.

## Scope notes

Ticket Tool runs with a real Discord bot token and per-server configuration
(staff roles, channels, ticket data) stored in `Data/`. Vulnerabilities of
particular interest include:

- Anything letting a non-staff user claim staff-only actions (claim, close,
  escalate, transfer, admin-only slash commands)
- Anything letting one server's config/tickets leak into another server
- Injection or path-traversal issues in transcript/config file handling
- Token or credential exposure

Please avoid testing against a production bot instance — spin up a private
test bot/server instead.
