#!/usr/bin/env python3
"""衣姿 PoseWear 自托管服务（Python 3.10 标准库）。

提供 dist/ 静态页，以及与 functions/api/proxy.js 相同边界的同源 /api/proxy：
只转发官方图片接口或带 x-proxy-custom 的自定义中转，拒绝内网与非公网地址，
按 IP 做频率限制。不记录密钥、请求体或图片。

本程序只监听 POSEWEAR_PORT（默认 80）。端口 443 直接拒绝，不会绑定，也不会
改写该端口上已有的程序。
"""
from __future__ import annotations

import gzip
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 与 functions/api/proxy.js 保持同一组白名单与路径限制。
API_PATH = re.compile(r'^/(api/v3|v\d+(beta|alpha)?)/(images/(edits|generations)|models(/[^/]+(:generateContent)?)?)$')
CUSTOM_PATH = re.compile(r'/(images/(edits|generations)|models(/[^/]+(:generateContent)?)?)$')
OFFICIAL = [
    re.compile(r'^api\.openai\.com$'),
    re.compile(r'^generativelanguage\.googleapis\.com$'),
    re.compile(r'^ark\.[a-z0-9-]+\.volces\.com$'),
    re.compile(r'^ark\.[a-z0-9-]+\.bytepluses\.com$'),
]
IMG_HOSTS = [
    re.compile(r'^oaidalleapiprodscus\.blob\.core\.windows\.net$'),
    re.compile(r'(^|\.)volces\.com$'),
    re.compile(r'(^|\.)bytepluses\.com$'),
    re.compile(r'(^|\.)googleusercontent\.com$'),
]
DROP = {
    'host', 'x-target-url', 'x-proxy-kind', 'x-proxy-custom', 'cookie', 'origin',
    'referer', 'x-forwarded-for', 'x-forwarded-proto', 'x-real-ip', 'content-length',
    'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest', 'connection', 'keep-alive',
    'transfer-encoding', 'te', 'trailer', 'upgrade', 'proxy-connection', 'proxy-authorization',
    'accept-encoding',
}
WIN = 60.0
MAX_API = 20
MAX_IMG = 40
MAX_BODY = 64 * 1024 * 1024
MAX_RESP = 80 * 1024 * 1024
CONNECT_TIMEOUT = 20
API_READ_TIMEOUT = 180
IMG_READ_TIMEOUT = 60

RL = {}
RL_LOCK = threading.Lock()
SSL_CTX = ssl.create_default_context()

mimetypes.add_type('image/webp', '.webp', strict=True)
mimetypes.add_type('application/manifest+json', '.webmanifest', strict=True)
mimetypes.add_type('image/svg+xml', '.svg', strict=True)
mimetypes.add_type('application/javascript', '.js', strict=True)
mimetypes.add_type('text/css', '.css', strict=True)
mimetypes.add_type('application/json', '.json', strict=True)


class PrivateTarget(Exception):
    pass


class TargetUnreachable(Exception):
    pass


def limited(ip, kind):
    """固定 60 秒窗口。返回 True 表示这一次已经超过上限。"""
    now = time.monotonic()
    cap = MAX_IMG if kind == 'img' else MAX_API
    key = kind + '|' + ip
    with RL_LOCK:
        entry = RL.get(key)
        if not entry or now - entry[0] > WIN:
            entry = [now, 0]
            RL[key] = entry
        entry[1] += 1
        if len(RL) > 5000:
            for old in list(RL):
                if now - RL[old][0] > WIN:
                    del RL[old]
        return entry[1] > cap


def reset_limits():
    with RL_LOCK:
        RL.clear()


def public_ip(ip):
    """只接受可全球路由的单播地址。组播在部分 Python 版本里 is_global 仍为真，要单独排除。"""
    if getattr(ip, 'ipv4_mapped', None) is not None and not public_ip(ip.ipv4_mapped):
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_private:
        return False
    if not ip.is_global:
        return False
    return True


def _literal_ip(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        number = int(host)
        if 0 <= number <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(number)
    return None


def screen_host(host):
    """解析并确认目标没有落到内网、回环、链路本地或保留地址。返回本次允许连接的地址。"""
    host = host.strip().rstrip('.').lower()
    if not host or '%' in host or host in {'localhost', 'metadata.google.internal', 'metadata.google'}:
        raise PrivateTarget()
    if host.endswith('.localhost') or host.endswith('.local') or host.endswith('.internal'):
        raise PrivateTarget()
    # 前导零会被部分浏览器按八进制解释（0177.0.0.1 → 127.0.0.1）。这里直接拒绝。
    if re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', host):
        parts = host.split('.')
        if any(len(part) > 1 and part.startswith('0') for part in parts):
            raise PrivateTarget()
    literal = _literal_ip(host)
    if literal is not None:
        if not public_ip(literal):
            raise PrivateTarget()
        return [str(literal)]
    # 纯数字或八进制/十六进制写法的“IP”，浏览器可能当成地址，这里一律拒绝。
    labels = host.split('.')
    if labels and all(re.fullmatch(r'0x[0-9a-f]+|\d+', label) for label in labels):
        raise PrivateTarget()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TargetUnreachable(str(exc)) from exc
    ips = []
    for info in infos:
        addr = info[4][0].split('%', 1)[0]
        ip = ipaddress.ip_address(addr)
        if not public_ip(ip):
            raise PrivateTarget()
        text = str(ip)
        if text not in ips:
            ips.append(text)
    if not ips:
        raise PrivateTarget()
    return ips


def classify_target(raw, custom, kind):
    """语法与白名单检查，不发起连接。返回 ('ok', parts) 或 ('err', status, message)。"""
    if not raw or any(ch in raw for ch in ' \t\r\n\\'):
        return ('err', 400, '缺少或无效的 x-target-url')
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ('err', 400, '缺少或无效的 x-target-url')
    if parts.username or parts.password or '@' in raw:
        return ('err', 400, '目标地址必须是标准 https 地址')
    if parts.scheme != 'https' or not parts.hostname:
        return ('err', 400, '目标地址必须是标准 https 地址')
    if parts.port not in (None, 443):
        return ('err', 400, '目标地址必须是标准 https 地址')
    host = parts.hostname.rstrip('.').lower()
    path = parts.path or '/'
    if kind == 'image':
        if not any(rx.search(host) for rx in IMG_HOSTS):
            return ('err', 403, '该图片地址不在允许范围内')
        return ('ok', parts)
    official = any(rx.search(host) for rx in OFFICIAL)
    if not official and not custom:
        return ('err', 403, '该地址不是官方接口；自定义中转需在设置中选择「自定义」')
    bare = path.rstrip('/') or '/'
    if official:
        if not API_PATH.search(bare):
            return ('err', 403, '该路径不允许通过代理转发')
    elif not CUSTOM_PATH.search(bare):
        return ('err', 403, '该路径不允许通过代理转发')
    return ('ok', parts)


def origin_allowed(host_header, origin, referer, public_origin):
    """与 proxy.js 的 sameSite 一致：只看 Origin，没有 Origin 才看 Referer。"""
    host = (host_header or '').strip()
    if not host:
        return False
    if public_origin:
        expected = urllib.parse.urlsplit(public_origin).netloc
        if not expected or host.lower() != expected.lower():
            return False
    for value in (origin, referer):
        if not value:
            continue
        try:
            netloc = urllib.parse.urlsplit(value).netloc
        except ValueError:
            return False
        return bool(netloc) and netloc.lower() == host.lower()
    return False


def strip_site_headers(pairs):
    """纯 HTTP 部署永远不发 HSTS，也不把请求升级到 https。"""
    out = []
    for name, value in pairs:
        key = name.lower()
        if key == 'strict-transport-security':
            continue
        if key == 'content-security-policy':
            parts = [p.strip() for p in value.split(';')]
            parts = [p for p in parts if p and p.lower() != 'upgrade-insecure-requests']
            value = '; '.join(parts)
        if '\r' in value or '\n' in value:
            continue
        out.append((name, value))
    return out


def parse_cf_headers(text):
    rules = []
    pattern = None
    headers = []

    def flush():
        nonlocal pattern, headers
        if pattern is not None:
            rules.append((pattern, headers))
        pattern, headers = None, []

    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line[:1] in ' \t':
            if ':' in line and pattern is not None:
                name, value = line.strip().split(':', 1)
                headers.append((name.strip(), value.strip()))
            continue
        flush()
        pattern = line.strip()
        headers = []
    flush()
    compiled = []
    for pattern, headers in rules:
        rx = re.compile('^' + re.escape(pattern).replace(r'\*', '.*') + '$')
        compiled.append((rx, strip_site_headers(headers)))
    return compiled


def headers_for(rules, path):
    found = {}
    order = []
    for rx, headers in rules:
        if not rx.search(path):
            continue
        for name, value in headers:
            key = name.lower()
            if key not in found:
                order.append(key)
            found[key] = (name, value)
    return [found[key] for key in order]


def error_payload(status, message):
    body = json.dumps({'error': {'message': message}}, ensure_ascii=False).encode('utf-8')
    return status, body


def _safe_header(value):
    value = value.strip()
    if not value or '\r' in value or '\n' in value:
        return ''
    return value


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, 443, timeout=timeout, context=SSL_CTX)
        self._pin_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pin_ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def open_upstream(method, parts, ips, req_headers, body, kind):
    """连到已经筛过的地址。返回 (status, content_type, resp, conn)。调用方负责关闭 conn。"""
    host = parts.hostname.rstrip('.').lower()
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    read_timeout = IMG_READ_TIMEOUT if kind == 'image' else API_READ_TIMEOUT
    last = None
    for ip in ips:
        conn = PinnedHTTPSConnection(host, ip, CONNECT_TIMEOUT)
        try:
            conn.connect()
            conn.sock.settimeout(read_timeout)
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader('Host', host)
            conn.putheader('Accept-Encoding', 'identity')
            for name, value in req_headers:
                conn.putheader(name, value)
            if body is not None:
                conn.putheader('Content-Length', str(len(body)))
            conn.endheaders(body if body else None)
            resp = conn.getresponse()
            content_type = _safe_header(resp.headers.get('Content-Type') or '')
            if kind == 'image':
                if resp.status < 200 or resp.status >= 300 or not content_type.lower().startswith('image/'):
                    resp.read(8192)
                    conn.close()
                    raise ImageRejected()
            length = resp.length
            if length is not None and length > MAX_RESP:
                conn.close()
                raise TargetUnreachable('response too large')
            return resp.status, content_type or 'application/octet-stream', resp, conn
        except ImageRejected:
            raise
        except Exception as exc:
            last = exc
            try:
                conn.close()
            except Exception:
                pass
    raise TargetUnreachable(str(last) if last else 'connect failed')


class ImageRejected(Exception):
    pass


def load_config():
    host = os.environ.get('POSEWEAR_HOST', '0.0.0.0')
    port = int(os.environ.get('POSEWEAR_PORT', '80'))
    root = os.environ.get('POSEWEAR_ROOT', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist'))
    if port == 443:
        raise SystemExit('拒绝监听 443：该端口不归本服务使用，程序不会绑定或修改它。')
    if port < 1 or port > 65535:
        raise SystemExit('POSEWEAR_PORT 无效')
    if not os.path.isdir(root):
        raise SystemExit('找不到站点目录：' + root)
    return host, port, os.path.realpath(root)


class Handler(BaseHTTPRequestHandler):
    server_version = 'PoseWear'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.timeout = 60

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.log_date_time_string(), fmt % args))

    def do_HEAD(self):
        self._static(head=True)

    def do_GET(self):
        if self._proxy_path():
            self._proxy('GET')
            return
        self._static(head=False)

    def do_POST(self):
        if self._proxy_path():
            self._proxy('POST')
            return
        self._send_json(*error_payload(405, '只支持 GET / POST'))

    def do_OPTIONS(self):
        if self._proxy_path():
            self.send_response(204)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _proxy_path(self):
        path = urllib.parse.urlsplit(self.path).path
        return path == '/api/proxy' or path == '/api/proxy/'

    def _client_ip(self):
        return self.client_address[0]

    def _send_json(self, status, body):
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        self.close_connection = True
        if method not in ('GET', 'POST'):
            self._send_json(*error_payload(405, '只支持 GET / POST'))
            return
        public = os.environ.get('POSEWEAR_PUBLIC_ORIGIN', '').strip()
        if not origin_allowed(self.headers.get('Host'), self.headers.get('Origin'), self.headers.get('Referer'), public):
            self._send_json(*error_payload(403, '只接受本站页面发起的请求'))
            return
        raw = self.headers.get('x-target-url') or ''
        kind = 'image' if self.headers.get('x-proxy-kind') == 'image' else 'api'
        custom = self.headers.get('x-proxy-custom') == '1'
        if kind == 'image' and method != 'GET':
            self._send_json(*error_payload(405, '图片下载只支持 GET'))
            return
        decision = classify_target(raw, custom, kind)
        if decision[0] != 'ok':
            self._send_json(*error_payload(decision[1], decision[2]))
            return
        parts = decision[1]
        if limited(self._client_ip(), 'img' if kind == 'image' else 'api'):
            self._send_json(*error_payload(429, '请求过于频繁，请稍后再试'))
            return
        body = None
        if method == 'POST':
            if self.headers.get('Transfer-Encoding'):
                self._send_json(*error_payload(411, '需要 Content-Length'))
                return
            try:
                length = int(self.headers.get('Content-Length') or '0')
            except ValueError:
                self._send_json(*error_payload(400, '缺少或无效的 x-target-url'))
                return
            if length < 0 or length > MAX_BODY:
                self._send_json(*error_payload(413, '请求体过大'))
                return
            body = self.rfile.read(length) if length else b''
        try:
            ips = screen_host(parts.hostname)
        except PrivateTarget:
            self._send_json(*error_payload(403, '不能转发到内网地址'))
            return
        except TargetUnreachable:
            self._send_json(*error_payload(502, '代理无法连接目标服务'))
            return
        forwarded = []
        for name, value in self.headers.items():
            key = name.lower()
            if key in DROP or key.startswith('cf-') or key.startswith('proxy-'):
                continue
            safe = _safe_header(value)
            if not safe:
                continue
            forwarded.append((name, safe))
        conn = None
        try:
            status, content_type, resp, conn = open_upstream(method, parts, ips, forwarded, body, kind)
        except ImageRejected:
            self._send_json(*error_payload(502, '结果图片下载失败'))
            return
        except (PrivateTarget, TargetUnreachable, OSError, http.client.HTTPException, ssl.SSLError):
            self._send_json(*error_payload(502, '代理无法连接目标服务'))
            return
        length = resp.length if resp.length is not None and resp.length <= MAX_RESP else None
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()
        sent = 0
        try:
            while True:
                if length is not None and sent >= length:
                    break
                want = 65536 if length is None else min(65536, length - sent)
                chunk = resp.read(want)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MAX_RESP:
                    break
                self.wfile.write(chunk)
        finally:
            if conn is not None:
                conn.close()

    def _root(self):
        return self.server.site_root

    def _translate(self, url_path):
        path = urllib.parse.urlsplit(url_path).path
        path = urllib.parse.unquote(path)
        if '\x00' in path:
            return None
        rel = path.lstrip('/')
        if rel == '_headers' or rel.startswith('.') or '/.' in '/' + rel:
            return None
        root = self._root()
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        return path, full

    def _static(self, head):
        found = self._translate(self.path)
        if found is None:
            self._not_found(head)
            return
        req_path, full = found
        if os.path.isdir(full):
            if not req_path.endswith('/'):
                dest = req_path + '/'
                if urllib.parse.urlsplit(self.path).query:
                    dest += '?' + urllib.parse.urlsplit(self.path).query
                self.send_response(301)
                self.send_header('Location', dest)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            full = os.path.join(full, 'index.html')
            if not os.path.isfile(full):
                self._not_found(head)
                return
        elif not os.path.isfile(full):
            self._not_found(head)
            return
        self._send_file(req_path if not req_path.endswith('/') else req_path + 'index.html', full, 200, head)

    def _not_found(self, head):
        page = os.path.join(self._root(), '404.html')
        if os.path.isfile(page):
            self._send_file('/404.html', page, 404, head)
            return
        body = '404'.encode()
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_file(self, header_path, full, status, head):
        try:
            size = os.path.getsize(full)
            handle = open(full, 'rb')
        except OSError:
            self._not_found(head)
            return
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in {'application/javascript', 'application/json', 'application/manifest+json'}:
            ctype += '; charset=utf-8'
        rules = getattr(self.server, 'header_rules', [])
        extra = headers_for(rules, header_path if header_path.startswith('/') else '/' + header_path)
        # 目录页用真实 URL 匹配缓存规则（/pose/id/ 而不是 index.html）。
        if header_path.endswith('/index.html'):
            extra = headers_for(rules, header_path[:-len('index.html')]) or extra
        data = None
        start = 0
        end = size - 1
        ranged = False
        range_header = self.headers.get('Range') if status == 200 else None
        if range_header and self.headers.get('If-Range') is None:
            spec = _parse_range(range_header, size)
            if spec == 'unsat':
                handle.close()
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if spec:
                start, end = spec
                ranged = True
                status = 206
        encoding = ''
        textish = ctype.startswith('text/') or ctype in {
            'application/javascript', 'application/json', 'application/manifest+json', 'image/svg+xml',
        }
        accept = self.headers.get('Accept-Encoding') or ''
        if not ranged and textish and 'gzip' in accept and 256 <= size <= 4 * 1024 * 1024:
            raw = handle.read()
            handle.close()
            handle = None
            data = gzip.compress(raw, mtime=0)
            encoding = 'gzip'
        length = (end - start + 1) if data is None else len(data)
        self.send_response(status)
        self.send_header('Content-Type', ctype if ctype != 'image/svg+xml' else 'image/svg+xml')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Accept-Ranges', 'bytes')
        if encoding:
            self.send_header('Content-Encoding', encoding)
            self.send_header('Vary', 'Accept-Encoding')
        for name, value in extra:
            if name.lower() == 'content-type':
                continue
            self.send_header(name, value)
        if ranged:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Content-Length', str(length))
        self.end_headers()
        if head:
            if handle:
                handle.close()
            return
        try:
            if data is not None:
                self.wfile.write(data)
            else:
                handle.seek(start)
                left = length
                while left:
                    chunk = handle.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        finally:
            if handle:
                handle.close()


def _parse_range(header, size):
    if not header or not header.startswith('bytes=') or ',' in header or size <= 0:
        return None
    spec = header[6:].strip()
    try:
        if spec.startswith('-'):
            count = int(spec[1:])
            if count <= 0:
                return None
            start = max(0, size - count)
            end = size - 1
        else:
            left, right = spec.split('-', 1)
            start = int(left)
            end = int(right) if right else size - 1
    except ValueError:
        return None
    if start >= size or start > end or start < 0:
        return 'unsat'
    return start, min(end, size - 1)


class SiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, root):
        self.site_root = root
        header_file = os.path.join(root, '_headers')
        self.header_rules = []
        if os.path.isfile(header_file):
            with open(header_file, encoding='utf-8') as fh:
                self.header_rules = parse_cf_headers(fh.read())
        super().__init__(addr, Handler)


def make_server(host, port, root):
    return SiteServer((host, port), os.path.realpath(root))


def main():
    host, port, root = load_config()
    httpd = make_server(host, port, root)
    print('PoseWear HTTP %s:%s  根目录 %s' % (host, port, root), flush=True)
    print('未监听 443，也不修改该端口上的现有程序。', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n停止', flush=True)
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
