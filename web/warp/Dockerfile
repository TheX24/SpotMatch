# Cloudflare WARP in proxy mode, so Spotify sees a Cloudflare address instead of
# your server's. Built from Cloudflare's own package repository.
FROM debian:bookworm-slim

RUN apt-get update \
	&& apt-get install -y --no-install-recommends ca-certificates curl dbus gnupg socat \
	&& curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
		| gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
	&& echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" \
		> /etc/apt/sources.list.d/cloudflare-client.list \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends cloudflare-warp \
	&& rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 40001
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
	CMD curl -fsS --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep -q "warp=on"
ENTRYPOINT ["/entrypoint.sh"]
