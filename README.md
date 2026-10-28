# docker-squid-proxy

Proxy Squid en conteneur pour limiter l'accès internet de services (typiquement des IA) à une liste blanche de domaines, avec une interface web pour voir chaque requête acceptée ou refusée.

![Docker Pulls](https://img.shields.io/github/v/release/veka-server/docker-squid-proxy?label=GHCR)
![Build Docker](https://github.com/veka-server/docker-squid-proxy/actions/workflows/build-central.yml/badge.svg)

```bash
docker pull ghcr.io/veka-server/docker-squid-proxy:latest
```

## Fonctionnalités

- **Whitelist de domaines** (`.domaine.tld` = domaine + sous-domaines) et d'IP.
- **Profils** : un seul conteneur, une whitelist par réseau docker client.
- **Interface web** (port 8080) : requêtes en direct, autorisées/refusées, filtres, top des domaines refusés, thème clair/sombre. Historique SQLite.
- **Rechargement à chaud** : toute modification d'une whitelist est validée (`squid -k parse`) puis appliquée sans redémarrage. Une conf invalide est rejetée et l'ancienne reste active.
- **Pas de rebond entre réseaux** : les réseaux docker sur lesquels le proxy est branché (détectés automatiquement), localhost et 169.254.0.0/16 sont toujours refusés comme destination, même s'ils figurent dans une whitelist. Les IP et noms du LAN restent joignables s'ils sont en whitelist.
- **Durcissement** : pas de résolution DNS des domaines refusés (pas de fuite DNS), IP littérales refusées sauf whitelist explicite, en-têtes `X-Forwarded-For` supprimés, aucun cache. Aucun filtrage par port : une entrée autorisée l'est sur tous les ports (ex. ComfyUI sur 8188).
- Dépendances : `squid`, `su-exec`, `python3` (bibliothèque standard uniquement, aucun paquet pip). Interface en HTML/CSS/JS vanilla.

## Mode simple (un proxy = une whitelist)

Compatible avec l'usage historique :

```yaml
  squid-proxy-dsh:
    image: ghcr.io/veka-server/docker-squid-proxy:latest
    restart: always
    volumes:
      - ./whitelist.txt:/etc/squid/whitelist.txt:ro
      - ./whitelist-ips.txt:/etc/squid/whitelist-ips.txt:ro   # optionnel
      - squid-data:/data
    ports:
      - "8080:8080"   # interface web
    networks:
      - no-internet-dsh
      - internet-dsh
```

> Monter un fichier seul fonctionne, mais certains éditeurs (vim, `sed -i`…) remplacent le fichier au lieu de le modifier, et le conteneur continue de voir l'ancien. Le mode profils (montage d'un dossier) n'a pas ce problème.

## Mode profils (un proxy pour tous les services)

Chaque service est sur son propre réseau interne. Le proxy est branché sur tous ces réseaux et porte, sur chacun, un **alias réseau `squid-profile-<nom>`**, qui relie ce réseau au dossier `profiles/<nom>/` :

```yaml
  squid-proxy:
    networks:
      internet: {}
      ai-dsh:
        aliases: [squid-profile-dsh]      # -> profiles/dsh/
      ai-ollama:
        aliases: [squid-profile-ollama]   # -> profiles/ollama/
```

```
profiles/
├── dsh/
│   └── whitelist.txt
└── ollama/
    ├── whitelist.txt
    └── whitelist-ips.txt   # optionnel
```

Au démarrage (puis toutes les `RELOAD_INTERVAL` secondes), le proxy résout chaque alias via le DNS de docker. Il ne retient que les adresses qui sont **les siennes** et en déduit le sous-réseau du profil. Rien à fixer dans le compose, et un changement de sous-réseau est pris en compte tout seul. Un autre conteneur qui porterait le même alias est ignoré.

Une requête est rattachée à un profil si l'IP source du client est dans le sous-réseau du profil **et** si le client a joint l'IP du proxy sur ce même réseau. L'IP d'un conteneur peut changer sans effet. Pour usurper un autre profil, un client devrait forger une IP source d'un réseau où il n'est pas, et la connexion TCP ne peut alors pas s'établir. Un client sans profil est refusé. Si un dossier de profil existe mais qu'aucun profil n'est valide (alias manquant), **tout est refusé** : il n'y a jamais de repli sur la whitelist legacy.

Pour forcer les sous-réseaux à la main, créez `profiles/<nom>/networks.txt` (un CIDR par ligne). Il remplace alors l'alias.

Exemple complet : [`examples/docker-compose.yml`](examples/docker-compose.yml). Les clients gardent la même URL de proxy (`http://squid-proxy:3128`).

Si vos services sont dans d'autres fichiers compose, déclarez les réseaux dans la stack du proxy et utilisez-les ailleurs avec `external: true` (en reprenant le nom complet du réseau, préfixé par le nom du projet).

### Appels entre conteneurs et `NO_PROXY`

Les appels directs entre conteneurs d'un même réseau ne passent pas par le proxy et fonctionnent normalement. En revanche, avec `HTTP_PROXY`/`HTTPS_PROXY`, beaucoup de clients HTTP envoient **tous** leurs appels au proxy, y compris ceux destinés aux autres conteneurs. Or le proxy refuse toujours une destination située sur ses propres réseaux docker. Excluez donc ces noms :

```yaml
NO_PROXY: localhost,127.0.0.1,ollama,comfyui,.interne
```

Un appel vers un de vos conteneurs qui apparaît en « refusé » dans l'interface signale un nom manquant dans `NO_PROXY`.

Les noms définis par `extra_hosts` sont résolus par le proxy lui-même, pas par le client : ajoutez les mêmes `extra_hosts` au conteneur du proxy, ou utilisez l'IP dans `whitelist-ips.txt`.

## Interface web

`http://<hôte>:8080`

Par défaut (`UI_ACCESS=egress`), l'interface ne répond **que** sur le réseau de sortie du proxy, celui par lequel passent les ports publiés. Les clients filtrés ne peuvent donc pas la consulter. Avec `UI_ACCESS=all` (par exemple derrière un reverse proxy sur un autre réseau), elle répond partout **sauf** depuis les sous-réseaux des profils. Définissez `UI_PASSWORD` dans tous les cas.

Chaque requête est aussi écrite sur la sortie standard (`docker logs`).

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `UI_ENABLED` | `1` | `0` désactive l'interface web et l'historique |
| `UI_PORT` | `8080` | Port de l'interface |
| `UI_ACCESS` | `egress` | `egress` ou `all` (voir ci-dessus) |
| `UI_USERNAME` / `UI_PASSWORD` | `admin` / vide | Authentification Basic (active si un mot de passe est défini) |
| `HISTORY_DAYS` | `30` | Durée de conservation de l'historique |
| `LOG_STDOUT` | `1` | Écrit chaque requête sur stdout |
| `DENY_NETWORKS` | vide | Réseaux supplémentaires toujours interdits (ex. `172.16.0.0/12` pour couvrir tous les réseaux docker, y compris ceux où le proxy n'est pas branché) |
| `BLOCK_PRIVATE_DESTINATIONS` | `0` | `1` : un **domaine** autorisé qui résout vers une IP privée est refusé (protection anti DNS-rebinding). Les IP de `whitelist-ips.txt` restent joignables |
| `RELOAD_INTERVAL` | `10` | Fréquence (s) de vérification des fichiers, des réseaux et des alias |
| `PROFILE_ALIAS_PREFIX` | `squid-profile-` | Préfixe des alias réseau qui désignent les profils |

Directives Squid supplémentaires : déposez des fichiers `*.conf` dans `/etc/squid/conf.d/`.

Pour afficher la configuration générée :

```bash
docker exec squid-proxy cat /etc/squid/squid.conf
```
