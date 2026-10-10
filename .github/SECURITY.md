# Security policy

## Reporting a vulnerability

Please don't open a public issue for security problems.

Report it privately through GitHub: go to the **Security** tab of
[akanani1/c10-g08](https://github.com/akanani1/c10-g08/security) and choose
**Report a vulnerability**. Include what you found, how to reproduce it, and what an attacker
could do with it.

We'll acknowledge the report within 3 days and keep you updated until it's fixed. This is a
hackathon project maintained by a small team, so there is no bug bounty, but we'll credit you
in the fix if you'd like.

## Supported versions

Only the latest commit on `main` (which is what the live demo runs) is supported.

## How the app protects you

- Secrets come only from environment variables or Streamlit secrets, never from the repo.
  gitleaks scans every commit.
- Uploaded logs are treated as data, never as instructions, and secrets and personal data in
  them are redacted before a model sees them.
- Nothing is sent to Slack or JIRA unless live integrations are switched on, and by default a
  person has to approve each send.
- Dependencies are pinned, and pip-audit and Bandit run on every push.
