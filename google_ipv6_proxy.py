"""Loopback CONNECT proxy: dynamic proxied DoH for Google APIs, no direct fallback.

Every outbound socket connects only to the configured loopback upstream proxy.
TLS is passed through unchanged; this program never decrypts application traffic.
"""
import argparse
import http.client
import ipaddress
import json
import random
import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import urlencode


def read_head(sock):
    data = bytearray()
    while not data.endswith(b'\r\n\r\n'):
        b = sock.recv(1)
        if not b:
            raise OSError('Upstream closed before CONNECT response')
        data += b
        if len(data) > 16384:
            raise OSError('Header too large')
    return bytes(data)


def parse_authority(value):
    if value.startswith('['):
        end = value.index(']')
        if value[end + 1:end + 2] != ':':
            raise ValueError('Missing port')
        host, port = value[1:end], int(value[end + 2:])
    else:
        host, port = value.rsplit(':', 1)
        port = int(port)
    if not host or any(c.isspace() for c in host) or not 1 <= port <= 65535:
        raise ValueError('Invalid authority')
    return host, port


class Resolver:
    def __init__(self, upstream_port):
        self.port = upstream_port
        self.cache = {}
        self.lock = threading.Lock()

    def addresses(self, host):
        host = host.lower().rstrip('.')
        with self.lock:
            cached = self.cache.get(host)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
        # HTTPSConnection first CONNECTs via the loopback proxy, then validates
        # dns.google's TLS certificate. There is no system DNS or direct socket.
        conn = http.client.HTTPSConnection('127.0.0.1', self.port, timeout=12,
                                           context=ssl.create_default_context())
        conn.set_tunnel('dns.google', 443)
        try:
            conn.request('GET', '/resolve?' + urlencode({'name': host, 'type': 'AAAA'}),
                         headers={'Accept': 'application/dns-json'})
            response = conn.getresponse()
            if response.status != 200:
                raise OSError('Proxied DoH returned HTTP ' + str(response.status))
            payload = response.read(1048577)
            if len(payload) > 1048576:
                raise OSError('DoH response too large')
            document = json.loads(payload)
        finally:
            conn.close()
        if document.get('Status') != 0:
            raise OSError('Proxied DNS query failed')
        records = [a for a in document.get('Answer', []) if a.get('type') == 28]
        ips = []
        for record in records:
            ip = ipaddress.ip_address(record['data'])
            if ip.version == 6 and ip.is_global:
                ips.append(str(ip))
        if not ips:
            raise OSError('No global AAAA record; refusing direct/IPv4 fallback')
        ttl = max(0, min(300, *(int(a.get('TTL', 0)) for a in records)))
        with self.lock:
            self.cache[host] = (time.monotonic() + ttl, tuple(ips))
        return ips


def upstream_connect(port, host, target_port):
    sock = socket.create_connection(('127.0.0.1', port), timeout=12)
    try:
        authority = f'[{host}]:{target_port}' if ':' in host else f'{host}:{target_port}'
        sock.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'))
        if b' 200 ' not in read_head(sock).split(b'\r\n', 1)[0]:
            raise OSError('Upstream proxy refused CONNECT')
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        established = False
        self.request.settimeout(15)
        if not self.server.slots.acquire(blocking=False):
            self.request.sendall(b'HTTP/1.1 503 Busy\r\nContent-Length: 0\r\n\r\n')
            return
        try:
            head = read_head(self.request)
            method, authority, version = head.split(b'\r\n', 1)[0].decode('ascii').split()
            if method != 'CONNECT':
                self.request.sendall(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
                return
            host, port = parse_authority(authority)
            normalized = host.lower().rstrip('.')
            if normalized == 'googleapis.com' or normalized.endswith('.googleapis.com'):
                host = random.choice(self.server.resolver.addresses(normalized))
            upstream = upstream_connect(self.server.upstream_port, host, port)
            self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            established = True
            self.request.settimeout(None)
            # Relay only. Neither application headers nor TLS contents are logged.
            active = [self.request, upstream]
            while active:
                ready, _, _ = select.select(active, [], [], 60)
                for source in ready:
                    dest = upstream if source is self.request else self.request
                    chunk = source.recv(65536)
                    if chunk:
                        dest.sendall(chunk)
                    else:
                        active.remove(source)
                        try:
                            dest.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        except (OSError, ValueError, KeyError):
            if not established:
                try:
                    self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()
            self.server.slots.release()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def main():
    raise SystemExit('Legacy IPv6 entry disabled. Use Start-Antigravity-IPv4.ps1.')


if __name__ == '__main__':
    main()
