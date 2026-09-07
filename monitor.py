#!/usr/bin/env python3
"""
Cloudflare 优选 IP 监控器（Docker 版）

功能：
  持续监控一个优选 IP 列表文件。当文件内容变化时，读取其中的最优 IP
  （第一行合法 IP，支持 IPv4 / IPv6），并把该 IP 映射到一批域名，
  写入宿主机 /etc/hosts 的标记区块。

域名来源（三者可叠加，自动去重）：
  1. TARGET_DOMAIN   单个域名（向后兼容）
  2. TARGET_DOMAINS  多个域名，逗号或空格分隔
  3. CF_API_TOKEN    Cloudflare API Token，自动发现该账号下所有
                     Workers 与 Pages 域名（含自定义域）

IP 列表兼容格式：
  104.27.200.69                          裸 IP
  91.110.174.190:8443#38.27MB/s-HKG-HK   带端口 + 备注（优选工具常见导出）
  2606:4700::1111                        IPv6

设计要点：
  - 纯标准库，无需第三方依赖。
  - 轮询（内容哈希）而非 inotify，跨 bind mount / 网络存储都可靠。
  - 只改写标记区块，绝不触碰 hosts 文件其余内容。
  - 内容无变化时不写入，避免无意义的文件改动。
  - Cloudflare 域名发现失败时降级为手动域名，不影响主流程。
  - CF 接口失败采用指数退避重试，不做无意义的高频轮询。
"""

import os
import re
import sys
import time
import json
import logging
import hashlib
import ipaddress
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# 配置（均可用环境变量覆盖）
# ---------------------------------------------------------------------------
IP_FILE       = os.environ.get("IP_FILE", "/data/ip_list.txt")
HOSTS_FILE    = os.environ.get("HOSTS_FILE", "/host/hosts")
# 同时写入多个 hosts 文件（逗号或空格分隔）。用于把映射同步给宿主机上的
# 其他 Docker 容器：把 /var/lib/docker/containers/<id>/hosts 挂进来即可。
# 设置后以此为准；留空则只用 HOSTS_FILE。
HOSTS_FILES   = os.environ.get("HOSTS_FILES", "")
TARGET_DOMAIN  = os.environ.get("TARGET_DOMAIN", "")      # 单个域名（兼容旧配置）
TARGET_DOMAINS = os.environ.get("TARGET_DOMAINS", "")     # 多域名，逗号/空格分隔
CF_API_TOKEN   = os.environ.get("CF_API_TOKEN", "")       # Cloudflare API Token（可选）
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "10"))        # IP 文件轮询间隔（秒）
CF_REFRESH_INTERVAL = float(os.environ.get("CF_REFRESH_INTERVAL", "3600"))  # 域名列表刷新间隔（秒）
CF_BACKOFF_BASE     = float(os.environ.get("CF_BACKOFF_BASE", "30"))        # 失败后首次重试等待（秒）
CF_BACKOFF_MAX      = float(os.environ.get("CF_BACKOFF_MAX", "900"))        # 退避上限（秒）
DISCOVER_ONLY  = os.environ.get("DISCOVER_ONLY", "")      # 只拉取一次域名并打印，用于诊断

CF_API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4")

BLOCK_BEGIN = "# >>> cf-best-ip >>>"
BLOCK_END   = "# <<< cf-best-ip <<<"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-best-ip")


class CFError(Exception):
    """Cloudflare API 调用失败。"""


def file_hash(path: str):
    """返回文件内容的 MD5；文件不存在返回 None。"""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def _normalize_ip(token: str):
    """把可能带端口/备注的字段规整为纯 IP，失败返回 None。

    兼容格式：
      104.27.200.69                          裸 IP
      91.110.174.190:8443#38.27MB/s-HKG-HK   带端口 + 备注
      2606:4700::1111                        IPv6
    """
    token = token.strip()
    if not token or token.startswith("#"):
        return None
    token = token.split("#", 1)[0].strip()          # 去掉 # 及其后的备注
    if not token:
        return None
    try:                                            # 整体尝试（裸 IPv4 / IPv6）
        return str(ipaddress.ip_address(token))
    except ValueError:
        pass
    if ":" in token:                                # 形如 IP:端口 -> 取冒号前部分
        prefix = token.split(":", 1)[0]
        try:
            return str(ipaddress.ip_address(prefix))
        except ValueError:
            pass
    return None


def read_best_ip(path: str):
    """读取文件中第一行合法 IP（IPv4 或 IPv6），无则返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                tokens = line.split()
                if not tokens:
                    continue
                ip = _normalize_ip(tokens[0])
                if ip:
                    return ip
    except FileNotFoundError:
        pass
    return None


# ---------------------------------------------------------------------------
# Cloudflare 域名发现（Workers + Pages）
# ---------------------------------------------------------------------------
def _cf_err_detail(body: str) -> str:
    """从 Cloudflare 错误响应里提取可读信息（errors[].code / message）。"""
    try:
        data = json.loads(body)
    except Exception:
        return (body or "").strip()[:200]
    errs = data.get("errors") or []
    if errs:
        parts = []
        for e in errs[:3]:
            code = e.get("code")
            msg = e.get("message") or ""
            parts.append("%s %s" % (code, msg) if code else msg)
        return "; ".join(p for p in parts if p.strip())
    return (body or "").strip()[:200]


def _cf_get(path: str, token: str, timeout: int = 20):
    """调用 Cloudflare API，返回解析后的 JSON；失败抛 CFError（含 CF 错误码）。"""
    req = urllib.request.Request(CF_API + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        raise CFError("HTTP %s | %s" % (exc.code, _cf_err_detail(body)))
    except urllib.error.URLError as exc:
        raise CFError("网络错误 %s" % exc.reason)
    except Exception as exc:
        raise CFError("%s" % exc)


def _cf_paged(base: str, token: str, page_size: int = 50, max_pages: int = 100):
    """分页拉取列表型端点。

    各端点 per_page 上限不一（Pages 较小），遇到 400 自动去掉 per_page 重试。
    """
    items, page, use_pp = [], 1, True
    while page <= max_pages:
        sep = "&" if "?" in base else "?"
        path = "%s%spage=%d&per_page=%d" % (base, sep, page, page_size) if use_pp \
            else "%s%spage=%d" % (base, sep, page)
        try:
            data = _cf_get(path, token)
        except CFError as exc:
            if "HTTP 400" in str(exc) and use_pp:
                use_pp = False               # per_page 不被接受，退回默认分页
                continue
            raise
        chunk = data.get("result") or []
        items += chunk
        info = data.get("result_info") or {}
        total_pages = info.get("total_pages") or 1
        if not chunk or page >= total_pages:
            break
        page += 1
    return items


def discover_cf_domains(token: str):
    """发现账号下所有 Workers 与 Pages 域名；返回 (域名集合, 因权限缺失的来源列表)。"""
    domains, missing = set(), set()

    try:
        accounts = _cf_get("/accounts?per_page=50", token).get("result", [])
    except CFError as exc:
        log.error("获取 Cloudflare 账号列表失败：%s", exc)
        return domains, sorted(missing)

    if not accounts:
        log.warning("Cloudflare 账号列表为空（Token 可能未授权任何账号）")

    for acct in accounts:
        aid = acct.get("id")
        if not aid:
            continue
        aname = acct.get("name", aid)

        # 1) workers.dev 子域 -> 需要 Workers Scripts:Read
        workers_sub = None
        try:
            res = _cf_get("/accounts/%s/workers/subdomain" % aid, token).get("result") or {}
            workers_sub = res.get("subdomain")
        except CFError as exc:
            log.warning("[%s] 读取 workers.dev 子域失败：%s", aname, exc)
            if "HTTP 403" in str(exc):
                missing.add("workers.dev 子域（需 Workers Scripts:Read）")

        # 2) Workers 脚本 -> {script}.{subdomain}.workers.dev
        if workers_sub:
            try:
                for s in _cf_paged("/accounts/%s/workers/scripts" % aid, token):
                    name = s.get("id")
                    if name:
                        domains.add("%s.%s.workers.dev" % (name, workers_sub))
            except CFError as exc:
                log.warning("[%s] 读取 Workers 脚本列表失败：%s", aname, exc)

        # 3) Workers 自定义域 -> 需要 Workers Routes:Read
        try:
            for d in _cf_paged("/accounts/%s/workers/domains" % aid, token):
                host = d.get("hostname") or d.get("domain") or d.get("name")
                if host:
                    domains.add(host)
        except CFError as exc:
            log.warning("[%s] 读取 Workers 自定义域失败：%s", aname, exc)
            if "HTTP 403" in str(exc):
                missing.add("Workers 自定义域（需 Workers Routes:Read）")

        # 4) Pages 项目 -> 默认 .pages.dev 域名 + 自定义域（需要 Cloudflare Pages:Read）
        try:
            for p in _cf_paged("/accounts/%s/pages/projects" % aid, token,
                               page_size=10):
                sub = p.get("subdomain")
                if sub:
                    domains.add(sub if "." in sub else "%s.pages.dev" % sub)
                for d in (p.get("domains") or []):
                    if d:
                        domains.add(d)
        except CFError as exc:
            log.warning("[%s] 读取 Pages 项目失败：%s", aname, exc)
            if "HTTP 403" in str(exc):
                missing.add("Pages 项目（需 Cloudflare Pages:Read）")

    miss = sorted(missing)
    if miss:
        msg = ("以下来源因 Token 权限未拉到：%s；补齐见 "
               "https://dash.cloudflare.com/profile/api-tokens" % "、".join(miss))
        if domains:
            # 部分成功：不影响运行，降级提示即可
            log.warning("%s（已获取到 %d 个域名，可正常使用；"
                        "若账号下确实没有 Workers 脚本，可忽略）", msg, len(domains))
        else:
            log.error("%s（当前未获取到任何域名）", msg)

    return domains, miss


def parse_manual_domains():
    """解析手动配置的域名（TARGET_DOMAINS + TARGET_DOMAIN），去重保序。"""
    raw = []
    if TARGET_DOMAINS:
        raw += [d for d in re.split(r"[,\s]+", TARGET_DOMAINS.strip()) if d]
    if TARGET_DOMAIN:
        raw.append(TARGET_DOMAIN)
    return _dedup(raw)


def _dedup(raw):
    seen, out = set(), []
    for d in raw:
        d = str(d).strip().rstrip(".")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# hosts 标记区块读写
# ---------------------------------------------------------------------------
def build_block(ip: str, domains):
    """生成标记区块内容（每行一个域名，兼容所有 hosts 解析器）。"""
    lines = [BLOCK_BEGIN]
    lines += ["%s\t%s" % (ip, d) for d in domains]
    lines.append(BLOCK_END)
    return lines


def read_block(hosts_path: str):
    """读取现有标记区块内容，不存在返回 None。"""
    try:
        with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None

    begin = end = None
    for i, line in enumerate(lines):
        if BLOCK_BEGIN in line:
            begin = i
        elif BLOCK_END in line:
            end = i
            break
    if begin is None or end is None or end < begin:
        return None
    return lines[begin:end + 1]


def write_block(hosts_path: str, block):
    """把区块写入 hosts（已存在则替换，否则追加）。

    注意：Docker 里 /etc/hosts 是「单文件 bind mount」，容器无法 rename 替换其
    inode（会触发 [Errno 16] Resource busy），因此这里采用原地写入，
    而不是 tmp + os.replace。
    """
    try:
        with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    begin = end = None
    for i, line in enumerate(lines):
        if BLOCK_BEGIN in line:
            begin = i
        elif BLOCK_END in line:
            end = i
            break

    if begin is not None and end is not None and end >= begin:
        lines[begin:end + 1] = block
    else:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(block)

    # 清理历史版本可能残留的 /etc/hosts.tmp（旧实现用 tmp + replace 会留下它）
    tmp = hosts_path + ".tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass

    # 原地写入：直接截断并重写，兼容 bind mount 场景
    with open(hosts_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def hosts_targets():
    """返回要写入的 hosts 路径列表（HOSTS_FILES 优先，否则 HOSTS_FILE）。"""
    paths = [p for p in re.split(r"[,\s]+", HOSTS_FILES.strip()) if p]
    if paths:
        return paths
    return [HOSTS_FILE] if HOSTS_FILE else []


def sync_hosts(paths, ip: str, domains):
    """把同一份区块同步写入所有目标 hosts 文件。

    每轮都会校验：既覆盖「IP 文件变化」的场景，也覆盖「hosts 被外部改动」
    （目标容器重启、Docker 重新生成容器 hosts 等）的场景。
    内容一致时静默跳过，不产生日志噪音。
    """
    desired = build_block(ip, domains)
    for p in paths:
        try:
            if read_block(p) == desired:
                continue                     # 无变化，静默
            write_block(p, desired)
            log.info("已写入 %s：%s -> %d 个域名", p, ip, len(domains))
        except Exception as exc:             # 单个文件失败不影响其它文件
            log.error("写入 %s 失败：%s", p, exc)


def main():
    # 至少要有手动域名或 CF Token 之一
    if not (TARGET_DOMAIN or TARGET_DOMAINS or CF_API_TOKEN):
        log.error("必须设置 TARGET_DOMAIN / TARGET_DOMAINS / CF_API_TOKEN 之一")
        sys.exit(1)

    manual = parse_manual_domains()

    if DISCOVER_ONLY:
        if not CF_API_TOKEN:
            log.error("DISCOVER_ONLY 需要设置 CF_API_TOKEN")
            sys.exit(1)
        cf, _ = discover_cf_domains(CF_API_TOKEN)
        for d in sorted(cf):
            print(d)
        log.info("共发现 %d 个域名", len(cf))
        sys.exit(0 if cf else 1)

    hosts_paths = hosts_targets()
    if not hosts_paths:
        log.error("未指定 hosts 文件（HOSTS_FILE / HOSTS_FILES 均为空）")
        sys.exit(1)

    log.info("启动监控 | IP文件=%s | 间隔=%ss | hosts目标=%d 个：%s",
             IP_FILE, POLL_INTERVAL, len(hosts_paths), ", ".join(hosts_paths))
    if manual:
        log.info("手动域名 %d 个：%s", len(manual), ", ".join(manual[:5]))

    last_hash = None          # IP 文件内容哈希
    empty_warned = False      # 「IP 文件为空」是否已提示过
    last_cf_fetch = 0.0       # 上次成功拉取 CF 域名的时间
    next_cf_try = 0.0         # 下次允许尝试 CF 的时间（失败退避用）
    cf_fail = 0
    cf_domains = []
    domains = list(manual)

    while True:
        try:
            now = time.time()

            # Cloudflare 域名发现：首轮 / 刷新间隔到期 / 失败退避结束
            if CF_API_TOKEN and now >= next_cf_try and \
                    (not cf_domains or now - last_cf_fetch >= CF_REFRESH_INTERVAL):
                cf, _ = discover_cf_domains(CF_API_TOKEN)
                if cf:
                    if sorted(cf) != sorted(cf_domains):
                        log.info("Cloudflare 自动发现域名 %d 个：%s%s", len(cf),
                                 ", ".join(sorted(cf)[:5]),
                                 " ..." if len(cf) > 5 else "")
                    cf_domains, cf_fail = sorted(cf), 0
                    last_cf_fetch = now
                    next_cf_try = now + CF_REFRESH_INTERVAL
                else:
                    cf_fail += 1
                    backoff = min(CF_BACKOFF_MAX, CF_BACKOFF_BASE * (2 ** (cf_fail - 1)))
                    last_cf_fetch = now
                    next_cf_try = now + backoff
                    log.warning("Cloudflare 未发现任何域名（第 %d 次失败），%.0fs 后重试",
                                cf_fail, backoff)
                    if not cf_domains and not manual:
                        log.warning("暂无可用域名，等待 Cloudflare 发现成功后再写入 hosts")

                domains = _dedup(list(manual) + list(cf_domains))

            if not domains:
                time.sleep(POLL_INTERVAL)
                continue

            h = file_hash(IP_FILE)
            best = read_best_ip(IP_FILE)

            if not best:
                if not empty_warned:          # 只在首次/由有变无时提示，避免每轮刷屏
                    log.warning("IP 文件为空或没有合法 IP：%s", IP_FILE)
                    empty_warned = True
            else:
                empty_warned = False
                if last_hash is not None and h != last_hash:
                    log.info("检测到 IP 文件变化，当前最优 IP = %s", best)
                # 每轮校验：IP 变化会更新，hosts 被外部改动也会自动修复
                sync_hosts(hosts_paths, best, domains)

            last_hash = h
        except Exception as exc:          # 单次异常不应中断守护循环
            log.error("处理出错：%s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
