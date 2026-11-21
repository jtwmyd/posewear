#!/usr/bin/env python3
"""把账号相关的前端写进已有 dist，不重新生成图片和详情页。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
DIST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dist')


def h8(data):
    return hashlib.sha256(data).hexdigest()[:10]


def put(name, data):
    base, ext = name.rsplit('.', 1)
    rel = 'assets/%s.%s.%s' % (base, h8(data), ext)
    dest = os.path.join(DIST, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, 'wb').write(data)
    return rel


def rewrite_app(data, index):
    text = data.decode('utf-8')
    import glob
    for name in ('poses.json', 'costumes.json', 'anime.json', 'portraits.json', 'depths.json'):
        key = name.split('.')[0]
        hits = glob.glob(os.path.join(DIST, 'assets', key + '.*.json'))
        if len(hits) != 1:
            sys.exit('%s 的数据文件数量是 %d' % (name, len(hits)))
        rel = '/assets/' + os.path.basename(hits[0])
        text = text.replace("'" + name + "'", "'" + rel + "'")
    return text.encode('utf-8')


def main():
    index_path = os.path.join(DIST, 'index.html')
    index = open(index_path, encoding='utf-8').read()
    mapping = {}
    for name in ('account.js', 'app.js', 'studio.js', 'extras.js', 'style.css'):
        data = open(os.path.join(SRC, name), 'rb').read()
        if name == 'app.js':
            data = rewrite_app(data, index)
        mapping[name] = put(name, data)
    # 保留未改动的 zip.js
    found = re.search(r'/assets/zip\.[0-9a-f]+\.js', index)
    if not found:
        sys.exit('dist/index.html 里没有 zip.js')
    mapping['zip.js'] = found.group(0).lstrip('/')
    index = re.sub(r'/assets/style\.[0-9a-f]+\.css', '/' + mapping['style.css'], index)
    if 'id="acctBtn"' not in index:
        index = index.replace(
            '<div class="tools"><button id="adultSw"',
            '<div class="tools"><button id="acctBtn" class="sm" type="button">登录</button><button id="adultSw"',
            1)
    if 'id="acctBox"' not in index:
        dialogs = open(os.path.join(SRC, 'index.html'), encoding='utf-8').read()
        start = dialogs.index('<div id="acctBox"')
        end = dialogs.index('<div id="toast"')
        index = index.replace('<div id="toast" role="status"></div>', dialogs[start:end] + '<div id="toast" role="status"></div>', 1)
    old_note = '<div class="note"><b>密钥</b>只存在这个浏览器里，请求直接发给你填的地址。用公共电脑记得清除。</div>'
    new_note = '<div class="note"><b>密钥</b>只留在这台设备上，不会上传。换一台设备要重新填写。用公共电脑记得清除。</div>'
    index = index.replace(old_note, new_note)
    index = index.replace(
        '<li>收藏和密钥只存在本机</li>',
        '<li>未登录时收藏在本机。登录后收藏、记录按账号分开，密钥仍只留在这台设备</li>')
    old_priv = '<li><b>本机存储</b>：收藏、浏览记录、筛选偏好、成人内容开关和试穿记录只保存在你的浏览器（localStorage / IndexedDB），不上传。清除浏览器数据即可删除。</li><li><b>API 密钥</b>：只保存在本机。生成时由浏览器直接发给你选择的服务商；开启「通过本站代理转发」或使用自定义中转时，请求会经过本站的转发接口，只转发、不记录、不保存。</li><li><b>代理限制</b>：转发接口只接受本站页面发起的请求，只能访问官方接口地址。</li><li><b>统计</b>：本站不使用统计、广告或第三方追踪脚本，不设置 Cookie。</li>'
    new_priv = '<li><b>账号</b>：登录后，收藏、浏览记录、试穿偏好和生成图片保存在这个账号下，别的账号看不到。未登录时这些仍只在本机，不会自动上传。退出或换账号会清掉当前页面上的内容。</li><li><b>API 密钥</b>：只保存在这台设备，服务器不保存。换一台设备需要重新填写。生成时由浏览器发给你选择的服务商；走本站转发时只在当次请求里使用，不写入数据库或日志。</li><li><b>代理限制</b>：转发接口只接受本站 HTTPS 页面发起的请求，只能访问官方接口地址，并拒绝内网目标。</li><li><b>Cookie</b>：登录后会有一个仅用于会话的 Cookie。素材库页面本身不靠它识别你。</li>'
    if old_priv in index:
        index = index.replace(old_priv, new_priv)
    scripts = ''.join('<script src="/%s" defer></script>' % mapping[name] for name in ('account.js', 'app.js', 'zip.js', 'extras.js', 'studio.js'))
    index2, n = re.subn(r'<script src="/assets/(?:account|app|zip|extras|studio)\.[0-9a-f]+\.js" defer></script>', '', index)
    if n < 4:
        sys.exit('脚本标签替换数量不对：%d' % n)
    index = index2.replace('</body>', scripts + '</body>', 1)
    open(index_path, 'w', encoding='utf-8').write(index)
    old_sw = open(os.path.join(DIST, 'sw.js'), encoding='utf-8').read()
    assets = ['/', '/favicon.svg', '/' + mapping['style.css'], '/' + mapping['account.js'], '/' + mapping['app.js'], '/' + mapping['studio.js'], '/' + mapping['extras.js'], '/' + mapping['zip.js']]
    for old in re.findall(r'/assets/(?:poses|depths)\.[0-9a-f]+\.json', old_sw):
        if old not in assets:
            assets.append(old)
    sw = (
        "const V='posewear-acct-%s';self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(V).then(c=>c.addAll(%s)))});\n"
        "self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!=V).map(k=>caches.delete(k))))));\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.origin!=location.origin||u.pathname.startsWith('/api/')||e.request.method!='GET')return;\n"
        " const img=/\\/(p|c)\\/(t|i|m)\\//.test(u.pathname);e.respondWith(img?caches.match(e.request).then(r=>r||fetch(e.request).then(res=>{const cl=res.clone();caches.open(V+'-img').then(c=>c.put(e.request,cl));return res}))\n"
        " :fetch(e.request).then(res=>{const cl=res.clone();caches.open(V).then(c=>c.put(e.request,cl));return res}).catch(()=>caches.match(e.request)))});\n"
    ) % (h8(scripts.encode()), json_list(assets))
    open(os.path.join(DIST, 'sw.js'), 'w', encoding='utf-8').write(sw)
    print('\n'.join('%s %s' % (k, v) for k, v in mapping.items()))


def json_list(items):
    return '[' + ', '.join(json_quote(x) for x in items) + ']'


def json_quote(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


if __name__ == '__main__':
    main()
