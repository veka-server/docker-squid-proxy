#!/usr/bin/env python3
"""Filtre DNS local pour squid : seuls les noms en whitelist sont résolus.

Squid résout parfois des noms qu'il ne contactera jamais lui-même (ex. le
"netdb ping" ICMP résout chaque site, même quand la requête part vers Tor).
Ce filtre écoute sur 127.0.0.1:53 (UDP + TCP) et :
  - transmet au résolveur d'origine (/etc/resolv.conf, ou DNS_UPSTREAM)
    les requêtes dont le nom correspond à une whitelist active ;
  - répond REFUSED à toutes les autres, sans rien émettre sur le réseau.

La liste des noms autorisés est lue dans DNS_ALLOW_FILE (générée par
l'entrypoint) et rechargée automatiquement. Aucune dépendance.
Le processus démarre en root pour écouter sur le port 53, puis abandonne
ses privilèges (DNS_GUARD_USER).
"""

import os
import pwd
import socket
import struct
import sys
import threading
import time

ALLOW_FILE = os.environ.get("DNS_ALLOW_FILE", "/run/squid-proxy/dns-allow.txt")
LISTEN = os.environ.get("DNS_GUARD_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("DNS_GUARD_PORT", "53"))
RUN_AS = os.environ.get("DNS_GUARD_USER", "squidui")
TIMEOUT = 5.0


def log(msg):
    print(f"[dns-guard] {msg}", file=sys.stderr, flush=True)


def upstreams():
    env = os.environ.get("DNS_UPSTREAM", "").replace(",", " ").split()
    if env:
        return env
    out = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == "nameserver" and p[1] not in (LISTEN,):
                    out.append(p[1])
    except OSError:
        pass
    return out or ["127.0.0.11"]


UPSTREAMS = upstreams()


class Allow:
    def __init__(self):
        self.mtime = None
        self.exact, self.suffix = set(), set()
        self.lock = threading.Lock()

    def refresh(self):
        try:
            m = os.stat(ALLOW_FILE).st_mtime
        except OSError:
            m = None
        if m == self.mtime:
            return
        exact, suffix = set(), set()
        try:
            with open(ALLOW_FILE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    for e in line.split("#", 1)[0].split():
                        e = e.lower().rstrip(".")
                        if e.startswith("."):
                            suffix.add(e[1:])
                        elif e:
                            exact.add(e)
        except OSError:
            pass
        with self.lock:
            self.exact, self.suffix, self.mtime = exact, suffix, m

    def ok(self, name):
        name = name.lower().rstrip(".")
        with self.lock:
            if name in self.exact or name in self.suffix:
                return True
            parts = name.split(".")
            return any(".".join(parts[i:]) in self.suffix for i in range(1, len(parts)))


ALLOW = Allow()


def qname(msg):
    """Nom de la première question (sans gestion de la compression : inutile ici)."""
    if len(msg) < 12 or struct.unpack(">H", msg[4:6])[0] < 1:
        return None
    i, labels = 12, []
    while i < len(msg):
        n = msg[i]
        if n == 0:
            return ".".join(labels)
        if n & 0xC0:
            return None
        labels.append(msg[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    return None


def refused(msg):
    # Même ID et même question, QR=1, RCODE=5 (REFUSED), aucune réponse.
    end = 12
    while end < len(msg) and msg[end] != 0:
        end += 1 + msg[end]
    end += 5
    flags = struct.unpack(">H", msg[2:4])[0]
    flags = 0x8000 | (flags & 0x0100) | 0x0080 | 5  # QR, RD recopié, RA, REFUSED
    return msg[:2] + struct.pack(">HHHHH", flags, 1, 0, 0, 0) + msg[12:end]


def decide(msg):
    ALLOW.refresh()
    name = qname(msg)
    return name, bool(name) and ALLOW.ok(name)


def forward_udp(msg):
    for up in UPSTREAMS:
        try:
            s = socket.socket(socket.AF_INET6 if ":" in up else socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(TIMEOUT)
            s.sendto(msg, (up, 53))
            data, _ = s.recvfrom(65535)
            s.close()
            if data[:2] == msg[:2]:
                return data
        except OSError:
            continue
    return None


def forward_tcp(msg):
    for up in UPSTREAMS:
        try:
            with socket.create_connection((up, 53), timeout=TIMEOUT) as s:
                s.sendall(struct.pack(">H", len(msg)) + msg)
                n = struct.unpack(">H", recv_exact(s, 2))[0]
                return recv_exact(s, n)
        except (OSError, ConnectionError):
            continue
    return None


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("fermé")
        buf += c
    return buf


def serve_udp(sock):
    while True:
        msg, addr = sock.recvfrom(65535)
        threading.Thread(target=handle_udp, args=(sock, msg, addr), daemon=True).start()


def handle_udp(sock, msg, addr):
    name, ok = decide(msg)
    if name is None:
        return
    if not ok:
        sock.sendto(refused(msg), addr)
        return
    resp = forward_udp(msg)
    if resp:
        sock.sendto(resp, addr)


def serve_tcp(sock):
    while True:
        c, _ = sock.accept()
        threading.Thread(target=handle_tcp, args=(c,), daemon=True).start()


def handle_tcp(c):
    try:
        c.settimeout(TIMEOUT)
        n = struct.unpack(">H", recv_exact(c, 2))[0]
        msg = recv_exact(c, n)
        name, ok = decide(msg)
        if name is None:
            return
        resp = forward_tcp(msg) if ok else refused(msg)
        if resp:
            c.sendall(struct.pack(">H", len(resp)) + resp)
    except (OSError, ConnectionError):
        pass
    finally:
        c.close()


def main():
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind((LISTEN, PORT))
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t.bind((LISTEN, PORT))
    t.listen(64)
    if os.getuid() == 0 and RUN_AS:
        pw = pwd.getpwnam(RUN_AS)
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
    ALLOW.refresh()
    log(f"filtre DNS sur {LISTEN}:{PORT} -> {', '.join(UPSTREAMS)} "
        f"({len(ALLOW.exact) + len(ALLOW.suffix)} entrées autorisées)")
    threading.Thread(target=serve_tcp, args=(t,), daemon=True).start()
    serve_udp(u)


if __name__ == "__main__":
    main()
