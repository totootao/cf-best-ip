# Cloudflare 优选 IP 自动写 Hosts 监控器（Docker）

监控一个「每行一个 IP」的文本文件，文件变化时自动把**第一行最优 IP** 写入
宿主机 `/etc/hosts` 的标记区块，使指定域名始终指向当前最优 IP。

例如 `TARGET_DOMAIN=cdn.example.com` 时，hosts 中维护：

```
# >>> cf-best-ip >>>
104.27.200.69    cdn.example.com
# <<< cf-best-ip <<<
```

---

## 1. 准备

1. 把你的优选 IP 列表放到某处，例如 `./ip_list.txt`（可直接复制 `ip_list.txt.example`）：

   ```bash
   cp ip_list.txt.example ip_list.txt
   # 编辑成你真实的优选 IP 列表
   ```

2. 编辑 `docker-compose.yml`：
   - 把 `TARGET_DOMAIN` 改成你真正要映射的域名（如 `cdn.example.com`）。
   - 把 `./ip_list.txt` 改成你服务器上 IP 列表文件的**真实绝对路径**（保持 `:ro` 只读即可）。

---

## 2. 部署（二选一）

### 方式 A：Docker Compose（推荐）

```bash
docker compose up -d --build
docker logs -f cf-best-ip-monitor
```

### 方式 B：直接 docker run（免构建）

```bash
docker run -d --name cf-best-ip-monitor --restart=unless-stopped \
  -e TARGET_DOMAIN=cdn.example.com \
  -e POLL_INTERVAL=10 \
  -v /etc/hosts:/host/hosts:rw \
  -v /你的路径/ip_list.txt:/data/ip_list.txt:ro \
  python:3.11-alpine \  # Alpine 环境用此镜像；Debian 用 python:3.11-slim
  python -c "$(curl -fsSL https://raw.githubusercontent.com/你的仓库/monitor.py)"
```

> 方式 B 需要把 `monitor.py` 放到可访问地址；更简单的做法是用方式 A 构建本地镜像。

---

## 3. 验证

```bash
# 改一下 IP 列表第一行，观察日志是否自动更新
echo "172.67.60.78" > ip_list.txt
docker logs -f cf-best-ip-monitor

# 查看宿主机 hosts 是否生效
grep cf-best-ip /etc/hosts
getent hosts cdn.example.com
```

---

## 4. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TARGET_DOMAIN` | 空（**必填**） | 要映射的域名 |
| `IP_FILE` | `/data/ip_list.txt` | 容器内 IP 列表路径 |
| `HOSTS_FILE` | `/host/hosts` | 容器内 hosts 路径（bind 到宿主机 `/etc/hosts`） |
| `POLL_INTERVAL` | `10` | 轮询间隔（秒） |

---

## 5. 注意事项

1. **挂载宿主机 `/etc/hosts` 有风险**：脚本只改写 `# >>> cf-best-ip >>>` 到 `# <<< cf-best-ip <<<` 之间的内容，其余不动；但请确认该区块不存在于你手动编辑的内容里。
2. **解析缓存**：若服务器用了 `systemd-resolved` 或 `dnsmasq`，改完 hosts 可能需刷新缓存：
   - `systemd-resolved`：`resolvectl flush-caches`
   - `dnsmasq`：`systemctl restart dnsmasq`（或 `pkill -HUP dnsmasq`）
   - 纯 glibc（nsswitch `hosts: files dns`）则立即生效。
3. **权限**：容器以 root 运行才能写 hosts；确保 Docker 守护进程对宿主机 `/etc/hosts` 有写权限（Linux 服务器通常 OK，Docker Desktop on Mac/Win 可能受限）。
4. **轮询而非 inotify**：跨 bind mount 文件变化事件不一定可靠，故采用「哈希 + mtime 轮询」，间隔可调，简单稳定。
5. **合规**：勿将优选 IP 用于违反 Cloudflare 服务条款的代理用途。

---

## 6. Alpine 适配

如果你的运行环境是 **Alpine Linux**（默认没装 Python3），本仓库提供三种契合方式：

1. **Alpine 版 Python 镜像（已默认改好）**
   `Dockerfile` 现已基于 `python:3.11-alpine`（镜像更小、与 Alpine 一致）。
   脚本是纯标准库，无需改动，直接 `docker compose up -d --build` 即可。
   若想换回 Debian 基础，把 `FROM` 改回 `python:3.11-slim` 即可。

2. **纯 shell 版（推荐，零 Python 依赖）**
   用 `monitor.sh`（busybox `ash` 实现，逻辑与 Python 版完全一致），无需 `apk add python3`：

   ```bash
   # 在 Alpine 宿主机直接跑（HOSTS_FILE 直接指向宿主机 /etc/hosts）
   TARGET_DOMAIN=cdn.example.com \
   IP_FILE=/root/ip_list.txt HOSTS_FILE=/etc/hosts POLL_INTERVAL=10 \
   sh /path/monitor.sh
   ```
   或用极小镜像 `Dockerfile.alpine-shell`：
   ```bash
   docker compose -f docker-compose.alpine-shell.yml up -d --build
   ```

3. **Alpine 裸跑做系统服务（OpenRC）**
   把 `monitor.sh` 作为 OpenRC 服务常驻：
   ```bash
   cp monitor.sh /usr/local/bin/cf-best-ip-monitor.sh
   chmod +x /usr/local/bin/cf-best-ip-monitor.sh
   # 写一个 /etc/init.d/cf-best-ip 包装脚本，然后：
   rc-service cf-best-ip start
   rc-update add cf-best-ip
   ```
   裸跑时 `HOSTS_FILE` 直接指向宿主机 `/etc/hosts`，无需 bind mount。

> Alpine 的 musl 解析器会读取 `/etc/hosts`，改完通常立即生效；若叠加了 `dnsmasq`，
> 执行 `rc-service dnsmasq restart`（或 `pkill -HUP dnsmasq`）刷新缓存。

---

## 7. Docker Hub 镜像（GitHub Actions 自动构建）

仓库已配置 `.github/workflows/docker.yml`：push 到 `main` 或打 `v*` tag 时，自动构建并推送到 Docker Hub：

- `tootootao/cf-best-ip:latest` —— 基于 `Dockerfile`（python:3.11-alpine，含 `monitor.py`）
- `tootootao/cf-best-ip:shell`  —— 基于 `Dockerfile.alpine-shell`（纯 busybox `ash`，零 Python 依赖）

拉取：

```bash
docker pull too tao/cf-best-ip:latest
docker pull too tao/cf-best-ip:shell
```

> 构建所需的 Docker Hub 凭据通过仓库 **Secrets**（`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`）提供，不会出现在代码里。
> 注：Docker Hub 的 `password` 字段实际要求是 **Access Token**（非账号登录密码）；若用登录密码登录失败，请到 Docker Hub → Account Settings → Security → Access Tokens 生成一个 Token 填入 `DOCKERHUB_TOKEN`。
