# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** in this repository (private vulnerability reporting). Do not open a public issue for security problems.

Include the version of the integration and of Home Assistant, what an attacker can do and how to reproduce it. You will get an answer within a few days.

## Supported versions

Only the latest release receives security fixes.

## How the integration handles secrets

- The Haier Evo password is used once to sign in (setup and re-authentication) and is not stored. Home Assistant keeps only the cloud tokens, in `.storage/haier_evo.<entry_id>` (file mode 0600, atomic writes); they are deleted together with the integration.
- When the cloud stops accepting the saved tokens, Home Assistant asks for the password again (re-authentication).
- There is no HTTP endpoint of its own. Debug data is available through Home Assistant diagnostics (administrators only). Credentials, tokens, MAC addresses, serial numbers and IP addresses are redacted by the keys the cloud uses for them and by key patterns for ones it may use (users, Wi-Fi, location); e-mail addresses, phone numbers, MAC addresses in any notation, JSON web tokens and the values of link queries are redacted in any field.
- The log masks MAC addresses (`30:c9:22:**:**:2c`) and has no serial numbers.
- TLS certificates are verified for the HTTP API and the WebSocket. Note that the Haier Evo cloud itself passes the access token in the WebSocket URL.
- HTTP redirects are not followed: `requests` would otherwise send the access token (`X-Auth-Token`), and on 307/308 the sign-in form with the password, to another host.
- Each config entry has its own rate limits: signing in elsewhere (config flow, re-authentication, another account) does not reset its back-off after HTTP 429.
