#!/usr/bin/env python3
"""账号、会话和私有文件。数据库与图片都在 POSEWEAR_STATE，不在 dist 里。

API 密钥不入库。调用方如果把 key 放进状态，这里会丢掉。
密码只存 scrypt 散列。日志不记录口令、Cookie 或密钥。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse

COOKIE = '__Host-pw'
FLAG = 'pw_in'
SESSION_TTL = 14 * 24 * 3600
MAX_STATE = 256 * 1024
MAX_BLOB = 8 * 1024 * 1024
MAX_USER_BYTES = 400 * 1024 * 1024
MAX_HISTORY = 80
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
USER_RE = re.compile(r'^[A-Za-z0-9_-]{3,32}$')
BLOB_RE = re.compile(r'^[a-f0-9]{32}$')
ACCOUNT_FIELDS = {
    'fav', 'sel', 'rec', 'recT', 'ang', 'adult', 'ageOk', 'lang', 'tl', 'qh',
    'wbMode', 'wbChar', 'wbCharTxt', 'wbStyle', 'wbBm', 'wbPl', 'wbKeepBg',
    'wbNeutral', 'wbSel', 'wbCfg',
}

_LOCK = threading.Lock()
_SCRYPT = threading.Semaphore(1)
_DB = None
_DUMMY = None
_RL = {}


def state_dir():
    path = os.environ.get('POSEWEAR_STATE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'var')
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _conn():
    global _DB, _DUMMY
    if _DB is not None:
        return _DB
    root = state_dir()
    blobs = os.path.join(root, 'blobs')
    os.makedirs(blobs, exist_ok=True)
    try:
        os.chmod(blobs, 0o700)
    except OSError:
        pass
    path = os.path.join(root, 'posewear.sqlite')
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY,
      username TEXT NOT NULL UNIQUE,
      display TEXT NOT NULL,
      phc TEXT NOT NULL,
      created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE,
      csrf TEXT NOT NULL,
      expires INTEGER NOT NULL,
      created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS states (
      user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      body TEXT NOT NULL,
      updated INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS blobs (
      id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      mime TEXT NOT NULL,
      size INTEGER NOT NULL,
      created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS history (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      t REAL NOT NULL,
      body TEXT NOT NULL,
      created INTEGER NOT NULL,
      UNIQUE(user_id, t)
    );
    ''')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _DB = conn
    _DUMMY = _phc('posewear-timing-padding')
    return conn


def close_db():
    global _DB
    with _LOCK:
        if _DB is not None:
            _DB.close()
            _DB = None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _unb64(text):
    pad = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _phc(password):
    salt = os.urandom(16)
    with _SCRYPT:
        dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return 'scrypt$%d$%d$%d$%s$%s' % (SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(dk))


def _verify(password, stored):
    try:
        kind, n, r, p, salt, dk = stored.split('$')
        if kind != 'scrypt':
            return False
        n, r, p = int(n), int(r), int(p)
        if n != SCRYPT_N or r != SCRYPT_R or p != SCRYPT_P:
            return False
        with _SCRYPT:
            got = hashlib.scrypt(password.encode('utf-8'), salt=_unb64(salt), n=n, r=r, p=p, dklen=32)
        return hmac.compare_digest(got, _unb64(dk))
    except Exception:
        return False


def _limited(bucket, cap, window):
    now = time.time()
    with _LOCK:
        row = _RL.get(bucket)
        if not row or now - row[0] > window:
            row = [now, 0]
            _RL[bucket] = row
        row[1] += 1
        return row[1] > cap


def _token():
    return _b64(os.urandom(32))


def _hash_token(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _secure(handler):
    return bool(getattr(handler.server, 'secure', False))


def _origin_ok(handler):
    host = (handler.headers.get('Host') or '').strip()
    if not host:
        return False
    public = os.environ.get('POSEWEAR_PUBLIC_ORIGIN', '').strip()
    if public:
        expected = urllib.parse.urlsplit(public).netloc
        if not expected or host.lower() != expected.lower():
            return False
    origin = handler.headers.get('Origin') or ''
    if not origin:
        return False
    try:
        netloc = urllib.parse.urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == host.lower()


def _json_body(handler, limit=MAX_STATE):
    if handler.headers.get('Transfer-Encoding'):
        return None, 411
    try:
        length = int(handler.headers.get('Content-Length') or '0')
    except ValueError:
        return None, 400
    if length < 0 or length > limit:
        return None, 413
    raw = handler.rfile.read(length) if length else b''
    if not raw:
        return {}, None
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        return None, 400
    if not isinstance(data, dict):
        return None, 400
    return data, None


def _cookies(handler):
    out = {}
    raw = handler.headers.get('Cookie') or ''
    for part in raw.split(';'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def _session(handler):
    token = _cookies(handler).get(COOKIE)
    if not token or len(token) > 200:
        return None
    now = int(time.time())
    with _LOCK:
        row = _conn().execute(
            'SELECT s.id sid, s.csrf, s.expires, u.id uid, u.username, u.display '
            'FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?',
            (_hash_token(token),)).fetchone()
    if not row or row['expires'] < now:
        return None
    return row


def _require(handler, mutating):
    if not _secure(handler):
        _err(handler, 403, '登录、注册、修改密码和账号数据只接受 HTTPS')
        return None
    # 浏览器对同源 GET 不一定带 Origin。改数据的请求才核对来源和 CSRF。
    if mutating and not _origin_ok(handler):
        _err(handler, 403, '只接受本站页面发起的请求')
        return None
    row = _session(handler)
    if not row:
        _err(handler, 401, '未登录')
        return None
    if mutating:
        sent = handler.headers.get('X-CSRF-Token') or ''
        if not sent or not hmac.compare_digest(sent, row['csrf']):
            _err(handler, 403, '页面已过期，请刷新后再试')
            return None
    return row


def _err(handler, status, message):
    body = json.dumps({'error': {'message': message}}, ensure_ascii=False).encode('utf-8')
    handler._send_json(status, body)


def _ok(handler, payload, cookies=None, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler._send_json(status, body, cookies or [])


def _cookie(name, value, max_age, http_only):
    parts = [
        '%s=%s' % (name, value),
        'Path=/',
        'Max-Age=%d' % max_age,
        'Secure',
        'SameSite=Strict',
    ]
    if http_only:
        parts.append('HttpOnly')
    return ('Set-Cookie', '; '.join(parts))


def _auth_cookies(token, ttl):
    return [
        _cookie(COOKIE, token, ttl, True),
        _cookie(FLAG, '1', ttl, False),
    ]


def _clear_cookies():
    return _auth_cookies('', 0)


def _public_user(row):
    return {'id': row['uid'], 'username': row['display'], 'csrf': row['csrf']}


def _clean_state(data):
    out = {}
    for key in ACCOUNT_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == 'wbCfg':
            if not isinstance(value, dict):
                continue
            cfg = {}
            for field in ('prov', 'base', 'model', 'field', 'proto', 'proxy'):
                if field in value:
                    cfg[field] = value[field]
            cfg.pop('key', None)
            out[key] = cfg
            continue
        out[key] = value
    raw = json.dumps(out, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_STATE:
        return None
    return out


def _blob_path(user_id, blob_id):
    if not BLOB_RE.fullmatch(blob_id):
        return None
    root = os.path.realpath(os.path.join(state_dir(), 'blobs', str(int(user_id))))
    full = os.path.realpath(os.path.join(root, blob_id))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def wants(handler):
    path = urllib.parse.urlsplit(handler.path).path
    return path.startswith('/api/auth') or path.startswith('/api/me')


def handle(handler):
    path = urllib.parse.urlsplit(handler.path).path.rstrip('/') or '/'
    method = handler.command
    if path == '/api/auth/info' and method == 'GET':
        return _info(handler)
    if path == '/api/auth/register' and method == 'POST':
        return _register(handler)
    if path == '/api/auth/login' and method == 'POST':
        return _login(handler)
    if path == '/api/auth/logout' and method == 'POST':
        return _logout(handler)
    if path == '/api/auth/password' and method == 'POST':
        return _password(handler)
    if path == '/api/auth/me' and method == 'GET':
        return _me(handler)
    if path == '/api/me/state' and method == 'GET':
        return _state_get(handler)
    if path == '/api/me/state' and method == 'PUT':
        return _state_put(handler)
    if path == '/api/me/history' and method == 'GET':
        return _history_list(handler)
    if path == '/api/me/history' and method == 'POST':
        return _history_add(handler)
    if path == '/api/me/history' and method == 'DELETE':
        return _history_clear(handler)
    if path.startswith('/api/me/history/') and method == 'DELETE':
        return _history_delete(handler, path.rsplit('/', 1)[-1])
    if path == '/api/me/blobs' and method == 'POST':
        return _blob_add(handler)
    if path.startswith('/api/me/blobs/') and method == 'GET':
        return _blob_get(handler, path.rsplit('/', 1)[-1])
    if path.startswith('/api/me/blobs/') and method == 'DELETE':
        return _blob_delete(handler, path.rsplit('/', 1)[-1])
    _err(handler, 404, '没有这个接口')


def _info(handler):
    origin = os.environ.get('POSEWEAR_TLS_ORIGIN', '').strip()
    _ok(handler, {'secure': _secure(handler), 'httpsOrigin': origin})


def _register(handler):
    if not _secure(handler):
        return _err(handler, 403, '登录、注册、修改密码和账号数据只接受 HTTPS')
    if not _origin_ok(handler):
        return _err(handler, 403, '只接受本站页面发起的请求')
    ip = handler.client_address[0]
    if _limited('reg|' + ip, 5, 3600):
        return _err(handler, 429, '注册过于频繁，请稍后再试')
    data, err = _json_body(handler, 4096)
    if err:
        return _err(handler, err, '请求格式不正确')
    username = str(data.get('username') or '').strip()
    password = str(data.get('password') or '')
    if not USER_RE.fullmatch(username):
        return _err(handler, 400, '用户名用 3 到 32 位字母、数字、下划线或短横线')
    if len(password) < 10 or len(password) > 128:
        return _err(handler, 400, '密码至少 10 位，最长 128 位')
    phc = _phc(password)
    now = int(time.time())
    try:
        with _LOCK:
            cur = _conn().execute(
                'INSERT INTO users(username,display,phc,created) VALUES(?,?,?,?)',
                (username.lower(), username, phc, now))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return _err(handler, 409, '这个用户名已经有人用了')
    return _issue(handler, uid, username)


def _login(handler):
    if not _secure(handler):
        return _err(handler, 403, '登录、注册、修改密码和账号数据只接受 HTTPS')
    if not _origin_ok(handler):
        return _err(handler, 403, '只接受本站页面发起的请求')
    ip = handler.client_address[0]
    data, err = _json_body(handler, 4096)
    if err:
        return _err(handler, err, '请求格式不正确')
    username = str(data.get('username') or '').strip().lower()
    password = str(data.get('password') or '')
    if _limited('login|' + ip, 20, 600) or _limited('login-name|' + username, 10, 600):
        return _err(handler, 429, '尝试次数太多，请稍后再试')
    with _LOCK:
        row = _conn().execute('SELECT id, display, phc FROM users WHERE username=?', (username,)).fetchone()
    stored = row['phc'] if row else _DUMMY
    ok = _verify(password, stored)
    if not row or not ok:
        return _err(handler, 401, '用户名或密码不正确')
    return _issue(handler, row['id'], row['display'])


def _issue(handler, uid, display):
    token = _token()
    csrf = _token()
    now = int(time.time())
    with _LOCK:
        _conn().execute(
            'INSERT INTO sessions(user_id,token_hash,csrf,expires,created) VALUES(?,?,?,?,?)',
            (uid, _hash_token(token), csrf, now + SESSION_TTL, now))
    _ok(handler, {'id': uid, 'username': display, 'csrf': csrf}, _auth_cookies(token, SESSION_TTL))


def _logout(handler):
    row = _require(handler, True)
    if not row:
        return
    with _LOCK:
        _conn().execute('DELETE FROM sessions WHERE id=?', (row['sid'],))
    _ok(handler, {'ok': True}, _clear_cookies())


def _password(handler):
    row = _require(handler, True)
    if not row:
        return
    if _limited('pw|' + str(row['uid']), 5, 3600):
        return _err(handler, 429, '修改过于频繁，请稍后再试')
    data, err = _json_body(handler, 4096)
    if err:
        return _err(handler, err, '请求格式不正确')
    old = str(data.get('old') or '')
    new = str(data.get('new') or '')
    if len(new) < 10 or len(new) > 128:
        return _err(handler, 400, '密码至少 10 位，最长 128 位')
    with _LOCK:
        user = _conn().execute('SELECT phc FROM users WHERE id=?', (row['uid'],)).fetchone()
    if not user or not _verify(old, user['phc']):
        return _err(handler, 401, '原密码不正确')
    phc = _phc(new)
    with _LOCK:
        _conn().execute('UPDATE users SET phc=? WHERE id=?', (phc, row['uid']))
        _conn().execute('DELETE FROM sessions WHERE user_id=? AND id!=?', (row['uid'], row['sid']))
    _ok(handler, {'ok': True})


def _me(handler):
    row = _require(handler, False)
    if not row:
        return
    _ok(handler, _public_user(row))


def _state_get(handler):
    row = _require(handler, False)
    if not row:
        return
    with _LOCK:
        found = _conn().execute('SELECT body FROM states WHERE user_id=?', (row['uid'],)).fetchone()
    body = json.loads(found['body']) if found else {}
    if isinstance(body, dict):
        body.pop('key', None)
        if isinstance(body.get('wbCfg'), dict):
            body['wbCfg'].pop('key', None)
    _ok(handler, {'state': body})


def _state_put(handler):
    row = _require(handler, True)
    if not row:
        return
    data, err = _json_body(handler)
    if err:
        return _err(handler, err, '请求格式不正确')
    state = _clean_state(data.get('state') if isinstance(data.get('state'), dict) else data)
    if state is None:
        return _err(handler, 413, '账号数据太大')
    raw = json.dumps(state, ensure_ascii=False)
    now = int(time.time())
    with _LOCK:
        _conn().execute(
            'INSERT INTO states(user_id,body,updated) VALUES(?,?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET body=excluded.body, updated=excluded.updated',
            (row['uid'], raw, now))
    _ok(handler, {'ok': True})


def _history_list(handler):
    row = _require(handler, False)
    if not row:
        return
    with _LOCK:
        rows = _conn().execute(
            'SELECT body FROM history WHERE user_id=? ORDER BY t DESC LIMIT ?',
            (row['uid'], MAX_HISTORY)).fetchall()
    items = []
    for item in rows:
        try:
            items.append(json.loads(item['body']))
        except Exception:
            continue
    _ok(handler, {'items': items})


def _owns(user_id, blob_id):
    if not blob_id:
        return True
    if not BLOB_RE.fullmatch(str(blob_id)):
        return False
    with _LOCK:
        found = _conn().execute('SELECT user_id FROM blobs WHERE id=?', (blob_id,)).fetchone()
    return bool(found) and found['user_id'] == user_id


def _history_add(handler):
    row = _require(handler, True)
    if not row:
        return
    data, err = _json_body(handler, 64 * 1024)
    if err:
        return _err(handler, err, '请求格式不正确')
    try:
        stamp = float(data.get('t'))
    except (TypeError, ValueError):
        return _err(handler, 400, '记录格式不正确')
    after = str(data.get('after') or '')
    before = data.get('before') or None
    if before:
        before = str(before)
    if not _owns(row['uid'], after) or not _owns(row['uid'], before):
        return _err(handler, 404, '找不到这张图片')
    body = {
        't': stamp,
        'prov': str(data.get('prov') or '')[:40],
        'model': str(data.get('model') or '')[:80],
        'prompt': str(data.get('prompt') or '')[:8000],
        'orig': (str(data.get('orig'))[:8000] if data.get('orig') else None),
        'neutral': bool(data.get('neutral')),
        'mode': str(data.get('mode') or '')[:20],
        'label': str(data.get('label') or '')[:200],
        'after': after,
        'before': before,
    }
    for secret in ('key', 'authorization', 'cookie'):
        body.pop(secret, None)
    raw = json.dumps(body, ensure_ascii=False)
    now = int(time.time())
    with _LOCK:
        count = _conn().execute('SELECT COUNT(*) c FROM history WHERE user_id=?', (row['uid'],)).fetchone()['c']
        if count >= MAX_HISTORY:
            extra = _conn().execute(
                'SELECT id, body FROM history WHERE user_id=? ORDER BY t ASC LIMIT ?',
                (row['uid'], count - MAX_HISTORY + 1)).fetchall()
            for old in extra:
                _conn().execute('DELETE FROM history WHERE id=?', (old['id'],))
        _conn().execute(
            'INSERT INTO history(user_id,t,body,created) VALUES(?,?,?,?) '
            'ON CONFLICT(user_id,t) DO UPDATE SET body=excluded.body',
            (row['uid'], stamp, raw, now))
    _ok(handler, {'ok': True})


def _history_delete(handler, stamp):
    row = _require(handler, True)
    if not row:
        return
    try:
        value = float(stamp)
    except ValueError:
        return _err(handler, 404, '找不到这条记录')
    with _LOCK:
        cur = _conn().execute('DELETE FROM history WHERE user_id=? AND t=?', (row['uid'], value))
        deleted = cur.rowcount
    if not deleted:
        return _err(handler, 404, '找不到这条记录')
    _ok(handler, {'ok': True})


def _history_clear(handler):
    row = _require(handler, True)
    if not row:
        return
    with _LOCK:
        _conn().execute('DELETE FROM history WHERE user_id=?', (row['uid'],))
    _ok(handler, {'ok': True})


def _usage(user_id):
    with _LOCK:
        row = _conn().execute('SELECT COALESCE(SUM(size),0) n FROM blobs WHERE user_id=?', (user_id,)).fetchone()
    return int(row['n'])


def _blob_add(handler):
    row = _require(handler, True)
    if not row:
        return
    mime = (handler.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
    if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
        return _err(handler, 415, '只接收图片')
    try:
        length = int(handler.headers.get('Content-Length') or '0')
    except ValueError:
        return _err(handler, 400, '请求格式不正确')
    if length <= 0 or length > MAX_BLOB:
        return _err(handler, 413, '单张图片不能超过 8MB')
    if _usage(row['uid']) + length > MAX_USER_BYTES:
        return _err(handler, 413, '这个账号的私有图片空间已满')
    blob_id = os.urandom(16).hex()
    path = _blob_path(row['uid'], blob_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    left = length
    with open(path, 'wb') as fh:
        while left:
            chunk = handler.rfile.read(min(65536, left))
            if not chunk:
                break
            fh.write(chunk)
            left -= len(chunk)
    size = os.path.getsize(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if size != length:
        os.remove(path)
        return _err(handler, 400, '图片没有传完')
    with _LOCK:
        _conn().execute(
            'INSERT INTO blobs(id,user_id,mime,size,created) VALUES(?,?,?,?,?)',
            (blob_id, row['uid'], mime, size, int(time.time())))
    _ok(handler, {'id': blob_id})


def _blob_get(handler, blob_id):
    row = _require(handler, False)
    if not row:
        return
    path = _blob_path(row['uid'], blob_id)
    if not path:
        return _err(handler, 404, '找不到这张图片')
    with _LOCK:
        found = _conn().execute('SELECT mime, size, user_id FROM blobs WHERE id=?', (blob_id,)).fetchone()
    if not found or found['user_id'] != row['uid'] or not os.path.isfile(path):
        return _err(handler, 404, '找不到这张图片')
    handler.close_connection = True
    handler.send_response(200)
    handler.send_header('Content-Type', found['mime'])
    handler.send_header('Content-Length', str(found['size']))
    handler.send_header('Cache-Control', 'private, no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    if handler.command == 'HEAD':
        return
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)


def _blob_delete(handler, blob_id):
    row = _require(handler, True)
    if not row:
        return
    path = _blob_path(row['uid'], blob_id)
    with _LOCK:
        cur = _conn().execute('DELETE FROM blobs WHERE id=? AND user_id=?', (blob_id, row['uid']))
        deleted = cur.rowcount
    if not deleted or not path:
        return _err(handler, 404, '找不到这张图片')
    if os.path.isfile(path):
        os.remove(path)
    _ok(handler, {'ok': True})
