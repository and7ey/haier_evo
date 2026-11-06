# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** in this repository (private vulnerability reporting). Do not open a public issue for security problems.

Include the version of the integration and of Home Assistant, what an attacker can do and how to reproduce it. You will get an answer within a few days.

## Supported versions

Only the latest release receives security fixes.

## How the integration handles secrets

- The Haier Evo password is used once to sign in (setup and re-authentication) and is not stored. Home Assistant keeps only the cloud tokens, in `.storage/haier_evo.<entry_id>` (file mode 0600, atomic writes); they are deleted together with the integration.
- When the cloud stops accepting the saved tokens, Home Assistant asks for the password again (re-authentication).
- There is no HTTP endpoint of its own. Debug data is available through Home Assistant diagnostics (administrators only), with credentials, tokens, MAC addresses, serial numbers and IP addresses redacted.
- TLS certificates are verified for the HTTP API and the WebSocket. Note that the Haier Evo cloud itself passes the access token in the WebSocket URL.
