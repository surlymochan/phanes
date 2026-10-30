# Self-hosted Signal and TURN

This bundle runs the OpenBot remote stack on one Linux server. It has no Docker and no
Cloudflare DNS API token. Caddy gets the certificates with ACME HTTP-01, so the ACME server
answers the challenge on port 80 of your host.

The bundle has these parts:

| Part | Path | Use |
| --- | --- | --- |
| Caddy configuration | `Caddyfile` | The TLS front. It proxies to the Signal service. |
| TURN configuration | `coturn/turnserver.conf` | The TURN service. It relays the media of a connection. |
| TURN runtime script | `coturn/openbot-turn-config.sh` | It writes the secret and the certificate into the memory disk. |
| Systemd units | `systemd/` | One unit for each service. |
| Environment examples | `env/` | The secrets. The real files live in `/etc/openbot`. |

The other deployment of the same services is the Docker deployment in `remote/`. It uses a
Cloudflare DNS API token for the certificates. The guide [Self-hosted remote access](../../docs/self-hosting.md) describes the account service
and the Docker deployment. Deploy the account service as that guide describes. This bundle replaces
only its Signal and TURN deployment.

## What you need

- Ubuntu 24.04 or Debian 12 with systemd.
- A static public IPv4 address.
- Two names that point at the server: `signal.example.com` and `turn.example.com`. Use your own names.
- A self-hosted account service on Cloudflare, as described in [the guide](../../docs/self-hosting.md).
- An SMTP account that accepts TLS on port 465. The sign-in code comes from it.
- Bun and Git.
- The packages `coturn` and `caddy`.

The examples in this file use `example.com` and the address `203.0.113.10`. Change every value.

## The ports and the firewall

| Port | Protocol | Use |
| --- | --- | --- |
| 80 | TCP | The challenge of ACME. Caddy sends the other requests to HTTPS. |
| 443 | TCP | The TLS of Caddy. It serves the Signal service. |
| 3478 | TCP and UDP | The TURN service, without TLS. |
| 5349 | TCP | The TURN service, with TLS. |
| 49152-65535 | UDP | The relay ports of the TURN service. |

The services keep their own ports on the loopback address. Only Caddy and coturn open ports.

```sh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 3478/tcp
sudo ufw allow 3478/udp
sudo ufw allow 5349/tcp
sudo ufw allow 49152:65535/udp
```

A relay needs the ports 49152 to 65535 for UDP. A closed port breaks the media of a connection
that has no direct path.

## Install

### 1. The packages and the tools

```sh
sudo apt-get update
sudo apt-get install -y coturn curl git
```

Install Bun, and put it in `/usr/local/bin`, because the units call it there:

```sh
curl -fsSL https://bun.sh/install | bash
sudo install -m 0755 "$HOME/.bun/bin/bun" /usr/local/bin/bun
```

Install Caddy. This bundle uses the Debian package path `/usr/bin/caddy`. Stop and disable the
package services before starting the units below, to prevent port conflicts.

```sh
sudo apt-get install -y caddy
sudo systemctl disable --now caddy.service coturn.service
```

### 2. The server user and the repository

```sh
sudo useradd --system --home-dir /var/lib/openbot --create-home --shell /usr/sbin/nologin openbot
sudo install -d -m 0755 /opt/openbot
sudo git clone https://github.com/nightly-labs/openbot.git /opt/openbot/openbot
cd /opt/openbot/openbot
bun install --frozen-lockfile
```

The coturn package makes the user `turnserver`. The units read environment files as root, so
the TURN user does not need access to `/etc/openbot`. The unit of the TURN service uses it.

### 3. The keys and the secrets

Make the ticket key pair and the secrets as step 1 of [the guide](../../docs/self-hosting.md)
describes. The command in that guide prints `REMOTE_TICKET_KEY_ID`, `REMOTE_TICKET_PRIVATE_JWK`
and `REMOTE_TICKET_PUBLIC_JWKS`.

Make each other secret with `openssl rand -base64 48`. You need four values:

| Secret | Where |
| --- | --- |
| `REMOTE_AUTH_WEBHOOK_SECRET` | The account service and the Signal service. |
| `REMOTE_SESSION_SECRET` | The Signal service. |
| `TURN_SHARED_SECRET` | The Signal service and the TURN service. |
| `REMOTE_METRICS_TOKEN` | The Signal service. |

Each value must have at least 32 bytes.

### 4. The account service

Deploy the account service through steps 1 and 2 of [Self-hosted remote access](../../docs/self-hosting.md).
Set its Signal URL and webhook URL to this server. Keep its private key and secrets private.

### 5. The environment files

The files hold the secrets. They live in `/etc/openbot`, and they never go into the repository.
The Git rule `deploy/self-host/env/*.env` ignores a copy that stays in the repository.

```sh
sudo install -d -m 0750 -o root -g openbot /etc/openbot
sudo install -m 0640 -o root -g openbot deploy/self-host/env/signal.env.example \
  /etc/openbot/signal.env
sudo install -m 0640 -o root -g turnserver deploy/self-host/env/coturn.env.example \
  /etc/openbot/coturn.env
sudoedit /etc/openbot/signal.env
sudoedit /etc/openbot/coturn.env
```

Fill in every value that says `replace-with`. The account service and the Signal service use the
same `REMOTE_AUTH_WEBHOOK_SECRET`. The Signal service and the TURN service use the same
`TURN_SHARED_SECRET`.

### 6. The configurations and the units

Change the two names and the address of the ACME account in the Caddy file before you copy it.

```sh
cd /opt/openbot/openbot
sudo install -m 0644 deploy/self-host/Caddyfile /etc/caddy/Caddyfile
sudo install -m 0644 deploy/self-host/coturn/turnserver.conf /etc/openbot/turnserver.conf
sudo install -m 0755 deploy/self-host/coturn/openbot-turn-config.sh /usr/local/sbin/openbot-turn-config
sudo install -m 0644 deploy/self-host/systemd/*.service deploy/self-host/systemd/*.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
```

The Caddy user must own the storage of the certificates:

```sh
sudo install -d -m 0750 -o caddy -g caddy /var/lib/caddy
```

### 7. Start the services

```sh
sudo systemctl enable --now openbot-signal.service
sudo systemctl enable --now openbot-caddy.service
sudo systemctl enable --now openbot-coturn.service
sudo systemctl enable --now openbot-turn-config.timer
```

Caddy needs some time for the first certificate. The TURN service waits five minutes for that
certificate. If it does not start, run this command again after Caddy has the certificate:

```sh
sudo systemctl start openbot-turn-config.service openbot-coturn.service
```

### 8. Check the services

Each command must answer `200`:

```sh
curl -fsS https://signal.example.com/health/ready
curl -fsS https://api.example.com/health/live
curl -fsS https://api.example.com/.well-known/jwks.json
```

The TURN service answers on the loopback address:

```sh
sudo turnutils_stunclient 127.0.0.1 -p 3478
```

## Use the app

Do these steps on each computer that hosts a server or that connects to one.

1. Sign out in the app if you signed in to `openbot.run` before. The app sends the old session
   token to your service otherwise.
2. Start the app with the two variables. The value of `OPENBOT_REMOTE_SIGNAL_URL` must be equal to
   the `REMOTE_SIGNAL_URL` of the account service.

   macOS:

   ```sh
   OPENBOT_AUTH_API_URL=https://api.example.com \
   OPENBOT_REMOTE_SIGNAL_URL=wss://signal.example.com/v1/signal \
   /Applications/OpenBot.app/Contents/MacOS/OpenBot
   ```

   Linux:

   ```sh
   OPENBOT_AUTH_API_URL=https://api.example.com \
   OPENBOT_REMOTE_SIGNAL_URL=wss://signal.example.com/v1/signal \
   ./OpenBot-<version>-x86_64.AppImage
   ```

3. Sign in with an email address. The code comes from your SMTP account.
4. Connect a provider, as on a hosted server.

## The certificates

Caddy gets the certificate of each name with ACME HTTP-01. The ACME server sends a request to
port 80 of your host, and Caddy answers it. Caddy keeps the certificate and the ACME account in
`/var/lib/caddy`. Caddy replaces a certificate before it ends, and it needs no action from you.

The TURN service cannot read the storage of Caddy, because the user of Caddy owns the files with
the mode 0600. The script `openbot-turn-config` copies the certificate of the TURN domain into
`/run/openbot`. The timer `openbot-turn-config.timer` runs the script each day, and the script
tells coturn to read the new certificate.

## The secrets

Keep secrets out of the repository. The key-generation command in the linked guide prints the
keys; run it in a private terminal. The service secrets are in environment files.

| File | Holds | Group |
| --- | --- | --- |
| `/etc/openbot/signal.env` | The secrets of the Signal service. | `openbot` |
| `/etc/openbot/coturn.env` | The secret of the TURN service. | `turnserver` |

Signal issues TURN credentials that expire after at most one hour, or when the session expires,
whichever comes first. The script of the TURN service writes the secret into `/run/openbot/turnserver.conf`. That file
is in a memory disk, and it has the mode 0600.

## Updates

```sh
cd /opt/openbot/openbot
sudo git pull
bun install --frozen-lockfile
sudo systemctl restart openbot-signal.service
```

A restart of the Signal service closes the WebSocket connections. The clients connect again, and
an active call stays up.

## Not confirmed

- This bundle has not been tested on a Linux server. The units were not started.
- The firewall rules above use `ufw`. Another firewall needs the same ports.
- The certificate of the TURN domain is a copy. A renewal of the certificate in Caddy does not
  reach coturn before the timer runs.
