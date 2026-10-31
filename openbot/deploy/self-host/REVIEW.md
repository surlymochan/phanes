# Deployment review

Line references identify the files in commit `b49c1b6c8`, before these fixes.

| File:line | Finding | Fix |
| --- | --- | --- |
| `README.md:115`, `systemd/openbot-auth-api.service:14` | Vite preview and local Worker storage are not the documented production account service. The checkout is also not writable by its service user. | Use the documented Cloudflare account service; remove the local account unit and its environment example. |
| `README.md:124` | The local account build and migration commands do not establish a supported production account service. | Link to the documented account deployment and its migrations. |
| `README.md:134` | Changing the group of `/etc/openbot` blocks the other service user from traversing it. | Keep one directory owner and group. systemd reads the environment files as root. |
| `README.md:181`, `systemd/openbot-turn-config.service:6` | A oneshot without an install section cannot be enabled, and its default timeout can stop the five-minute certificate wait. | Start it through the coturn dependency and set a 330-second timeout. |
| `systemd/openbot-caddy.service:11` | The installed Debian binary is `/usr/bin/caddy`; an unprivileged process also needs permission to bind ports 80 and 443. | Use the package path and grant only `CAP_NET_BIND_SERVICE`. Document stopping the package units to prevent port conflicts. |
| `Caddyfile:32` | Access logs can include sensitive request headers and tokens. | Disable access logging for the proxy. |
| `coturn/openbot-turn-config.sh:30` | Environment values can inject coturn directives through newline characters. | Validate the secret, realm, certificate domain and public IPv4 address before rendering. |
| `coturn/openbot-turn-config.sh:65` | Signalling a PID from a file can target a reused PID. | Ask systemd to reload its active coturn service. |
| `README.md:253` | The text says no secrets appear in commands, but the linked key generator prints private keys. | Warn users to run it in a private terminal. |
| `README.md:38` | The guide did not state the credential lifetime. | State the one-hour maximum and session expiry bound used by Signal. |

The Signal and TURN templates use placeholders. The TURN configuration uses shared-secret authentication. No Team API contract changes are included. The bundle has not been deployed or tested on Linux.
