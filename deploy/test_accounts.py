#!/usr/bin/env python3
"""两个隔离账号的正反向检查。测试用户和证书只存在临时目录，不进入发布包。"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import account
import serve

ROOT = tempfile.mkdtemp(prefix='posewear-acct-')
STATE = tempfile.mkdtemp(prefix='posewear-state-')
os.environ['POSEWEAR_STATE'] = STATE
os.environ['POSEWEAR_ROOT'] = ROOT
os.environ.pop('POSEWEAR_PUBLIC_ORIGIN', None)
open(os.path.join(ROOT, 'index.html'), 'w').write('<!doctype html><title>衣姿</title>')
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


def cert():
    folder = tempfile.mkdtemp(prefix='posewear-cert-')
    crt, key = os.path.join(folder, 'cert.pem'), os.path.join(folder, 'key.pem')
    subprocess.check_call([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', key, '-out', crt,
        '-days', '1', '-nodes', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return crt, key


def listen(factory):
    httpd = factory()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def opener_for(crt):
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(crt)
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


class Client:
    def __init__(self, opener, base):
        self.opener = opener
        self.base = base
        self.host = base.split('://', 1)[1]
        self.cookie = ''
        self.csrf = ''

    def call(self, method, path, payload=None, raw=None, content_type=None, origin=None, cookie=None, csrf=None):
        headers = {'Host': self.host, 'Origin': self.base if origin is None else origin}
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers['Cookie'] = use_cookie
        token = self.csrf if csrf is None else csrf
        if token and method != 'GET':
            headers['X-CSRF-Token'] = token
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        elif content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                status, hdrs, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, hdrs, body = exc.code, exc.headers, exc.read()
        cookies = []
        if hdrs is not None and hasattr(hdrs, 'get_all'):
            cookies = hdrs.get_all('Set-Cookie') or []
        return status, cookies, body

    def json(self, method, path, payload=None, **kw):
        status, cookies, body = self.call(method, path, payload=payload, **kw)
        try:
            data = json.loads(body.decode())
        except Exception:
            data = {}
        return status, cookies, data, body


def grab(cookies, name):
    for raw in cookies:
        first = raw.split(';', 1)[0]
        if first.startswith(name + '='):
            return first.split('=', 1)[1], raw
    return '', ''


def main():
    crt, key = cert()
    httpd = listen(lambda: serve.make_server('127.0.0.1', 0, ROOT))
    tls = listen(lambda: serve.make_tls_server('127.0.0.1', 0, ROOT, crt, key))
    http_base = 'http://127.0.0.1:%d' % httpd.server_address[1]
    https_base = 'https://127.0.0.1:%d' % tls.server_address[1]
    plain = Client(urllib.request.build_opener(), http_base)
    a = Client(opener_for(crt), https_base)
    b = Client(opener_for(crt), https_base)
    pw_a = 'Aa-' + secrets.token_hex(8)
    pw_b = 'Bb-' + secrets.token_hex(8)
    sentinel = 'SENTINEL-' + secrets.token_hex(4)

    status, _, data, _ = plain.json('POST', '/api/auth/register', {'username': 'plainuser', 'password': pw_a})
    check('明文注册被拒绝', status == 403 and 'HTTPS' in (data.get('error') or {}).get('message', ''), data)
    status, _, data, _ = plain.json('GET', '/api/auth/me')
    check('明文会话接口拒绝', status == 403, data)
    status, _, body = plain.call('GET', '/')
    check('明文仍可看页面', status == 200 and '衣姿'.encode() in body, status)

    status, cookies, data, _ = a.json('POST', '/api/auth/register', {'username': 'userA', 'password': pw_a})
    token, raw = grab(cookies, '__Host-pw')
    check('A 注册', status == 200 and data.get('username') == 'userA' and token, data)
    check('会话 Cookie 安全属性', 'Secure' in raw and 'HttpOnly' in raw and 'SameSite=Strict' in raw and 'Path=/' in raw, raw)
    a.cookie = '__Host-pw=' + token
    a.csrf = data.get('csrf') or ''
    status, cookies, data, _ = b.json('POST', '/api/auth/register', {'username': 'userB', 'password': pw_b})
    token_b, _ = grab(cookies, '__Host-pw')
    b.cookie = '__Host-pw=' + token_b
    b.csrf = data.get('csrf') or ''
    check('B 注册', status == 200 and data.get('username') == 'userB', data)
    status, _, data, _ = a.json('POST', '/api/auth/register', {'username': 'userA', 'password': pw_a + '-x'})
    check('重名注册被拒绝', status == 409, data)

    status, _, data, _ = a.json('PUT', '/api/me/state', {'state': {'fav': ['c:SPR-0001'], 'wbCfg': {'prov': 'openai', 'key': sentinel, 'model': 'gpt-image-1'}}})
    check('A 写入状态', status == 200, data)
    status, _, data, _ = a.json('GET', '/api/me/state')
    state = data.get('state') or {}
    check('A 读到自己的收藏', state.get('fav') == ['c:SPR-0001'], state)
    check('密钥没有落在服务端状态', sentinel not in json.dumps(state) and 'key' not in (state.get('wbCfg') or {}), state)
    status, _, data, _ = b.json('GET', '/api/me/state')
    check('B 看不到 A 的收藏', (data.get('state') or {}).get('fav') in (None, []), data)
    status, _, data, _ = b.json('PUT', '/api/me/state', {'state': {'fav': ['p:other']}})
    check('B 写入自己的收藏', status == 200, data)
    status, _, data, _ = a.json('GET', '/api/me/state')
    check('A 的收藏没被 B 改掉', (data.get('state') or {}).get('fav') == ['c:SPR-0001'], data)

    png = b'\x89PNG\r\n\x1a\n' + b'A-private-image'
    status, _, body = a.call('POST', '/api/me/blobs', raw=png, content_type='image/png')
    blob = json.loads(body.decode())
    check('A 上传私有图', status == 200 and blob.get('id'), blob)
    status, _, body = b.call('GET', '/api/me/blobs/' + blob['id'])
    check('B 读取 A 的图片被拒绝', status == 404 and b'A-private-image' not in body, (status, body[:80]))
    status, _, body = a.call('GET', '/api/me/blobs/' + blob['id'])
    check('A 能读自己的图片', status == 200 and body == png, (status, body[:20]))
    status, _, body = b.call('GET', '/api/me/blobs/../../etc/passwd')
    check('路径穿越不是图片', status == 404 and b'root:' not in body, status)
    status, _, body = b.call('GET', '/api/me/blobs/' + 'ab' * 16)
    check('猜一个不存在的图片 ID', status == 404, status)
    status, _, body = plain.call('GET', '/api/me/blobs/' + blob['id'])
    check('未登录读图片', status == 403 or status == 401, status)

    status, _, data, _ = a.json('POST', '/api/me/history', {'t': 1001, 'prov': 'openai', 'model': 'm', 'prompt': 'red coat', 'mode': 'tryon', 'label': 'A', 'after': blob['id']})
    check('A 写入生成记录', status == 200, data)
    status, _, data, _ = b.json('GET', '/api/me/history')
    check('B 的历史是空的', data.get('items') == [], data)
    status, _, data, _ = b.json('DELETE', '/api/me/history/1001')
    check('B 删不掉 A 的记录', status == 404, data)
    status, _, data, _ = a.json('GET', '/api/me/history')
    check('A 的记录还在', any(item.get('label') == 'A' for item in data.get('items') or []), data)
    status, _, data, _ = b.json('POST', '/api/me/history', {'t': 1002, 'after': blob['id'], 'mode': 'tryon', 'prov': 'openai', 'model': 'm', 'prompt': 'x'})
    check('B 不能把 A 的图片写进自己的历史', status == 404, data)

    status, _, data, _ = a.json('PUT', '/api/me/state', {'state': {'fav': ['c:hacked']}}, origin='https://evil.example')
    check('错误 Origin 被拒绝', status == 403, data)
    status, _, data, _ = a.json('GET', '/api/me/state')
    check('CSRF 失败没有改掉收藏', (data.get('state') or {}).get('fav') == ['c:SPR-0001'], data)
    status, _, data, _ = a.json('PUT', '/api/me/state', {'state': {'fav': ['c:hacked']}}, csrf='nope')
    check('错误 CSRF 被拒绝', status == 403, data)

    other = Client(opener_for(crt), https_base)
    status, cookies, data, _ = other.json('POST', '/api/auth/login', {'username': 'userA', 'password': pw_a})
    other_token, _ = grab(cookies, '__Host-pw')
    other.cookie = '__Host-pw=' + other_token
    other.csrf = data.get('csrf') or ''
    check('同一账号第二台设备可以登录', status == 200, data)
    status, _, data, _ = other.json('GET', '/api/me/state')
    check('第二台设备看得到收藏', (data.get('state') or {}).get('fav') == ['c:SPR-0001'], data)
    check('第二台设备的状态里没有密钥', sentinel not in json.dumps(data), data)
    new_pw = 'Cc-' + secrets.token_hex(8)
    status, _, data, _ = a.json('POST', '/api/auth/password', {'old': pw_a, 'new': new_pw})
    check('A 修改密码', status == 200, data)
    status, _, data, _ = other.json('GET', '/api/auth/me')
    check('改密后另一台会话失效', status == 401, data)
    status, _, data, _ = a.json('GET', '/api/auth/me')
    check('改密的这台会话还在', status == 200 and data.get('username') == 'userA', data)

    old = a.cookie
    status, _, data, _ = a.json('POST', '/api/auth/logout', {})
    check('退出', status == 200, data)
    a.cookie = old
    status, _, data, _ = a.json('GET', '/api/auth/me')
    check('退出后原会话失效', status == 401, data)
    status, _, data, _ = a.json('GET', '/api/me/state')
    check('退出后不能再读状态', status == 401, data)

    db = os.path.join(STATE, 'posewear.sqlite')
    raw = open(db, 'rb').read()
    check('数据库里没有明文密码', pw_a.encode() not in raw and new_pw.encode() not in raw and pw_b.encode() not in raw)
    check('数据库里没有 API 密钥', sentinel.encode() not in raw)
    check('数据库不在站点目录', not db.startswith(os.path.realpath(ROOT)))
    mode = os.stat(db).st_mode & 0o777
    check('数据库文件不是全员可读', mode & 0o077 == 0, oct(mode))

    os.environ['POSEWEAR_PORT'] = '41080'
    os.environ['POSEWEAR_ROOT'] = ROOT
    try:
        serve.load_config()
        check('拒绝绑定 41080', False)
    except SystemExit as exc:
        check('拒绝绑定 41080', '41080' in str(exc), exc)
    os.environ['POSEWEAR_PORT'] = '80'
    os.environ['POSEWEAR_TLS_PORT'] = '443'
    try:
        serve.load_config()
        check('拒绝把 TLS 绑到 443', False)
    except SystemExit as exc:
        check('拒绝把 TLS 绑到 443', '443' in str(exc), exc)

    httpd.shutdown()
    tls.shutdown()
    account.close_db()
    print('\n%d passed, %d failed' % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == '__main__':
    raise SystemExit(main())
