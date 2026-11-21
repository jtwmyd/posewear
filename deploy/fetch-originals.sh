#!/bin/sh
# 用 curl 从官方图库补齐 dist/o 下的原图 PNG。不需要 pip、git 或 node。
# 清单里的地址只来自四家公开图库，脚本不会访问其它主机。
set -eu
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
DEST=${1:-$HERE/dist}
LIST=$HERE/originals.tsv
if [ ! -f "$LIST" ]; then
  echo "找不到 $LIST" >&2
  exit 1
fi
ok=0
skip=0
fail=0
while IFS=$(printf '\t') read -r url rel bytes; do
  case "$url" in
    ''|\#*) continue ;;
  esac
  case "$url" in
    https://ai-pose-gallery.pages.dev/*|https://ai-costume-gallery.pages.dev/*|https://ai-costume-anime-gallery.pages.dev/*|https://ai-portrait-gallery.pages.dev/*) ;;
    *) echo "拒绝非官方地址：$url" >&2; exit 1 ;;
  esac
  out="$DEST/$rel"
  if [ -f "$out" ]; then
    sz=$(stat -c%s "$out")
    if [ "$sz" = "$bytes" ] && [ "$bytes" != "0" ]; then
      skip=$((skip + 1))
      continue
    fi
  fi
  mkdir -p "$(dirname "$out")"
  if curl -fsSL --retry 4 --retry-delay 2 -A "PoseWear local build" --max-time 120 -o "$out.part" "$url"; then
    mv "$out.part" "$out"
    ok=$((ok + 1))
  else
    rm -f "$out.part"
    echo "失败 $url" >&2
    fail=$((fail + 1))
  fi
done < "$LIST"
echo "下载 $ok，已存在 $skip，失败 $fail"
[ "$fail" -eq 0 ]
