#!/bin/sh
# Renders the runtime configuration of the TURN service.
#
# The unit openbot-turn-config.service runs this script as root before it starts coturn. The
# timer runs it again each day, so the service gets the new certificate of Caddy. The script
# writes the secret and the certificate into /run/openbot, which is a memory disk. The secret is
# never in a file that stays on the disk.
#
# The values come from the environment file of the unit, /etc/openbot/coturn.env.
set -eu

runtime_directory=/run/openbot
template=/etc/openbot/turnserver.conf
config="${runtime_directory}/turnserver.conf"

: "${TURN_SHARED_SECRET:?TURN_SHARED_SECRET is required}"
: "${TURN_REALM:?TURN_REALM is required}"

if [ "${#TURN_SHARED_SECRET}" -lt 32 ]; then
  echo "TURN_SHARED_SECRET must contain at least 32 bytes." >&2
  exit 1
fi

# These values become coturn directives. Reject newlines and other config syntax.
case "$TURN_SHARED_SECRET" in *[!A-Za-z0-9+/=]*|'') echo "Invalid TURN_SHARED_SECRET." >&2; exit 1;; esac
case "$TURN_REALM" in *[!A-Za-z0-9.-]*|'') echo "Invalid TURN_REALM." >&2; exit 1;; esac
case "${TURN_CERTIFICATE_DOMAIN:-$TURN_REALM}" in *[!A-Za-z0-9.-]*|'') echo "Invalid certificate domain." >&2; exit 1;; esac
case "${TURN_PUBLIC_IP:-}" in *[!0-9.]*) echo "Invalid TURN_PUBLIC_IP." >&2; exit 1;; esac

if [ ! -f "$template" ]; then
  echo "The TURN configuration is missing: ${template}" >&2
  exit 1
fi

install -d -m 0750 -o turnserver -g turnserver "$runtime_directory"
install -m 0600 -o turnserver -g turnserver "$template" "$config"
{
  echo "realm=${TURN_REALM}"
  echo "server-name=${TURN_REALM}"
  echo "static-auth-secret=${TURN_SHARED_SECRET}"
  if [ -n "${TURN_PUBLIC_IP:-}" ]; then
    echo "external-ip=${TURN_PUBLIC_IP}"
  fi
} >>"$config"

# Caddy keeps one directory per certificate, under its storage root. The issuer directory has the
# name of the certificate authority, so the script takes the newest directory of the domain.
certificate_root="${CADDY_CERTIFICATE_ROOT:-/var/lib/caddy/certificates}"
domain="${TURN_CERTIFICATE_DOMAIN:-$TURN_REALM}"
directory=""
waited=0
while [ -z "$directory" ]; do
  directory="$(ls -dt "$certificate_root"/*/"$domain" 2>/dev/null | head -n 1 || true)"
  if [ -n "$directory" ]; then
    break
  fi
  if [ "$waited" -ge 300 ]; then
    echo "Caddy has no certificate for ${domain} in ${certificate_root}." >&2
    echo "Start Caddy first, and check that ${domain} points at this host." >&2
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

install -m 0644 -o root -g turnserver "$directory/$domain.crt" "${runtime_directory}/turn.crt"
install -m 0640 -o root -g turnserver "$directory/$domain.key" "${runtime_directory}/turn.key"

# Ask systemd to signal only the running coturn service.
if systemctl is-active --quiet openbot-coturn.service; then
  systemctl reload openbot-coturn.service
fi
