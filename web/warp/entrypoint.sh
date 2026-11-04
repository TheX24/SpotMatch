#!/bin/sh
# Starting this container accepts Cloudflare's WARP terms of service:
# https://www.cloudflare.com/application/terms/
set -e

# warp-svc watches network and power state over the system D-Bus; without it the
# connection keeps resetting itself.
mkdir -p /run/dbus
rm -f /run/dbus/pid
dbus-daemon --config-file=/usr/share/dbus-1/system.conf

warp-svc --accept-tos &
warp_pid=$!

for _ in $(seq 30); do
	warp-cli --accept-tos status >/dev/null 2>&1 && break
	sleep 1
done

# Registration persists in the /var/lib/cloudflare-warp volume across restarts.
if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
	warp-cli --accept-tos registration new
fi
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

# WARP's proxy only listens on localhost; expose it to the compose network.
socat TCP-LISTEN:40001,fork,reuseaddr TCP:127.0.0.1:40000 &
socat_pid=$!

trap 'kill "$warp_pid" "$socat_pid" 2>/dev/null; exit 0' TERM INT

# warp-svc leaks memory and eventually hits mem_limit. If it (or socat) dies, exit
# so Docker's restart policy brings the container back; an unhealthy healthcheck
# alone never triggers a restart.
while kill -0 "$warp_pid" 2>/dev/null && kill -0 "$socat_pid" 2>/dev/null; do
	sleep 5
done
echo "warp-svc or socat exited; restarting container" >&2
exit 1
