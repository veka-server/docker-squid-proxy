FROM alpine:latest

# Installer Squid et nettoyer le cache
RUN apk add --no-cache squid && \
    rm -rf /var/cache/apk/*

# Copier le fichier de white liste de domaine
COPY ./whitelist.txt /etc/squid/whitelist.txt

# Configurer Squid
RUN echo "http_port 3128" > /etc/squid/squid.conf && \
    echo "max_filedescriptors 1024" >> /etc/squid/squid.conf && \
    echo "pid_filename /var/lib/squid/squid.pid" >> /etc/squid/squid.conf && \
    echo "cache_dir ufs /var/cache/squid 8 16 256" >> /etc/squid/squid.conf && \
    echo "cache_mem 8 MB" >> /etc/squid/squid.conf && \
    echo "maximum_object_size 1 MB" >> /etc/squid/squid.conf && \
    echo "minimum_object_size 0 KB" >> /etc/squid/squid.conf && \
    echo "acl whitelist dstdomain \"/etc/squid/whitelist.txt\"" >> /etc/squid/squid.conf && \
    echo "http_access allow whitelist" >> /etc/squid/squid.conf && \
    echo "http_access deny all" >> /etc/squid/squid.conf && \
    echo "access_log stdio:/var/log/squid/access.log squid" >> /etc/squid/squid.conf && \
    echo "cache_log /var/log/squid/cache.log" >> /etc/squid/squid.conf

# Préparer et initialiser le cache
RUN mkdir -p /var/cache/squid /var/log/squid /var/lib/squid && \
    chown -R squid:squid /var/cache/squid /var/log/squid /var/lib/squid 
#    su -s /bin/sh squid -c "/usr/sbin/squid -Nz"

# Exposer le port du proxy
EXPOSE 3128

# Initialisation du cache au runtime
ENTRYPOINT ["/bin/sh", "-c", "squid -Nz && squid -N"]
