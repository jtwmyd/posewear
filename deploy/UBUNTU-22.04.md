# 衣姿 PoseWear · Ubuntu 22.04 纯 HTTP 自托管

这份说明只覆盖把已经构建好的站点放到一台 Ubuntu 22.04 上，用系统自带的 Python 3.10 提供页面、素材和同源 `/api/proxy`。不需要 node、nginx、Docker、git 或 pip。

目标机器按这次的条件准备：3 vCPU、2 GiB 内存、几十 GB 空闲磁盘，已有 Python 3.10、curl、rsync。**443 上已有别的程序，安装和运行都不会绑定、重启或改写它。** 站点用 80 端口的纯 HTTP，没有域名：

`http://176.122.145.34/`

预构建页面里的 canonical、Open Graph 和 sitemap 都写的是这个地址。改端口可以跑起来，但这些绝对地址不会自动跟着变。

## 包里有什么

解压后的目录：

| 路径 | 作用 |
|---|---|
| `serve.py` | 静态文件 + `/api/proxy`。只依赖 Python 3.10 标准库 |
| `dist/` | 构建好的页面、脚本、缩略图、展示用 WebP、详情页 |
| `dist/o/` | 原图 PNG。若这个发布拆成两个压缩包，原图在第二个包里，解到同一目录 |
| `originals.tsv` | 原图的官方 URL、相对路径和字节数 |
| `fetch-originals.sh` | 只用 curl，按 `originals.tsv` 从四家官方图库补原图 |
| `posewear.service.in` | systemd 单元模板 |
| `install.sh` | 建立系统用户并安装单元。不装包，不碰 443 |
| `SHA256SUMS` | 发布文件的校验和 |

素材来源（只读这些站，深度姿势的图仍然由浏览器直接热链原站，不进本包）：

- 姿势：<https://ai-pose-gallery.pages.dev/>
- 服装：<https://ai-costume-gallery.pages.dev/>
- 动画服装：<https://ai-costume-anime-gallery.pages.dev/>
- 人像：<https://ai-portrait-gallery.pages.dev/>
- 深度姿势（热链，不下载）：<https://ai-pose-depth-gallery.pages.dev/>

人像为 CC0，姿势和服装的版权在原作者。页面上的来源说明保持原样。

## 上传

发布物是两个未压缩的 tar。应用包约 609MB，原图包约 6.9GB。若下载端把它们拆成不超过 100MB 的分卷，文件名是 `posewear-selfhost-app.tar.part-000` 这种序号，先在你自己的电脑上拼回去再校验：

```bash
cat posewear-selfhost-app.tar.part-* > posewear-selfhost-app.tar
cat posewear-selfhost-originals.tar.part-* > posewear-selfhost-originals.tar
sha256sum -c SHA256SUMS
scp posewear-selfhost-app.tar posewear-selfhost-originals.tar root@176.122.145.34:/tmp/
```

没有分卷时，直接对这两个 tar 做 `sha256sum -c SHA256SUMS`。服务器上不需要 git。

在服务器上：

```bash
sudo mkdir -p /opt/posewear
sudo tar -xf /tmp/posewear-selfhost-app.tar -C /opt/posewear --strip-components=1
sudo tar -xf /tmp/posewear-selfhost-originals.tar -C /opt/posewear --strip-components=1
cd /opt/posewear
sudo bash install.sh
```

`install.sh` 会：

1. 拒绝 `POSEWEAR_PORT=443`，也拒绝公开地址里出现 `:443`
2. 建立系统用户 `posewear`（若还没有）
3. 安装 `/etc/systemd/system/posewear.service`
4. `systemctl enable --now posewear`

它不会执行 apt，不会改防火墙里 443 的规则，也不会 systemctl 其他服务。

换一个非 443 端口时（例如 8080）：

```bash
sudo POSEWEAR_PORT=8080 POSEWEAR_PUBLIC_ORIGIN=http://176.122.145.34:8080 bash install.sh
```

页面功能用的是相对路径，换端口后图库和代理仍然同源。canonical / sitemap 仍是构建时写死的 `http://176.122.145.34`（80 端口）。

## 服务行为

- 监听 `0.0.0.0:$POSEWEAR_PORT`，默认 80。代码里端口 443 会直接退出。
- `/`、`/pose/…/`、`/costume/…/`、`/anime/…/`、`/portrait/…/`、`/en/`、`/tag/` 提供构建好的 HTML。
- `/p/`、`/c/`、`/a/`、`/r/` 是展示图，`/o/` 是原图 PNG。
- 找不到的路径返回站内 404 页。
- `_headers` 里的缓存和 CSP 会带上，但 **HSTS 和 `upgrade-insecure-requests` 一律去掉**。纯 HTTP 若发出这两项，浏览器会改去连 443，打到已经在用的程序上，本站也会挂。
- `/api/proxy` 与仓库里的 `functions/api/proxy.js` 同一边界，并额外做了地址解析：
  - 只接受 GET / POST；必须带本站 Origin 或 Referer，且 Host 等于 `POSEWEAR_PUBLIC_ORIGIN`
  - 只转发 `https` 的 443，拒绝用户名、密码和其它端口
  - 官方域名只允许原来的图片/模型路径（OpenAI、Gemini、火山方舟 `volces.com` / `bytepluses.com`）
  - 自定义中转必须带 `x-proxy-custom: 1`，路径也必须是图片生成或模型路径
  - 结果图只允许原来的官方结果域名
  - 目标若是内网、回环、链路本地、元数据地址、保留地址、组播，或 DNS 解析进这些网段（含 `127.0.0.0/8`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、`100.64/10`、IPv6 本地地址），直接 403，不连接
  - 不跟随重定向，响应里不回传 `Location`
  - 每个 IP 每 60 秒最多 20 次接口转发、40 次结果图转发，超出 429
  - 不记录、不保存密钥和图片，日志里只有方法、路径和状态码
  - 请求体上限 64 MiB，上游响应上限 80 MiB，避免把 2 GiB 内存打满

这不是开放代理：任意网站、任意路径、内网地址都不会被转发。自定义中转仍然可用，这是原站就有的功能，限制与上面相同。

## 部署后自测

```bash
curl -fsS -D- http://127.0.0.1/ -o /dev/null | head -n 20
curl -fsS -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1/favicon.svg
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1/no-such
curl -sS -D- -o /tmp/proxy.json -H 'Origin: http://176.122.145.34' \
  -H 'x-target-url: https://127.0.0.1/v1/images/generations' \
  -H 'x-proxy-custom: 1' http://127.0.0.1/api/proxy
curl -sS -D- -o /tmp/nokey.json -H 'Origin: http://176.122.145.34' \
  -H 'x-target-url: https://api.openai.com/v1/models' \
  http://127.0.0.1/api/proxy
```

期望：首页 200 且响应头里没有 `Strict-Transport-Security`，CSP 里没有 `upgrade-insecure-requests`；不存在的路径是 404；打向 `127.0.0.1` 的代理是 403；无密钥访问官方模型列表会得到官方的 401 或 403，而不是本站的 200。无密钥失败只说明转发没有附带密钥，**不能**说明试穿生成已经成功。

查看服务：

```bash
systemctl status posewear --no-pager
journalctl -u posewear -n 50 --no-pager
```

## 纯 HTTP 下浏览器做不到的事

`http://176.122.145.34` 不是安全上下文（localhost 才是例外）。下面这些是浏览器的限制，不是页面坏了：

- 服务工作线程注册会失败，离线缓存和「安装到桌面」不可用。注册失败已被页面自己吃掉，在线浏览不受影响。
- 复制文字会走页面里原有的回退；复制图片到剪贴板需要安全上下文，浏览器会走到页面原来的提示「当前浏览器不支持复制图片」。
- 密钥、收藏、最近浏览、生成记录仍写在这台浏览器的 localStorage / IndexedDB 里，这一点和原来一样。
- 深度姿势图是 https 热链，浏览器可以直接加载，不经过本机代理。
- 从 http 页面 `fetch` https 的官方接口是允许的。豆包 Seedream 仍然默认走同源 `/api/proxy`，因为官方接口不允许浏览器跨域，这和原站一致。

没有用真实 API Key 跑通过试穿。构建和下面的自动检查只覆盖了页面、素材文件，以及代理在没有密钥时的鉴权失败和恶意地址拦截。

## 原图如果没放进压缩包

展示用的 WebP 已经在应用包的 `dist/` 里，图库可以浏览。原图下载按钮指向 `/o/...png`，那 5665 个 PNG 合计约 6.9GB（7,385,004,829 字节），在原图包里。若原图包没有一起上传，用官方地址补：

```bash
cd /opt/posewear
sh fetch-originals.sh /opt/posewear/dist
```

脚本只接受清单里那四个官方主机名。官方原图合计约数 GB，服务器需要留出相应磁盘。

## 更新

用新的发布包覆盖 `/opt/posewear` 后：

```bash
sudo systemctl restart posewear
```

不要为了更新去动 443 上的程序。
