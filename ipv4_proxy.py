"""IPv4-only CONNECT bridge. All outbound sockets go to a loopback proxy.

No application TLS interception, request replay, direct or IPv6 fallback.
The DNS bootstrap name dns.google is resolved by the pinned remote proxy.
"""
import concurrent.futures
import http.client
import ipaddress
import json
import random
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

import google_ipv6_proxy as base  # Only CONNECT framing and server primitives.


def public_ipv4(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OSError('Expected an IPv4 literal') from exc
    if ip.version != 4 or not ip.is_global:
        raise OSError('IPv6 and non-public destinations are prohibited')
    return str(ip)


def connect_ipv4(port, ip, target_port):
    return base.upstream_connect(port, public_ipv4(ip), target_port)


class Resolver:
    def __init__(self, upstream_port):
        self.port = upstream_port
        self.cache = {}
        self.lock = threading.Lock()

    def query(self, host):
        conn = http.client.HTTPSConnection('127.0.0.1', self.port, timeout=12,
                                           context=ssl.create_default_context())
        conn.set_tunnel('dns.google', 443)
        try:
            conn.request('GET', '/resolve?' + urlencode({'name': host, 'type': 'A'}),
                         headers={'Accept': 'application/dns-json'})
            response = conn.getresponse()
            if response.status != 200:
                raise OSError('Proxied DNS failed')
            payload = response.read(1048577)
            if len(payload) > 1048576:
                raise OSError('DNS response too large')
            document = json.loads(payload)
        finally:
            conn.close()
        if document.get('Status') != 0:
            raise OSError('DNS response reported failure')
        records = [a for a in document.get('Answer', []) if a.get('type') == 1]
        if not records:
            raise OSError('No A records; IPv6 fallback is prohibited')
        ips = list(dict.fromkeys(public_ipv4(a['data']) for a in records))
        return ips, max(0, min(60, *(int(a.get('TTL', 0)) for a in records)))

    def healthy(self, host, ip, port):
        raw = None
        try:
            raw = connect_ipv4(self.port, ip, port)
            raw.settimeout(7)
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
                return True
        except (OSError, ValueError):
            return False
        finally:
            if raw:
                raw.close()

    def addresses(self, host, port):
        host = host.lower().rstrip('.')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not host or len(host) > 253 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host):
                raise OSError('Invalid destination')
        else:
            return [public_ipv4(host)]
        key = (host, port)
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])
            started = time.monotonic()
            ips, ttl = self.query(host)
            if host == 'googleapis.com' or host.endswith('.googleapis.com'):
                if port != 443:
                    raise OSError('Google API tunnel requires TLS port 443')
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    checks = list(pool.map(lambda ip: self.healthy(host, ip, port), ips))
                ips = [ip for ip, ok in zip(ips, checks) if ok]
            if not ips:
                raise OSError('No healthy IPv4 target; refusing fallback')
            self.cache[key] = (started + ttl, tuple(ips))
            return ips

    def invalidate(self, host, port):
        with self.lock:
            self.cache.pop((host.lower().rstrip('.'), port), None)


class ExitMismatch(OSError):
    """A response actually identifies an unexpected exit, rather than timing out."""


def require_exit(expected, observed):
    try:
        valid = len(observed) >= 2 and all(public_ipv4(ip.strip()) == expected for ip in observed)
    except OSError as exc:
        raise ExitMismatch('Exit response is not a permitted IPv4 address') from exc
    if not valid:
        raise ExitMismatch('Exit verification failed; refusing a different exit')


def https_get(resolver, host, path='/'):
    last = None
    for ip in resolver.addresses(host, 443):
        raw = None
        try:
            raw = connect_ipv4(resolver.port, ip, 443)
            raw.settimeout(12)
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: ' + host +
                             '\r\nConnection: close\r\n\r\n').encode('ascii'))
                response = http.client.HTTPResponse(tls)
                response.begin()
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise OSError('Exit check returned an invalid HTTP response')
                return body.decode('utf-8').strip()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last = exc
        finally:
            if raw:
                raw.close()
    raise OSError('IPv4 check failed') from last


def check_exit(resolver, expected):
    first = https_get(resolver, 'api64.ipify.org')
    if first.strip() != expected:
        raise ExitMismatch('First exit service returned an unexpected address')
    trace = https_get(resolver, 'www.cloudflare.com', '/cdn-cgi/trace')
    second = next((line[3:] for line in trace.splitlines() if line.startswith('ip=')), '')
    require_exit(expected, [first, second])
    return [first, second]


class Server(base.Server):
    def __init__(self, address, upstream_port):
        super().__init__(address, Handler)
        self.upstream_port = upstream_port
        self.resolver = Resolver(upstream_port)
        self.slots = threading.BoundedSemaphore(64)
        self.active = set()
        self.active_lock = threading.Lock()
        self.verified_at = 0
        self.failed = False
        self.paused = False

    def valid(self):
        return not self.paused and self.tunnels_valid()

    def tunnels_valid(self):
        return not self.failed and time.monotonic() - self.verified_at < 150

    def close_tunnels(self):
        self.failed = True
        with self.active_lock:
            for sock in list(self.active):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


class ExitMonitor:
    """Keep the listener alive during outages; never accept an unverified new exit."""
    def __init__(self, server, expected, probe=None):
        self.server = server
        self.expected = expected
        self.probe = probe or check_exit
        self.next_check = server.verified_at + 45
        self.failures = 0

    def poll(self, force=False):
        if not force and time.monotonic() < self.next_check:
            return None
        try:
            observed = self.probe(self.server.resolver, self.expected)
            require_exit(self.expected, observed)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self.failures += 1
            self.server.paused = True
            # Existing TCP tunnels cannot change their source address mid-connection.
            # Keep them only within the existing 150-second verification window.
            if isinstance(exc, ExitMismatch) or not self.server.tunnels_valid():
                self.server.close_tunnels()
            delay = min(30, 5 * (2 ** min(self.failures - 1, 3)))
            self.next_check = time.monotonic() + delay
            return {'state': 'paused', 'reason': str(exc), 'retry_seconds': delay}
        with self.server.active_lock:
            self.server.verified_at = time.monotonic()
            self.server.failed = False
            self.server.paused = False
        recovered = self.failures > 0
        self.failures = 0
        self.next_check = self.server.verified_at + 45
        return {'state': 'active', 'observed': observed, 'recovered': recovered}


class Handler(base.socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        established = False
        if not self.server.slots.acquire(blocking=False):
            return
        self.request.settimeout(15)
        try:
            head = base.read_head(self.request)
            if not self.server.valid():
                raise OSError('Exit not verified')
            method, authority, version = head.split(b'\r\n', 1)[0].decode('ascii').split()
            if method != 'CONNECT':
                raise OSError('CONNECT only')
            host, port = base.parse_authority(authority)
            candidates = self.server.resolver.addresses(host, port)
            random.shuffle(candidates)
            for ip in candidates:
                try:
                    upstream = connect_ipv4(self.server.upstream_port, ip, port)
                    break
                except OSError:
                    continue
            if upstream is None:
                self.server.resolver.invalidate(host, port)
                raise OSError('All IPv4 connections failed')
            with self.server.active_lock:
                if not self.server.valid():
                    raise OSError('Exit verification expired')
                self.server.active.update((self.request, upstream))
            self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            established = True
            self.request.settimeout(None)
            active = [self.request, upstream]
            while active and self.server.tunnels_valid():
                ready, _, _ = select.select(active, [], [], 2)
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
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            if not established:
                try:
                    self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
        finally:
            with self.server.active_lock:
                self.server.active.discard(self.request)
                self.server.active.discard(upstream)
            if upstream:
                upstream.close()
            self.server.slots.release()
