# Security

Keep local `config.json`, environment files, deployment bundles, credentials,
logs and backups private. They are ignored by Git. Never force-add these files.
The versioned default configuration is an example containing no signing keys,
passwords, private server addresses or operator wallet accounts.

Bind to loopback behind a reverse proxy. Public service URLs should use DNS
names; DNS names do not guarantee that an origin IP cannot be discovered.
Run the service as an unprivileged user and maintain your own TLS, access
controls, request limits and deployment review. Do not put credentials in URLs.

Changes run automated source and history secret scans. A passing scan is not
a full production security audit. Report vulnerabilities privately to
support@filetrust.io; do not post credentials or operational logs in issues.
