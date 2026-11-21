#!/usr/bin/env python3
"""自托管服务的本地检查：静态路由、响应头、无密钥转发、内网与开放代理拦截。

不调用付费生成接口。无密钥请求只打到官方接口的鉴权失败响应。
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

ROOT = tempfile.mkdtemp(prefix='posewear-testdist-')
PASSED = 0
FAILED = 0


def check(name, ok, detail=''):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print('PASS', name)
    else:
        FAILED += 1
        print('FAIL', name, detail)


def build_fixture():
    os.makedirs(os.path.join(ROOT, 'assets'), exist_ok=True)
    os.makedirs(os.path.join(ROOT, 'p', 't'), exist_ok=True)
    os.makedirs(os.path.join(ROOT, 'pose', 'demo'), exist_ok=True)
    open(os.path.join(ROOT, 'index.html'), 'w', encoding='utf-8').write('<!doctype html><title>衣姿</title><p>posewear</p>')
    open(os.path.join(ROOT, '404.html'), 'w', encoding='utf-8').write('<!doctype html><title>页面不存在</title>')
    open(os.path.join(ROOT, 'favicon.svg'), 'w', encoding='utf-8').write('<svg xmlns="http://www.w3.org/2000/svg"/>')
    # 最小 WebP 容器，只用来确认静态类型和字节原样返回。
    tiny = (
        b'RIFF' + (20 + 10).to_bytes(4, 'little') + b'WEBP'
        + b'VP8 ' + (10).to_bytes(4, 'little')
        + bytes([0x30, 0x01, 0x00, 0x9d, 0x01, 0x2a, 0x01, 0x00, 0x01, 0x00])
    )
    open(os.path.join(ROOT, 'p', 't', 'x.webp'), 'wb').write(tiny)
    open(os.path.join(ROOT, 'pose', 'demo', 'index.html'), 'w', encoding='utf-8').write('<!doctype html><title>demo pose</title>')
    open(os.path.join(ROOT, '_headers'), 'w', encoding='utf-8').write(
        '/*\n'
        '  Content-Security-Policy: default-src \'self\'; frame-ancestors \'none\'; upgrade-insecure-requests\n'
        '  X-Content-Type-Options: nosniff\n'
        '  X-Frame-Options: DENY\n'
        '  Strict-Transport-Security: max-age=31536000; includeSubDomains\n'
        '  Referrer-Policy: strict-origin-when-cross-origin\n'
        '/\n'
        '  Cache-Control: public, max-age=0, must-revalidate\n'
        '/p/*\n'
        '  Cache-Control: public, max-age=31536000, immutable\n'
        '/pose/*\n'
        '  Cache-Control: public, max-age=600\n'
    )
    return tiny


def start():
    os.environ.pop('POSEWEAR_PUBLIC_ORIGIN', None)
    os.environ['POSEWEAR_ROOT'] = ROOT
    httpd = serve.make_server('127.0.0.1', 0, ROOT)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, 'http://127.0.0.1:%d' % httpd.server_address[1]


OPENER = urllib.request.OpenerDirector()
OPENER.add_handler(urllib.request.HTTPHandler())
OPENER.add_handler(urllib.request.HTTPSHandler())


def start_tls(root):
    folder = tempfile.mkdtemp(prefix='posewear-cert-')
    cert, key = os.path.join(folder, 'cert.pem'), os.path.join(folder, 'key.pem')
    subprocess.check_call([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', key, '-out', cert,
        '-days', '1', '-nodes', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    httpd = serve.make_tls_server('127.0.0.1', 0, root, cert, key)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cert)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    return httpd, 'https://127.0.0.1:%d' % httpd.server_address[1], opener


def fetch(url, headers=None, method='GET', data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with OPENER.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main():
    samples = [
        ('https://api.openai.com/v1/images/edits', 'api', False),
        ('https://api.openai.com/v1/images/generations', 'api', False),
        ('https://api.openai.com/v1/models', 'api', False),
        ('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent', 'api', False),
        ('https://ark.cn-beijing.volces.com/api/v3/images/generations', 'api', False),
        ('https://ark.ap-southeast.bytepluses.com/api/v3/images/generations', 'api', False),
    ]
    for url, kind, custom in samples:
        got = serve.classify_target(url, custom, kind)
        check('允许官方路径 ' + url.split('//', 1)[1], got[0] == 'ok', got)
    blocked = serve.classify_target('https://api.openai.com/v1/chat/completions', False, 'api')
    check('拦截 chat 路径', blocked[0] == 'err' and blocked[1] == 403, blocked)
    check('100.64 不是公网', not serve.public_ip(__import__('ipaddress').ip_address('100.64.1.1')))
    check('8.8.8.8 是公网', serve.public_ip(__import__('ipaddress').ip_address('8.8.8.8')))

    tiny = build_fixture()
    httpd, base = start()
    host = base.split('://', 1)[1]
    origin = {'Origin': base, 'Host': host}

    status, headers, body = fetch(base + '/')
    check('首页 200', status == 200 and b'posewear' in body, status)
    check('首页不含 HSTS', 'Strict-Transport-Security' not in headers and 'strict-transport-security' not in {k.lower() for k in headers}, headers)
    csp = headers.get('Content-Security-Policy') or headers.get('content-security-policy') or ''
    check('CSP 不含 upgrade-insecure-requests', 'upgrade-insecure-requests' not in csp.lower(), csp)
    check('CSP 仍限制 default-src', "default-src 'self'" in csp, csp)
    check('首页不被缓存死', 'max-age=0' in (headers.get('Cache-Control') or headers.get('cache-control') or ''), headers)

    status, headers, body = fetch(base + '/favicon.svg')
    check('favicon', status == 200 and b'<svg' in body, status)
    check('svg 类型', 'image/svg+xml' in (headers.get('Content-Type') or ''), headers.get('Content-Type'))

    status, headers, body = fetch(base + '/p/t/x.webp')
    check('素材 webp 200', status == 200 and body == tiny, (status, len(body), len(tiny)))
    check('webp 类型', (headers.get('Content-Type') or '').startswith('image/webp'), headers.get('Content-Type'))
    check('素材长期缓存', 'immutable' in (headers.get('Cache-Control') or ''), headers.get('Cache-Control'))
    check('素材响应也不含 HSTS', 'Strict-Transport-Security' not in headers, headers)

    status, headers, body = fetch(base + '/pose/demo/')
    check('详情页目录', status == 200 and b'demo pose' in body, status)
    check('详情页缓存', 'max-age=600' in (headers.get('Cache-Control') or ''), headers.get('Cache-Control'))

    status, _, body = fetch(base + '/pose/demo')
    check('目录补斜杠', status in (301, 302), status)

    status, _, body = fetch(base + '/no-such-page')
    check('未知路由走 404 页', status == 404 and '页面不存在'.encode() in body, (status, body[:80]))

    status, _, body = fetch(base + '/%2e%2e/%2e%2e/etc/passwd')
    check('路径穿越被拦住', status == 404 and b'root:' not in body, status)

    status, _, body = fetch(base + '/_headers')
    check('_headers 不对外', status == 404 and b'Strict-Transport-Security' not in body, status)

    # 代理：没有 Origin
    status, _, body = fetch(base + '/api/proxy', {'x-target-url': 'https://api.openai.com/v1/models'})
    check('无 Origin 拒绝', status == 403 and '只接受本站页面'.encode() in body, (status, body))

    status, _, body = fetch(base + '/api/proxy', {**origin, 'Origin': 'https://evil.example'})
    msg = body.decode('utf-8', 'replace')
    check('异源 Origin 拒绝', status == 403 and '只接受本站' in msg, (status, msg))

    cases = [
        ('http 被拒绝', 'https://127.0.0.1/'.replace('https', 'http'), None, 400),
        ('缺少目标', '', None, 400),
        ('用户信息被拒绝', 'https://user:pass@api.openai.com/v1/models', None, 400),
        ('非 443 端口', 'https://api.openai.com:8443/v1/models', None, 400),
        ('非官方且未声明自定义', 'https://example.com/v1/images/generations', None, 403),
        ('自定义但路径不是图片接口', 'https://example.com/', '1', 403),
        ('官方路径不允许 chat', 'https://api.openai.com/v1/chat/completions', None, 403),
        ('回环', 'https://127.0.0.1/v1/images/generations', '1', 403),
        ('十进制回环', 'https://2130706433/v1/images/generations', '1', 403),
        ('前导零回环', 'https://0177.0.0.1/v1/images/generations', '1', 403),
        ('本机名', 'https://localhost/v1/images/generations', '1', 403),
        ('链路本地元数据', 'https://169.254.169.254/v1/images/generations', '1', 403),
        ('10 网段', 'https://10.1.2.3/v1/images/generations', '1', 403),
        ('172.16 网段', 'https://172.16.0.5/v1/images/generations', '1', 403),
        ('192.168 网段', 'https://192.168.0.2/v1/images/generations', '1', 403),
        ('IPv6 回环', 'https://[::1]/v1/images/generations', '1', 403),
        ('未指定地址', 'https://0.0.0.0/v1/images/generations', '1', 403),
        ('图片代理打到任意站', 'https://example.com/a.png', None, 403),
        ('图片代理打到元数据', 'https://169.254.169.254/latest/meta-data', None, 403),
    ]
    for name, target, custom, expect in cases:
        hdr = dict(origin)
        hdr['x-target-url'] = target
        if name.startswith('图片'):
            hdr['x-proxy-kind'] = 'image'
        if custom:
            hdr['x-proxy-custom'] = custom
        status, _, body = fetch(base + '/api/proxy', hdr)
        text = body.decode('utf-8', 'replace')
        intranet = name in {
            '回环', '十进制回环', '前导零回环', '本机名', '链路本地元数据', '10 网段',
            '172.16 网段', '192.168 网段', 'IPv6 回环', '未指定地址',
        }
        ok = status == expect and 'error' in text and (not intranet or '内网' in text)
        check(name, ok, (status, text[:180]))

    status, _, body = fetch(base + '/api/proxy', {**origin, 'x-target-url': 'https://localtest.me/v1/images/generations', 'x-proxy-custom': '1'})
    text = body.decode('utf-8', 'replace')
    check('DNS 指向 127.0.0.1 被拒绝', status == 403 and '内网' in text, (status, text[:180]))

    status, _, body = fetch(base + '/api/proxy', {**origin, 'x-target-url': 'https://api.openai.com/v1/models'})
    text = body.decode('utf-8', 'replace')
    check('明文不转发官方接口', status == 403 and 'HTTPS' in text, (status, text[:180]))

    tls, tls_base, tls_open = start_tls(ROOT)
    thost = tls_base.split('://', 1)[1]
    torigin = {'Origin': tls_base, 'Host': thost}

    def tfetch(path, headers):
        req = urllib.request.Request(tls_base + path, headers=headers)
        try:
            with tls_open.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    serve.reset_limits()
    for _ in range(serve.MAX_API):
        serve.limited('127.0.0.1', 'api')
    status, body = tfetch('/api/proxy', {**torigin, 'x-target-url': 'https://api.openai.com/v1/models'})
    check('超过频率返回 429', status == 429 and '频繁'.encode() in body, (status, body[:120]))
    serve.reset_limits()

    direct = urllib.request.Request('https://api.openai.com/v1/models', method='GET')
    try:
        with urllib.request.urlopen(direct, timeout=30) as resp:
            direct_status, direct_body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        direct_status, direct_body = exc.code, exc.read()
    status, body = tfetch('/api/proxy', {**torigin, 'x-target-url': 'https://api.openai.com/v1/models'})
    proxy_text = body.decode('utf-8', 'replace')
    check('无密钥状态与直连一致', status == direct_status and status in (401, 403), (status, direct_status, proxy_text[:160], direct_body[:160]))
    check('响应里没有本站注入的密钥字段', 'sk-' not in proxy_text and 'Authorization' not in proxy_text, proxy_text[:160])
    check('无密钥不是本站伪造的 200', status != 200, status)
    tls.shutdown()

    # 绑定保护
    os.environ['POSEWEAR_PORT'] = '443'
    os.environ['POSEWEAR_ROOT'] = ROOT
    try:
        serve.load_config()
        check('拒绝绑定 443', False, '没有退出')
    except SystemExit as exc:
        check('拒绝绑定 443', '443' in str(exc), exc)
    os.environ['POSEWEAR_PORT'] = '80'

    httpd.shutdown()
    print('\n%d passed, %d failed' % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    raise SystemExit(main())
