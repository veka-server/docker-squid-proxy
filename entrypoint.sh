#!/bin/sh
# Point d'entrée du conteneur docker-squid-proxy.
#
# Deux modes :
#   - legacy  : un seul jeu de whitelists (/etc/squid/whitelist.txt + whitelist-ips.txt)
#               appliqué à tous les clients — comportement historique.
#   - profils : /etc/squid/profiles/<nom>/ contient whitelist.txt et, optionnellement,
#               whitelist-ips.txt et networks.txt. Le profil est choisi selon le réseau
#               par lequel arrive le client (IP source ET IP locale du proxy dans le
#               sous-réseau du profil).
#               Le sous-réseau est détecté automatiquement : donnez au proxy l'alias
#               réseau "squid-profile-<nom>" sur le réseau docker du profil. Le proxy
#               résout cet alias et ne retient que ses PROPRES adresses (un autre
#               conteneur portant le même alias est ignoré). networks.txt, s'il est
#               présent et non vide, force les sous-réseaux à la main.
#
# Syntaxe des whitelists (une entrée par ligne, # = commentaire), tous ports :
#   .exemple.com            domaine + sous-domaines
#   comfyui.lan             un nom du LAN
#   192.168.1.20            (whitelist-ips) IP ou CIDR
#
# Quoi qu'il y ait dans les whitelists, sont TOUJOURS refusés : les réseaux sur
# lesquels le proxy est branché (réseaux docker des clients et de sortie, détectés
# automatiquement), localhost, link-local (169.254/16, métadonnées cloud) et
# DENY_NETWORKS. Un client ne peut donc pas rebondir vers un autre réseau docker
# via le proxy, mais peut joindre une IP du LAN explicitement autorisée.
#
# Requêtes hors whitelist : refusées par défaut. Avec FALLBACK=tor (fichier
# profiles/<nom>/profile.conf, ou variable FALLBACK en mode legacy), elles sont
# envoyées vers le proxy SOCKS de Tor (TOR_SOCKS=hôte:port) via un pont local ;
# la whitelist, elle, sort en direct. Les noms sont résolus par Tor (pas de DNS
# local pour le trafic Tor).
#
# Les fichiers (et les réseaux du conteneur) sont surveillés : toute modification
# régénère la conf, la valide (squid -k parse) puis recharge squid à chaud.
set -eu

PROFILES_DIR="${PROFILES_DIR:-/etc/squid/profiles}"
LEGACY_WL="${LEGACY_WHITELIST:-/etc/squid/whitelist.txt}"
LEGACY_WL_IPS="${LEGACY_WHITELIST_IPS:-/etc/squid/whitelist-ips.txt}"
SQUID_CONF="${SQUID_CONF:-/etc/squid/squid.conf}"
SQUID_USER="${SQUID_USER:-squid}"
UI_USER="${UI_USER:-squidui}"
PID_FILE="${PID_FILE:-/var/lib/squid/squid.pid}"
PROXY_PORT="${PROXY_PORT:-3128}"
DENY_NETWORKS="${DENY_NETWORKS:-}"
BLOCK_PRIVATE="${BLOCK_PRIVATE_DESTINATIONS:-0}"
UI_ENABLED="${UI_ENABLED:-1}"
LOG_UDP_PORT="${LOG_UDP_PORT:-5140}"
RELOAD_INTERVAL="${RELOAD_INTERVAL:-10}"
UI_APP="${UI_APP:-/app/ui.py}"
EXTRA_CONF_DIR="${EXTRA_CONF_DIR:-/etc/squid/conf.d}"
RUN_DIR="${RUN_DIR:-/run/squid-proxy}"
PROFILE_ALIAS_PREFIX="${PROFILE_ALIAS_PREFIX:-squid-profile-}"
LEGACY_FALLBACK="${FALLBACK:-deny}"
TOR_SOCKS="${TOR_SOCKS:-}"
TOR_BRIDGE_PORT="${TOR_BRIDGE_PORT:-3129}"
BRIDGE_APP="${BRIDGE_APP:-/app/socks_bridge.py}"
DNS_GUARD="${DNS_GUARD:-1}"
LOCAL_DOMAINS="${LOCAL_DOMAINS:-}"
DNS_GUARD_APP="${DNS_GUARD_APP:-/app/dns_guard.py}"

log() { echo "[entrypoint] $*" >&2; }

# Vrai si le fichier existe et contient au moins une ligne utile.
has_entries() {
    [ -f "$1" ] && grep -qvE '^[[:space:]]*(#|$)' "$1"
}

# Sur stdin : sortie de "ip -o addr". Résout l'alias $1 et affiche le sous-réseau
# de chaque interface du proxy dont l'adresse correspond exactement.
resolve_alias() {
    python3 -c '
import ipaddress, socket, sys
try:
    ips = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(sys.argv[1], None)}
except (socket.gaierror, UnicodeError):
    ips = set()
for line in sys.stdin:
    f = line.split()
    if len(f) > 3 and f[2] in ("inet", "inet6"):
        itf = ipaddress.ip_interface(f[3])
        if itf.ip in ips:
            print(itf.network)
' "$1"
}

# État des profils, une ligne par profil (sans log) :
#   nom <TAB> invalid|nowhitelist|manual|alias|noalias <TAB> sous-réseaux
profile_state() {
    [ -d "$PROFILES_DIR" ] || return 0
    addrs=$(ip -o addr show 2>/dev/null || true)
    for d in "$PROFILES_DIR"/*/; do
        [ -d "$d" ] || continue
        name=$(basename "$d")
        case "$name" in
            *[!A-Za-z0-9_-]*) printf '%s\tinvalid\t\n' "$name"; continue ;;
        esac
        if [ ! -f "$d/whitelist.txt" ]; then
            printf '%s\tnowhitelist\t\n' "$name"
        elif has_entries "$d/networks.txt"; then
            printf '%s\tmanual\t%s\n' "$name" "$(sed 's/#.*//' "$d/networks.txt" | tr -s ' \t\n' ' ')"
        else
            nets=$(echo "$addrs" | resolve_alias "$PROFILE_ALIAS_PREFIX$name" | tr '\n' ' ')
            if [ -n "$nets" ]; then
                printf '%s\talias\t%s\n' "$name" "$nets"
            else
                printf '%s\tnoalias\t\n' "$name"
            fi
        fi
    done
}

# Mode de repli d'un profil (deny|tor), lu dans profiles/<nom>/profile.conf.
profile_fallback() {
    if [ "$1" = "-" ]; then v="$LEGACY_FALLBACK"
    else v=$(sed -n 's/^[[:space:]]*FALLBACK[[:space:]]*=[[:space:]]*//p' "$PROFILES_DIR/$1/profile.conf" 2>/dev/null | tr -d ' \t"'"'"'\r' | tail -n 1)
    fi
    case "$v" in
        tor) echo tor ;;
        ""|deny) echo deny ;;
        *) log "FALLBACK inconnu ($v) pour ${1#-}, refus appliqué"; echo deny ;;
    esac
}

# Sous-réseaux de toutes les interfaces du conteneur (hors loopback).
proxy_networks() {
    ip -o addr show 2>/dev/null | awk '$2 != "lo" && ($3 == "inet" || $3 == "inet6") && $4 !~ /^fe80/ { print $4 }'
}

# Génère la conf + les fichiers d'ACL dans $2 (dossier neuf). Écrit la conf dans $1.
generate_conf() {
    out="$1"; acl_dir="$2"
    mkdir -p "$acl_dir"
    profiles=""
    state=$(profile_state)
    tab=$(printf '\t')
    while IFS="$tab" read -r name kind nets; do
        [ -n "$name" ] || continue
        case "$kind" in
            invalid)     log "profil ignoré (nom invalide) : $name" ;;
            nowhitelist) log "profil ignoré (whitelist.txt absent) : $name" ;;
            noalias)     log "profil ignoré : $name — l'alias ${PROFILE_ALIAS_PREFIX}$name ne pointe vers aucune IP du proxy (ajoutez-le au proxy sur le réseau du profil, ou créez networks.txt)" ;;
            manual|alias)
                for n in $nets; do echo "$n"; done > "$acl_dir/$name.networks"
                log "profil $name : $(echo $nets) ($( [ "$kind" = alias ] && echo "alias ${PROFILE_ALIAS_PREFIX}$name" || echo networks.txt))"
                profiles="$profiles $name" ;;
        esac
    done <<EOF
$state
EOF
    profiles=$(echo $profiles)
    # Dès qu'un dossier de profil existe, on reste en mode profils, même si aucun
    # profil n'est valide : les clients sans profil sont alors tous refusés
    # (jamais de repli silencieux sur la whitelist legacy).
    if [ -n "$state" ]; then profile_mode=1; else profile_mode=0; fi
    if [ "$profile_mode" = 1 ] && [ -z "$profiles" ]; then
        log "ATTENTION : aucun profil valide, toutes les requêtes seront refusées"
    fi

    # Réseaux toujours interdits comme destination.
    {
        echo "# réseaux du proxy (détectés)"
        proxy_networks
        echo "# toujours interdits"
        echo "127.0.0.0/8"; echo "::1"; echo "0.0.0.0/8"; echo "169.254.0.0/16"; echo "fe80::/10"
        echo "224.0.0.0/4"; echo "ff00::/8"
        if [ -n "$profiles" ]; then
            for p in $profiles; do cat "$acl_dir/$p.networks"; done
        fi
        for n in $DENY_NETWORKS; do echo "$n"; done
    } > "$acl_dir/deny-networks.txt"


    {
        echo "# Généré automatiquement par entrypoint.sh — ne pas éditer à la main."
        echo "http_port $PROXY_PORT"
        echo "pid_filename $PID_FILE"
        echo "max_filedescriptors 4096"
        echo "shutdown_lifetime 3 seconds"
        echo "coredump_dir /tmp"
        echo
        echo "# Proxy de filtrage : aucun cache (inutile pour du HTTPS/CONNECT)."
        echo "cache deny all"
        echo "cache_mem 0 MB"
        echo "# Pas de pinger ICMP : il résout chaque site demandé (fuite DNS, y compris"
        echo "# pour le trafic envoyé à Tor) et ne sert qu'au choix de proxy parent."
        echo "pinger_enable off"
        echo "query_icmp off"
        if [ "$DNS_GUARD" = "1" ]; then
            echo "# Squid ne résout que via le filtre DNS local : seuls les noms en"
            echo "# whitelist sont transmis au résolveur, les autres reçoivent REFUSED."
            echo "dns_nameservers 127.0.0.1"
        fi
        echo
        echo "# Ne pas divulguer les IP internes ni la version de squid."
        echo "visible_hostname squid-proxy"
        echo "forwarded_for delete"
        echo "httpd_suppress_version_string on"
        echo
        echo "# Logs : cache_log via stderr (-d 1), access_log vers l'interface web."
        echo "cache_log /dev/null"
        echo "logfile_rotate 0"
        if [ "$UI_ENABLED" = "1" ]; then
            echo "logformat ui %ts.%03tu %>a %la %{profile}note %Ss %Sh %>Hs %rm %>st %<st %tr %<a %#{User-Agent}>h %#ru"
            echo "access_log udp://127.0.0.1:$LOG_UDP_PORT ui"
        else
            echo "access_log none"
        fi
        echo
        # Directives supplémentaires (optionnel) : /etc/squid/conf.d/*.conf
        for f in "$EXTRA_CONF_DIR"/*.conf; do
            if [ -f "$f" ]; then echo "include $f"; fi
        done
        echo
        echo "# ===== ACL communes ====="
        echo "# Hôte demandé sous forme d'IP littérale (v4 ou v6) : détection sans DNS."
        echo 'acl ip_host dstdom_regex -n ^[0-9.]+$ :'
        echo "acl deny_nets dst \"$acl_dir/deny-networks.txt\""
        echo "# Noms internes (sans point, ou suffixes réservés au réseau local) : jamais"
        echo "# envoyés à Tor ni ailleurs s'ils ne sont pas en whitelist. + LOCAL_DOMAINS."
        echo 'acl local_names dstdom_regex -n -i ^[^.]+$ \.(local|lan|internal|intranet|localdomain|localhost|home|home\.arpa|corp|private|test|invalid)$'
        if [ -n "$LOCAL_DOMAINS" ]; then
            echo "acl local_names_extra dstdomain -n $(echo "$LOCAL_DOMAINS" | tr ',' ' ')"
        fi
        echo "acl to_private dst 10.0.0.0/8 100.64.0.0/10 172.16.0.0/12 192.168.0.0/16 fc00::/7"
        echo

        # Déclare les ACL d'un profil et émet ses règles dans deux fichiers
        # temporaires (règles IP / règles domaines) pour respecter l'ordre global.
        rules_ip="$acl_dir/.rules_ip"; rules_dom="$acl_dir/.rules_dom"
        rules_tor_ip="$acl_dir/.rules_tor_ip"; rules_tor_dom="$acl_dir/.rules_tor_dom"
        rules_route="$acl_dir/.rules_route"
        : > "$rules_ip"; : > "$rules_dom"; : > "$rules_tor_ip"; : > "$rules_tor_dom"; : > "$rules_route"
        tor_used=0
        if [ "$BLOCK_PRIVATE" = "1" ]; then priv=" !to_private"; else priv=""; fi

        if [ "$profile_mode" = 1 ]; then
            echo "# ===== Mode profils ($PROFILES_DIR) ====="
            set -- $profiles
        else
            echo "# ===== Mode legacy (whitelist unique : $LEGACY_WL) ====="
            set -- default
        fi
        for p in "$@"; do
            if [ "$profile_mode" = 0 ]; then
                who=""
                echo "note profile default all"
            else
                nets="$acl_dir/$p.networks"
                # IP source ET IP locale du proxy dans le sous-réseau du profil :
                # usurper un autre profil exigerait de forger une IP source, ce qui
                # empêche toute connexion TCP (les réponses partent ailleurs).
                echo "acl p_${p}_src src \"$nets\""
                echo "acl p_${p}_in localip \"$nets\""
                echo "note profile $p p_${p}_src p_${p}_in"
                who="p_${p}_src p_${p}_in "
            fi
            if [ -z "$who" ]; then wl="$LEGACY_WL"; wl_ips="$LEGACY_WL_IPS"
            else wl="$PROFILES_DIR/$p/whitelist.txt"; wl_ips="$PROFILES_DIR/$p/whitelist-ips.txt"; fi
            not_ip=""; not_dom=""
            if has_entries "$wl_ips"; then
                echo "acl wl_ip_$p dst \"$wl_ips\""
                echo "http_access allow ${who}ip_host wl_ip_$p" >> "$rules_ip"
                not_ip=" !wl_ip_$p"
            fi
            if has_entries "$wl"; then
                echo "acl wl_dom_$p dstdomain -n \"$wl\""
                echo "http_access allow ${who}wl_dom_$p !deny_nets$priv" >> "$rules_dom"
                not_dom=" !wl_dom_$p"
            fi
            if [ "$profile_mode" = 0 ]; then fb=$(profile_fallback -); else fb=$(profile_fallback "$p"); fi
            if [ "$fb" = tor ] && [ -z "$TOR_SOCKS" ]; then
                log "ERREUR : FALLBACK=tor pour ${p} mais TOR_SOCKS n'est pas défini — hors whitelist refusé"
                fb=deny
            fi
            if [ "$fb" = tor ]; then
                tor_used=1
                # Hors whitelist -> Tor. Les ACL utilisées ici sont toutes « rapides »
                # (aucune résolution DNS locale des domaines envoyés à Tor).
                echo "http_access allow ${who}ip_host" >> "$rules_tor_ip"
                if [ -n "$not_dom" ]; then
                    # domaine en whitelist mais refusé à l'étape 4 (réseau interdit) : reste refusé
                    echo "http_access deny ${who}wl_dom_$p" >> "$rules_tor_dom"
                fi
                echo "http_access allow ${who%?}" >> "$rules_tor_dom"
                echo "${who}ip_host$not_ip" >> "$rules_route"
                echo "${who}!ip_host$not_dom" >> "$rules_route"
            fi
        done
        # "allow " seul est invalide en mode legacy (who vide) : on le remplace par all.
        sed -i 's/^http_access allow $/http_access allow all/' "$rules_tor_dom"
        echo
        echo "# 1. IP littérales vers les réseaux interdits (docker, localhost…) : refus sans DNS"
        echo "http_access deny ip_host deny_nets"
        echo "# 2. IP littérales en whitelist"
        cat "$rules_ip"
        if [ -s "$rules_tor_ip" ]; then
            echo "# 2b. Profils FALLBACK=tor : autres IP littérales -> Tor"
            cat "$rules_tor_ip"
        fi
        echo "# 3. Toute autre IP littérale est refusée"
        echo "http_access deny ip_host"
        echo "# 4. Domaines en whitelist. La résolution DNS"
        echo "#    n'a lieu qu'APRÈS le match du domaine : pas de fuite DNS vers les"
        echo "#    domaines refusés. Un domaine qui résout vers un réseau interdit est refusé."
        cat "$rules_dom"
        echo "# 5. Noms internes hors whitelist : refus immédiat (pas de fuite vers Tor)"
        echo "http_access deny local_names"
        if [ -n "$LOCAL_DOMAINS" ]; then echo "http_access deny local_names_extra"; fi
        if [ -s "$rules_tor_dom" ]; then
            echo "# 6. Profils FALLBACK=tor : tout le reste -> Tor"
            cat "$rules_tor_dom"
        fi
        echo "http_access deny all"
        if [ "$tor_used" = 1 ]; then
            echo
            echo "# ===== Routage Tor (pont local HTTP->SOCKS5 vers $TOR_SOCKS) ====="
            echo "cache_peer 127.0.0.1 parent $TOR_BRIDGE_PORT 0 no-query no-digest no-netdb-exchange name=tor"
            # Hors whitelist : uniquement via Tor, jamais de repli en direct.
            sed 's/^/never_direct allow /' "$rules_route"
            sed 's/^/cache_peer_access tor allow /' "$rules_route"
            echo "cache_peer_access tor deny all"
        fi
        rm -f "$rules_ip" "$rules_dom" "$rules_tor_ip" "$rules_tor_dom" "$rules_route"
    } > "$out"

    # Noms autorisés pour le filtre DNS : union des whitelists actives.
    {
        if [ "$profile_mode" = 1 ]; then
            for p in $profiles; do cat "$PROFILES_DIR/$p/whitelist.txt" 2>/dev/null; echo; done
        else
            cat "$LEGACY_WL" 2>/dev/null
        fi
    } > "$acl_dir/dns-allow.txt"

    # Correspondance profil -> fichiers pour l'interface web.
    # nom <TAB> whitelist <TAB> whitelist-ips <TAB> networks <TAB> repli
    {
        if [ "$profile_mode" = 1 ]; then
            for p in $profiles; do
                d="$PROFILES_DIR/$p"
                printf '%s\t%s\t%s\t%s\t%s\n' "$p" "$d/whitelist.txt" "$d/whitelist-ips.txt" "$acl_dir/$p.networks" "$(profile_fallback "$p" 2>/dev/null)"
            done
        else
            printf '%s\t%s\t%s\t%s\t%s\n' default "$LEGACY_WL" "$LEGACY_WL_IPS" - "$(profile_fallback - 2>/dev/null)"
        fi
    } > "$acl_dir/profiles.map"
}

checksum() {
    {
        proxy_networks
        profile_state
        find "$PROFILES_DIR" "$EXTRA_CONF_DIR" -exec ls -ld {} \; 2>/dev/null
        find "$PROFILES_DIR" "$LEGACY_WL" "$LEGACY_WL_IPS" "$EXTRA_CONF_DIR" -type f -exec md5sum {} \; 2>/dev/null
    } | sort | md5sum
}

describe_mode() {
    if [ "$(awk -F'\t' 'NR == 1 { print $4 }' "$RUN_DIR/profiles.map" 2>/dev/null)" != "-" ]; then
        log "mode profils : $(cut -f1 "$RUN_DIR/profiles.map" | tr '\n' ' ')"
    else
        log "mode legacy : $LEGACY_WL"
    fi
    log "réseaux du proxy (interdits comme destination) : $(echo $(proxy_networks))"
}

# Génère une nouvelle conf dans un dossier d'ACL neuf, la valide, puis l'active.
# En cas d'échec, l'ancienne conf et ses fichiers restent intacts.
build() {
    gen="$RUN_DIR/acl-$(date +%s)-$$"
    generate_conf "$SQUID_CONF.new" "$gen"
    if squid -k parse -f "$SQUID_CONF.new" >"$RUN_DIR/parse.log" 2>&1; then
        mv "$SQUID_CONF.new" "$SQUID_CONF"
        cp "$gen/profiles.map" "$RUN_DIR/profiles.map.tmp" && mv "$RUN_DIR/profiles.map.tmp" "$RUN_DIR/profiles.map"
        cp "$gen/dns-allow.txt" "$RUN_DIR/dns-allow.txt.tmp" && chmod 644 "$RUN_DIR/dns-allow.txt.tmp" && mv "$RUN_DIR/dns-allow.txt.tmp" "$RUN_DIR/dns-allow.txt"
        [ -z "${UI_PROFILES_MAP:-}" ] || [ "$UI_PROFILES_MAP" = "$RUN_DIR/profiles.map" ] || cp "$RUN_DIR/profiles.map" "$UI_PROFILES_MAP"
        for d in "$RUN_DIR"/acl-*; do
            [ "$d" = "$gen" ] || rm -rf "$d"
        done
        return 0
    fi
    rm -rf "$gen" "$SQUID_CONF.new"
    grep -iE "error|fatal" "$RUN_DIR/parse.log" >&2 || cat "$RUN_DIR/parse.log" >&2
    return 1
}

watch_loop() {
    last=$(checksum)
    while sleep "$RELOAD_INTERVAL"; do
        cur=$(checksum)
        [ "$cur" = "$last" ] && continue
        last="$cur"
        log "changement détecté, régénération de la configuration"
        if build; then
            describe_mode
            if squid -k reconfigure -f "$SQUID_CONF" >"$RUN_DIR/reconfigure.log" 2>&1; then
                log "squid rechargé"
            else
                log "ÉCHEC du rechargement de squid :"; cat "$RUN_DIR/reconfigure.log" >&2
            fi
        else
            log "configuration invalide, ancienne conf conservée"
        fi
    done
}

dns_guard_loop() {
    while :; do
        # démarre en root pour le port 53, puis abandonne ses privilèges
        DNS_ALLOW_FILE="$RUN_DIR/dns-allow.txt" DNS_GUARD_USER="$UI_USER" python3 "$DNS_GUARD_APP" || true
        log "filtre DNS arrêté, redémarrage dans 1 s"
        sleep 1
    done
}

bridge_loop() {
    while :; do
        su-exec "$UI_USER" python3 "$BRIDGE_APP" || true
        log "pont Tor arrêté, redémarrage dans 2 s"
        sleep 2
    done
}

ui_loop() {
    while :; do
        su-exec "$UI_USER" python3 "$UI_APP" || true
        log "interface web arrêtée, redémarrage dans 2 s"
        sleep 2
    done
}

# ---------------------------------------------------------------------------

mkdir -p "$RUN_DIR"
chmod 755 "$RUN_DIR"

if [ "${1:-}" = "--print-conf" ]; then
    generate_conf /dev/stdout "$RUN_DIR/acl-print"
    exit 0
fi

export UI_PROFILES_MAP="${UI_PROFILES_MAP:-$RUN_DIR/profiles.map}"

if ! build; then
    log "configuration squid invalide, arrêt"
    exit 1
fi
describe_mode

if [ "$DNS_GUARD" = "1" ]; then
    dns_guard_loop &
fi

if [ "$UI_ENABLED" = "1" ]; then
    # Le dossier de la base est souvent un montage (bind mount) créé par docker
    # pour root : on le rend inscriptible par l'utilisateur de l'interface.
    db_path="${DB_PATH:-/data/history.db}"
    db_dir=$(dirname "$db_path")
    mkdir -p "$db_dir"
    for f in "$db_dir" "$db_path" "$db_path-wal" "$db_path-shm"; do
        [ -e "$f" ] || continue
        chown "$UI_USER" "$f" 2>/dev/null || log "ATTENTION : impossible de donner $f à $UI_USER (montage NFS/root_squash ?) — l'historique risque de ne pas fonctionner"
    done
    export LOG_UDP_PORT
    ui_loop &
fi
if [ -n "$TOR_SOCKS" ]; then
    export TOR_SOCKS TOR_BRIDGE_PORT
    bridge_loop &
fi
watch_loop &

# Compatibilité : les images dérivées qui ajoutent des tâches dans
# /etc/crontabs/root continuent de fonctionner.
if command -v crond >/dev/null 2>&1; then crond -l 8 || true; fi

rm -f "$PID_FILE"
exec su-exec "$SQUID_USER" squid -N -d 1 -f "$SQUID_CONF"
