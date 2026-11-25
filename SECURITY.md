# Security Policy

## Reporting a vulnerability

Please **don't** open a public issue for a security problem. Use GitHub's private reporting
instead: go to the repository's **Security** tab and choose **Report a vulnerability**.

Please include what you found, how to reproduce it, and what an attacker could do with it.
You'll get a reply as soon as possible. Please give the maintainer reasonable time to fix the
problem before you disclose it publicly.

## Supported versions

Only the latest release (`main`) gets security fixes.

## Good to know when you deploy

- API keys are stored as SHA-256 hashes and shown once. Keep `GROQ_API_KEY` in the environment
  or `.env`, never in `relay.toml`.
- The development key `relay-dev-key` works only while the server listens on loopback.
  Disable it (`[auth] dev_key_enabled = false`) for any shared deployment.
- Docker Compose binds the API to `127.0.0.1:8000` by default. Put a TLS-terminating proxy in
  front of it before you expose it.
