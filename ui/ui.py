#!/usr/bin/env python3
"""Interface web de docker-squid-proxy.

Un seul processus, bibliothèque standard Python uniquement :
  - reçoit les logs d'accès de squid en UDP local (127.0.0.1) ;
  - les stocke dans SQLite (WAL, purge automatique) ;
  - sert une petite API JSON et une interface statique (HTML/CSS/JS vanilla).

Variables d'environnement :
  UI_PORT=8080            port HTTP de l'interface
  UI_ACCESS=egress        egress : l'UI ne répond que sur le réseau de sortie du
                          proxy (celui des ports publiés), jamais depuis les réseaux
                          des clients. all : répond sur toutes les interfaces
                          (sauf depuis les sous-réseaux des profils).
  UI_PASSWORD=            si défini, authentification HTTP Basic obligatoire
  UI_USERNAME=admin
  DB_PATH=/data/history.db
  HISTORY_DAYS=30         durée de conservation de l'historique
  LOG_STDOUT=1            réécrit chaque requête sur stdout (docker logs)
  LOG_UDP_PORT=5140
"""

import base64
import hmac
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

UI_PORT = int(os.environ.get("UI_PORT", "8080"))
UI_ACCESS = os.environ.get("UI_ACCESS", "egress").strip().lower()
UI_USERNAME = os.environ.get("UI_USERNAME", "admin")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
DB_PATH = os.environ.get("DB_PATH", "/data/history.db")
HISTORY_DAYS = float(os.environ.get("HISTORY_DAYS", "30"))
LOG_STDOUT = os.environ.get("LOG_STDOUT", "1") == "1"
LOG_UDP_PORT = int(os.environ.get("LOG_UDP_PORT", "5140"))
PROFILES_MAP = os.environ.get("UI_PROFILES_MAP", "/run/squid-proxy/profiles.map")
PID_FILE = os.environ.get("PID_FILE", "/var/lib/squid/squid.pid")

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    profile     TEXT    NOT NULL,
    client      TEXT    NOT NULL,
    method      TEXT    NOT NULL,
    host        TEXT    NOT NULL,
    port        INTEGER,
    url         TEXT    NOT NULL,
    decision    TEXT    NOT NULL,      -- 'allowed' | 'denied'
    squid_status TEXT   NOT NULL,      -- ex. TCP_TUNNEL, TCP_DENIED
    http_status INTEGER,
    bytes_in    INTEGER,
    bytes_out   INTEGER,
    duration_ms INTEGER,
    server_ip   TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_req_decision_ts ON requests(decision, ts);
CREATE INDEX IF NOT EXISTS idx_req_profile_ts ON requests(profile, ts);
CREATE INDEX IF NOT EXISTS idx_req_host ON requests(host);
"""


def log(msg):
    print(f"[ui] {msg}", file=sys.stderr, flush=True)


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # auto_vacuum doit être fixé avant la création des tables.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Profils (lus depuis la table générée par l'entrypoint)
# --------------------------------------------------------------------------

def read_list(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.extend(line.split())
    except OSError:
        pass
    return out


def load_profiles():
    profiles = []
    try:
        with open(PROFILES_MAP, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                name, wl, wl_ips, nets = parts
                profiles.append({
                    "name": name,
                    "networks": read_list(nets) if nets != "-" else [],
                    "domains": read_list(wl),
                    "ips": read_list(wl_ips),
                })
    except OSError:
        pass
    return profiles


def profile_networks():
    nets = []
    for p in load_profiles():
        for n in p["networks"]:
            try:
                nets.append(ipaddress.ip_network(n, strict=False))
            except ValueError:
                pass
    return nets


# --------------------------------------------------------------------------
# Collecteur : squid -> UDP -> SQLite
# --------------------------------------------------------------------------

def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_line(line):
    # logformat ui : ts client local profile squid_status http_status method
    #                req_bytes resp_bytes duration server_ip ua(urlencoded) url(urlencoded)
    f = line.strip().split(" ")
    if len(f) != 13:
        return None
    ts, client, _local, profile, sstatus, hstatus, method, b_in, b_out, dur, server, ua, url = f
    url = unquote(url)
    if url.startswith("error:") or method == "-":
        return None  # connexions fermées avant toute requête (health checks, scans)
    host, port = "", None
    if method == "CONNECT":
        h, _, p = url.rpartition(":")
        host, port = (h or url), to_int(p)
    else:
        try:
            u = urlsplit(url)
            host, port = u.hostname or "", u.port
        except ValueError:
            host = url
    host = host.strip("[]").lower()
    ua = unquote(ua)
    return (
        float(ts), profile if profile != "-" else "", client, method, host, port, url,
        "denied" if "DENIED" in sstatus else "allowed",
        sstatus.split("/")[0], to_int(hstatus), to_int(b_in), to_int(b_out), to_int(dur),
        server if server != "-" else "", ua if ua != "-" else "",
    )


def collector():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("127.0.0.1", LOG_UDP_PORT))
    sock.settimeout(0.5)
    conn = db_connect()
    pending, last_commit, last_purge = [], time.time(), 0.0
    log(f"collecteur UDP sur 127.0.0.1:{LOG_UDP_PORT}, base {DB_PATH}")
    while True:
        try:
            data = sock.recv(65535)
            for line in data.decode("utf-8", "replace").splitlines():
                row = parse_line(line)
                if row:
                    pending.append(row)
                    if LOG_STDOUT:
                        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(row[0]))
                        print(f"{t} {row[7].upper():7} [{row[1] or '-'}] {row[2]} {row[3]} {row[6]} {row[9] or '-'}",
                              flush=True)
        except socket.timeout:
            pass
        except Exception as e:  # ne jamais tuer le collecteur sur une ligne bizarre
            log(f"erreur collecteur : {e}")
        now = time.time()
        if pending and (now - last_commit > 0.5 or len(pending) > 500):
            try:
                conn.executemany(
                    "INSERT INTO requests(ts,profile,client,method,host,port,url,decision,"
                    "squid_status,http_status,bytes_in,bytes_out,duration_ms,server_ip,user_agent)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pending)
                conn.commit()
                pending = []
            except sqlite3.Error as e:
                log(f"erreur SQLite : {e}")
            last_commit = now
        if now - last_purge > 3600:
            last_purge = now
            try:
                cur = conn.execute("DELETE FROM requests WHERE ts < ?", (now - HISTORY_DAYS * 86400,))
                conn.commit()
                if cur.rowcount:
                    conn.execute("PRAGMA incremental_vacuum")
                    log(f"purge : {cur.rowcount} lignes de plus de {HISTORY_DAYS:g} jours")
            except sqlite3.Error as e:
                log(f"erreur purge : {e}")


# --------------------------------------------------------------------------
# Contrôle d'accès à l'interface
# --------------------------------------------------------------------------

def egress_addresses():
    """IP locales portant la route par défaut (réseau des ports publiés)."""
    addrs = set()
    for fam, target in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            s = socket.socket(fam, socket.SOCK_DGRAM)
            s.connect((target, 9))  # aucun paquet n'est émis
            addrs.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return addrs


EGRESS = egress_addresses()


def norm_ip(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        return a
    except ValueError:
        return None


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}


def build_filters(qs, with_decision=True):
    where, args = [], []
    rng = RANGES.get(qs.get("range", ["24h"])[0], 86400)
    where.append("ts >= ?")
    args.append(time.time() - rng)
    profile = qs.get("profile", [""])[0]
    if profile:
        where.append("profile = ?")
        args.append(profile if profile != "(aucun)" else "")
    if with_decision:
        decision = qs.get("decision", [""])[0]
        if decision in ("allowed", "denied"):
            where.append("decision = ?")
            args.append(decision)
    q = qs.get("q", [""])[0].strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(host LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' OR client LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    return where, args, rng


def api_requests(conn, qs):
    where, args, _ = build_filters(qs)
    before = to_int(qs.get("before_id", [None])[0])
    after = to_int(qs.get("after_id", [None])[0])
    if before:
        where.append("id < ?")
        args.append(before)
    if after:
        where.append("id > ?")
        args.append(after)
    limit = max(1, min(to_int(qs.get("limit", ["100"])[0]) or 100, 500))
    rows = conn.execute(
        f"SELECT * FROM requests WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        args + [limit]).fetchall()
    return {"items": [dict(r) for r in rows]}


def api_stats(conn, qs):
    where, args, rng = build_filters(qs, with_decision=False)
    w = " AND ".join(where)
    tot = conn.execute(
        f"SELECT COUNT(*) n, SUM(decision='allowed') ok, SUM(decision='denied') ko,"
        f" COUNT(DISTINCT host) hosts FROM requests WHERE {w}", args).fetchone()
    nb = 48
    now = time.time()
    size = rng / nb
    start = now - rng
    buckets = [{"t": start + i * size, "allowed": 0, "denied": 0} for i in range(nb)]
    for r in conn.execute(
            f"SELECT CAST((ts - ?) / ? AS INTEGER) b, decision, COUNT(*) n FROM requests"
            f" WHERE {w} GROUP BY b, decision", [start, size] + args):
        if 0 <= r["b"] < nb:
            buckets[r["b"]][r["decision"]] += r["n"]
    top_denied = [dict(r) for r in conn.execute(
        f"SELECT host, COUNT(*) n, MAX(ts) last, GROUP_CONCAT(DISTINCT NULLIF(profile, '')) profiles"
        f" FROM requests WHERE {w} AND decision='denied' GROUP BY host ORDER BY n DESC LIMIT 15", args)]
    top_allowed = [dict(r) for r in conn.execute(
        f"SELECT host, COUNT(*) n, MAX(ts) last FROM requests WHERE {w} AND decision='allowed'"
        f" GROUP BY host ORDER BY n DESC LIMIT 15", args)]
    return {
        "total": tot["n"] or 0, "allowed": tot["ok"] or 0, "denied": tot["ko"] or 0,
        "hosts": tot["hosts"] or 0, "bucket_seconds": size, "timeline": buckets,
        "top_denied": top_denied, "top_allowed": top_allowed,
    }


def api_profiles(conn):
    profiles = load_profiles()
    since = time.time() - 86400
    counts = {r["profile"]: dict(r) for r in conn.execute(
        "SELECT profile, COUNT(*) n, SUM(decision='denied') denied FROM requests"
        " WHERE ts >= ? GROUP BY profile", (since,))}
    for p in profiles:
        c = counts.get(p["name"], {})
        p["requests_24h"] = c.get("n", 0)
        p["denied_24h"] = c.get("denied", 0) or 0
    return {"profiles": profiles, "unmatched_24h": counts.get("", {}).get("n", 0)}


def squid_alive():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        return os.path.exists(f"/proc/{pid}")
    except (OSError, ValueError):
        return False


_local = threading.local()


def thread_db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = db_connect()
    return conn


class Handler(BaseHTTPRequestHandler):
    server_version = "squid-proxy-ui"
    sys_version = ""

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def allowed(self):
        local = norm_ip(self.connection.getsockname()[0])
        client = norm_ip(self.client_address[0])
        if local is None or client is None:
            return False
        if local.is_loopback:
            return True
        # Jamais depuis le réseau d'un client du proxy.
        if any(client in n for n in profile_networks() if n.version == client.version):
            return False
        if UI_ACCESS == "all":
            return True
        return str(local) in {str(norm_ip(a)) for a in EGRESS}

    def authorized(self):
        if not UI_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user.encode(), UI_USERNAME.encode()) & \
            hmac.compare_digest(pwd.encode(), UI_PASSWORD.encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            ok = squid_alive()
            return self.send(200 if ok else 503, {"squid": ok})
        if not self.allowed():
            return self.send(403, {"error": "interface non accessible depuis ce réseau"})
        if not self.authorized():
            return self.send(401, {"error": "authentification requise"},
                             extra={"WWW-Authenticate": 'Basic realm="squid-proxy"'})
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    return self.send(200, f.read(), ctype)
            except OSError:
                return self.send(404, {"error": "introuvable"})
        qs = parse_qs(query)
        try:
            conn = thread_db()
            if path == "/api/requests":
                return self.send(200, api_requests(conn, qs))
            if path == "/api/stats":
                return self.send(200, api_stats(conn, qs))
            if path == "/api/profiles":
                return self.send(200, api_profiles(conn))
        except sqlite3.Error as e:
            return self.send(500, {"error": str(e)})
        return self.send(404, {"error": "introuvable"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        # Écoute double pile (IPv4 + IPv6) si possible.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def main():
    db_init()
    threading.Thread(target=collector, daemon=True, name="collector").start()
    try:
        srv = Server(("::", UI_PORT), Handler)
    except OSError:
        Server.address_family = socket.AF_INET
        srv = Server(("0.0.0.0", UI_PORT), Handler)
    mode = "toutes interfaces" if UI_ACCESS == "all" else f"réseau de sortie {sorted(EGRESS) or '?'}"
    log(f"interface web sur le port {UI_PORT} ({mode}, auth {'activée' if UI_PASSWORD else 'désactivée'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
