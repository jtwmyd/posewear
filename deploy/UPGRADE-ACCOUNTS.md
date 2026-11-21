# 账号功能增量升级

这次只替换运行代码和页面脚本，不替换 `dist` 里的图片、详情页和 JSON 素材。数据库和私有图片在 `/var/lib/posewear`，不要放进 `dist`，也不要打进备份后公开。

站点仍监听 80。账号、改密和携带密钥的 `/api/proxy` 只在 `https://176.122.145.34:8443` 上接受。8443 没有证书时，这些接口返回 403，图库页面继续用原来的 HTTP。不要把服务绑到 443，也不要动 41080。

## 先备份

```bash
sudo systemctl stop posewear
sudo tar -C / -cf /root/posewear-before-accounts.tar \
  opt/posewear/serve.py opt/posewear/dist/index.html opt/posewear/dist/sw.js \
  opt/posewear/dist/assets opt/posewear/posewear.service.in \
  etc/systemd/system/posewear.service
sudo systemctl start posewear
```

还没有数据库。升级后再出现的 `/var/lib/posewear` 才需要以后备份：

```bash
sudo tar -C / -cf /root/posewear-var.tar var/lib/posewear
```

这个包里有密码散列和用户图片，不要放到公开目录，不要把日志一并发出去。

## 替换这些文件

解压增量包后，覆盖到 `/opt/posewear`（`--strip-components=1`）。需要换掉的是：

| 路径 | 作用 |
|---|---|
| `serve.py` `account.py` | 服务和账号 |
| `posewear.service.in` | 单元模板 |
| `dist/index.html` `dist/sw.js` | 页面和离线脚本 |
| `dist/assets/account.*.js` `app.*.js` `studio.*.js` `extras.*.js` `style.*.css` | 新的前端。旧的带哈希文件可以留着，页面不再引用 |

图片目录 `dist/p`、`dist/c`、`dist/a`、`dist/r`、`dist/o` 和原来的 `poses.*.json` 都不要删。

然后安装单元并重启本服务：

```bash
sudo mkdir -p /var/lib/posewear/tls /var/lib/posewear/acme-webroot
sudo chown -R posewear:posewear /var/lib/posewear
sudo chmod 700 /var/lib/posewear
sudo sed -e 's|__PREFIX__|/opt/posewear|g' -e 's|__PORT__|80|g' -e 's|__ORIGIN__|https://176.122.145.34:8443|g' \
  /opt/posewear/posewear.service.in | sudo tee /etc/systemd/system/posewear.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart posewear
```

`systemctl restart posewear` 只重启这个单元。不要 restart xray，也不要改 443 的监听。

此时 80 上的图库应仍可打开。`https://176.122.145.34:8443` 还没有证书，登录按钮会说明接口已关闭，直接向 80 提交密码会得到 403。

## 8443 上的受信证书

用 Let’s Encrypt 给 IP `176.122.145.34` 签的短有效期证书。2026 年起这种证书已公开签发，有效期约 6 天（160 小时），必须用 `shortlived` 配置，验证用 80 端口的 HTTP-01。不需要域名，也不改任何 DNS。证书签好后，浏览器访问 `https://176.122.145.34:8443` 会认这张证书，不要换成自签证书，也不要让访问者忽略警告。

服务器上的 Certbot 需要 5.4 或更新（Ubuntu 22.04 自带的版本不够）。用 snap 安装，不使用 pip：

```bash
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot
sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/lib/posewear/acme-webroot \
  --ip-address 176.122.145.34 \
  --agree-tos -m YOU@example.com --no-eff-email
```

先把邮箱换成运维自己的地址。签发成功后，证书在 `/etc/letsencrypt/live/176.122.145.34/`。复制到服务可读的位置，私钥不要进 `dist`：

```bash
sudo install -d -o root -g posewear -m 750 /var/lib/posewear/tls
sudo install -o root -g posewear -m 644 /etc/letsencrypt/live/176.122.145.34/fullchain.pem /var/lib/posewear/tls/fullchain.pem
sudo install -o root -g posewear -m 640 /etc/letsencrypt/live/176.122.145.34/privkey.pem /var/lib/posewear/tls/privkey.pem
sudo systemctl restart posewear
```

续期必须自动做。证书大约 6 天过期，certbot 的定时器每天跑两次即可。部署钩子只复制证书并重启 posewear：

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/posewear.sh >/dev/null << 'EOF'
#!/bin/sh
set -eu
install -o root -g posewear -m 644 /etc/letsencrypt/live/176.122.145.34/fullchain.pem /var/lib/posewear/tls/fullchain.pem
install -o root -g posewear -m 640 /etc/letsencrypt/live/176.122.145.34/privkey.pem /var/lib/posewear/tls/privkey.pem
systemctl restart posewear
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/posewear.sh
```

确认续期演练：`sudo certbot renew --dry-run`。这个演练不会动 443。

登录地址固定为 `https://176.122.145.34:8443/`。80 端口继续提供图库和 ACME 挑战，不要把 80 整站跳转到 443。

## 账号怎么隔离

- 注册、登录、退出、修改密码。没有邮箱，也没有预置账号。
- 收藏、浏览记录、试穿偏好、生成图片按账号存在 `/var/lib/posewear`。别的账号访问返回 404 或看不到。
- API 密钥只留在登录过的那台浏览器里，请求时临时使用，不写入数据库。换一台设备要重新填写。
- 未登录时已有的收藏和生成记录不会自动上传。登录后页面会问是导入当前账号，还是留在本机。
- 退出或换账号会清掉当前页面缓存并重新加载，避免串号。

## 回滚

```bash
sudo systemctl stop posewear
sudo tar -C / -xf /root/posewear-before-accounts.tar
sudo systemctl daemon-reload
sudo systemctl start posewear
```

回滚后 80 上的旧站恢复。8443 不再由这个进程监听。`/var/lib/posewear` 可以留着，旧程序不会读它。证书文件不用删；不启动 8443 就不会用到。不要为了回滚去停 xray。
