#!/usr/bin/env python3
"""根据官方图库的元数据写出原图清单：URL、站内相对路径、字节数。

只列原图 PNG。缩略图和页面用的 WebP 由 build.py 生成，不能用这份清单代替。
"""
from __future__ import annotations

import json
import os
import sys

RAW = os.environ.get('POSEWEAR_RAW', 'assets-raw')
SRC = {
    'pose': 'https://ai-pose-gallery.pages.dev/',
    'costume': 'https://ai-costume-gallery.pages.dev/',
    'anime': 'https://ai-costume-anime-gallery.pages.dev/',
    'portrait': 'https://ai-portrait-gallery.pages.dev/',
}


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return 0


def rows():
    pose = json.load(open(os.path.join(RAW, 'pose', 'data.json'), encoding='utf-8'))['D']
    costume = json.load(open(os.path.join(RAW, 'costume', 'data.json'), encoding='utf-8'))['D']
    anime = json.load(open(os.path.join(RAW, 'anime', 'data.json'), encoding='utf-8'))['D']
    text = open(os.path.join(RAW, 'portrait', 'images.js'), encoding='utf-8').read()
    portrait = json.loads(text[text.index('['):text.rindex(']') + 1])
    out = []
    for item in pose:
        rel = 'o/p/%s.png' % item['id']
        out.append((SRC['pose'] + item['原寸'], rel, size_of(os.path.join(RAW, 'pose', item['原寸']))))
    for item in costume:
        rel = 'o/c/%s.png' % item['id']
        out.append((SRC['costume'] + item['原寸'], rel, size_of(os.path.join(RAW, 'costume', item['原寸']))))
    for item in anime:
        rel = 'o/a/%s.png' % item['id']
        out.append((SRC['anime'] + item['原寸'], rel, size_of(os.path.join(RAW, 'anime', item['原寸']))))
    for item in portrait:
        rel = 'o/r/%s.png' % item['id']
        out.append((SRC['portrait'] + item['full'], rel, size_of(os.path.join(RAW, 'portrait', item['full']))))
    return out


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else ''
    lines = ['# url\trelative-to-dist\tbytes']
    total = 0
    for url, rel, nbytes in rows():
        lines.append('%s\t%s\t%d' % (url, rel, nbytes))
        total += nbytes
    text = '\n'.join(lines) + '\n'
    if dest:
        open(dest, 'w', encoding='utf-8').write(text)
    else:
        sys.stdout.write(text)
    print('files %d bytes %d' % (len(lines) - 1, total), file=sys.stderr)


if __name__ == '__main__':
    main()
