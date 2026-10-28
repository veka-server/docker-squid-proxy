FROM alpine:latest

# squid + su-exec (drop de privilèges) + python3 (interface web, stdlib uniquement)
RUN apk add --no-cache squid su-exec python3 && \
    adduser -S -D -H -h /data -s /sbin/nologin squidui && \
    mkdir -p /var/lib/squid /data /run/squid-proxy /etc/squid/profiles /etc/squid/conf.d && \
    chown squid:squid /var/lib/squid && \
    chown squidui /data

# Whitelists par défaut (mode legacy, si aucun profil n'est monté)
COPY whitelist.txt whitelist-ips.txt /etc/squid/
COPY --chmod=755 entrypoint.sh /entrypoint.sh
COPY ui/ /app/

ENV PROXY_PORT=3128 \
    UI_ENABLED=1 \
    UI_PORT=8080 \
    UI_ACCESS=egress \
    DB_PATH=/data/history.db \
    HISTORY_DAYS=30 \
    DENY_NETWORKS="" \
    BLOCK_PRIVATE_DESTINATIONS=0 \
    RELOAD_INTERVAL=10 \
    PROFILE_ALIAS_PREFIX=squid-profile- \
    TOR_SOCKS="" \
    DNS_GUARD=1

EXPOSE 3128 8080
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD sh -c 'if [ "$UI_ENABLED" = 1 ]; then wget -q -O /dev/null "http://127.0.0.1:$UI_PORT/healthz"; else kill -0 "$(cat /var/lib/squid/squid.pid)"; fi'

# L'entrypoint génère squid.conf, lance l'interface web et le rechargement à chaud,
# puis exec squid en foreground (PID 1, arrêt propre sur SIGTERM).
ENTRYPOINT ["/entrypoint.sh"]
