#!/usr/bin/env python3
"""Pont HTTP -> SOCKS5 pour router vers Tor les requêtes hors whitelist.

Squid ne sait pas parler SOCKS : il envoie ces requêtes à ce pont (cache_peer
parent sur 127.0.0.1), qui les transmet au proxy SOCKS de Tor.

  - CONNECT hôte:port  -> tunnel SOCKS5, puis relais brut (HTTPS, etc.)
  - GET http://...     -> connexion SOCKS5 vers hôte:port, requête réécrite
                          en forme "origine", relais de la réponse

Les noms d'hôte sont transmis tels quels à Tor (équivalent socks5h) : la
résolution DNS est faite par Tor, jamais localement. Aucune dépendance.

Variables : TOR_SOCKS=tor:9050, TOR_BRIDGE_PORT=3129, TOR_CONNECT_TIMEOUT=60
"""

import ipaddress
import os
import socket
import struct
import sys
import threading
from urllib.parse import urlsplit

TOR_SOCKS = os.environ.get("TOR_SOCKS", "tor:9050")
LISTEN_PORT = int(os.environ.get("TOR_BRIDGE_PORT", "3129"))
CONNECT_TIMEOUT = float(os.environ.get("TOR_CONNECT_TIMEOUT", "60"))
IDLE_TIMEOUT = 900  # aligné sur read_timeout de squid
MAX_HEADER = 64 * 1024

SOCKS_ERRORS = {
    1: "échec général du serveur SOCKS", 2: "connexion interdite par les règles de Tor",
    3: "réseau injoignable", 4: "hôte injoignable", 5: "connexion refusée",
    6: "TTL expiré (délai dépassé dans Tor)", 7: "commande non supportée", 8: "type d'adresse non supporté",
}
HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer", "upgrade"}


def log(msg):
    print(f"[tor-bridge] {msg}", file=sys.stderr, flush=True)


def split_hostport(value, default_port):
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port = value.rpartition(":")
        if not sep or not port.isdigit():
            host, port = value, ""
    return host, int(port) if port else default_port


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connexion SOCKS fermée")
        buf += chunk
    return buf


def socks5_connect(host, port):
    th, tp = split_hostport(TOR_SOCKS, 9050)
    s = socket.create_connection((th, tp), timeout=CONNECT_TIMEOUT)
    try:
        s.sendall(b"\x05\x01\x00")  # version 5, 1 méthode : sans authentification
        if recv_exact(s, 2) != b"\x05\x00":
            raise ConnectionError("le proxy SOCKS refuse la connexion sans authentification")
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            name = host.encode("idna")
            if len(name) > 255:
                raise ConnectionError("nom d'hôte trop long")
            addr = b"\x03" + bytes([len(name)]) + name  # nom transmis à Tor (pas de DNS local)
        s.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        ver, rep, _, atyp = recv_exact(s, 4)
        if rep != 0:
            raise ConnectionError(SOCKS_ERRORS.get(rep, f"erreur SOCKS {rep}"))
        recv_exact(s, {1: 4, 4: 16}.get(atyp) or recv_exact(s, 1)[0])  # adresse liée
        recv_exact(s, 2)
        s.settimeout(IDLE_TIMEOUT)
        return s
    except Exception:
        s.close()
        raise


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (dst, src):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(a, b):
    t = threading.Thread(target=pipe, args=(b, a), daemon=True)
    t.start()
    pipe(a, b)
    t.join()


def http_error(client, code, reason, detail):
    body = f"{code} {reason} (Tor) : {detail}\n".encode()
    client.sendall(
        f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)


def handle(client):
    upstream = None
    try:
        client.settimeout(60)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                return
            data += chunk
            if len(data) > MAX_HEADER:
                return http_error(client, 431, "Request Header Fields Too Large", "en-têtes trop longs")
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return http_error(client, 400, "Bad Request", "ligne de requête invalide")
        client.settimeout(IDLE_TIMEOUT)

        if method.upper() == "CONNECT":
            host, port = split_hostport(target, 443)
            try:
                upstream = socks5_connect(host, port)
            except Exception as e:
                log(f"CONNECT {host}:{port} échec : {e}")
                return http_error(client, 502, "Bad Gateway", str(e))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            relay(client, upstream)
            return

        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            return http_error(client, 400, "Bad Request", "URL absolue http:// attendue")
        host, port = u.hostname, u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        headers = [line for line in lines[1:] if line.split(":", 1)[0].strip().lower() not in HOP_BY_HOP]
        headers.append("Connection: close")
        try:
            upstream = socks5_connect(host, port)
        except Exception as e:
            log(f"{method} {host}:{port} échec : {e}")
            return http_error(client, 502, "Bad Gateway", str(e))
        upstream.sendall(f"{method} {path} {version}\r\n".encode("iso-8859-1")
                         + "\r\n".join(headers).encode("iso-8859-1") + b"\r\n\r\n" + rest)
        relay(client, upstream)
    except Exception as e:
        log(f"erreur : {e}")
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))  # jamais exposé : seul squid s'y connecte
    srv.listen(256)
    log(f"pont HTTP->SOCKS5 sur 127.0.0.1:{LISTEN_PORT} vers {TOR_SOCKS}")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
