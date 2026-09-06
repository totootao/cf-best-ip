#!/usr/bin/env python3
"""
Cloudflare 优选 IP 监控器（Docker 版）

功能：
  持续监控一个优选 IP 列表文件。当文件内容变化时，读取其中的最优 IP
  （第一行合法 IP，支持 IPv4 / IPv6），并把该 IP 映射到一批域名，
  写入宿主机 /etc/hosts 的标记区块。

域名来源（三者可叠加）：
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
TARGET_DOMAIN  = os.environ.get("TARGET_DOMAIN", "")      # 单个域名（兼容旧配置）
TARGET_DOMAINS = os.environ.get("TARGET_DOMAINS", "")     # 多域名，逗号/空格分隔
CF_API_TOKEN   = os.environ.get("CF_API_TOKEN", "")       # Cloudflare API Token（可选）
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "10"))        # IP 文件轮询间隔（秒）
CF_REFRESH_INTERVAL = float(os.environ.get("CF_REFRESH_INTERVAL", "3600"))  # 域名列表刷新间隔（秒）

BLOCK_BEGIN = "# >>> cf-best-ip >>>"
BLOCK_END   = "# <<< cf-best-ip <<<"
CF_API = "https://api.cloudflare.com/client/v4"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-best-ip")


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
def _cf_get(path: str, token: str, timeout: int = 20):
    """调用 Cloudflare API，返回解析后的 JSON。"""
    req = urllib.request.Request(CF_API + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_cf_domains(token: str):
    """发现账号下所有 Workers 与 Pages 域名；失败返回空集合。"""
    domains = set()
    try:
        accounts = _cf_get("/accounts?per_page=50", token).get("result", [])
    except Exception as exc:
        log.error("获取 Cloudflare 账号列表失败：%s", exc)
        return domains

    for acct in accounts:
        aid = acct.get("id")
        if not aid:
            continue
        aname = acct.get("name", aid)

        # 1) workers.dev 子域
        workers_sub = None
        try:
            res = _cf_get("/accounts/%s/workers/subdomain" % aid, token).get("result") or {}
            workers_sub = res.get("subdomain")
        except Exception as exc:
            log.warning("[%s] 读取 workers.dev 子域失败：%s", aname, exc)

        # 2) Workers 脚本 -> {script}.{subdomain}.workers.dev
        if workers_sub:
            try:
                scripts = _cf_get("/accounts/%s/workers/scripts?per_page=100" % aid,
                                  token).get("result", [])
                for s in scripts:
                    name = s.get("id")
                    if name:
                        domains.add("%s.%s.workers.dev" % (name, workers_sub))
            except Exception as exc:
                log.warning("[%s] 读取 Workers 脚本列表失败：%s", aname, exc)

        # 3) Workers 自定义域
        try:
            wdom = _cf_get("/accounts/%s/workers/domains?per_page=100" % aid,
                           token).get("result", [])
            for d in wdom:
                host = d.get("hostname") or d.get("domain") or d.get("name")
                if host:
                    domains.add(host)
        except Exception as exc:
            log.warning("[%s] 读取 Workers 自定义域失败：%s", aname, exc)

        # 4) Pages 项目 -> 默认 .pages.dev 域名 + 自定义域
        try:
            projects = _cf_get("/accounts/%s/pages/projects?per_page=100" % aid,
                               token).get("result", [])
            for p in projects:
                sub = p.get("subdomain")
                if sub:
                    domains.add(sub if "." in sub else "%s.pages.dev" % sub)
                for d in (p.get("domains") or []):
                    if d:
                        domains.add(d)
        except Exception as exc:
            log.warning("[%s] 读取 Pages 项目失败：%s", aname, exc)

    return domains


def resolve_domains():
    """汇总所有域名来源，去重并保持顺序。"""
    raw = []
    if TARGET_DOMAINS:
        raw += [d for d in re.split(r"[,\s]+", TARGET_DOMAINS.strip()) if d]
    if TARGET_DOMAIN:
        raw.append(TARGET_DOMAIN)
    if CF_API_TOKEN:
        cf = discover_cf_domains(CF_API_TOKEN)
        if cf:
            log.info("Cloudflare 自动发现域名 %d 个", len(cf))
        raw += sorted(cf)

    seen, out = set(), []
    for d in raw:
        d = d.strip().rstrip(".")
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
    """把区块写入 hosts（已存在则替换，否则追加）。"""
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

    tmp = hosts_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, hosts_path)          # 原子替换，避免写坏 hosts


def main():
    global CF_API_TOKEN

    # 至少要有手动域名或 CF Token 之一
    if not (TARGET_DOMAIN or TARGET_DOMAINS or CF_API_TOKEN):
        log.error("必须设置 TARGET_DOMAIN / TARGET_DOMAINS / CF_API_TOKEN 之一")
        sys.exit(1)

    log.info("启动监控 | IP文件=%s | hosts=%s | 间隔=%ss",
             IP_FILE, HOSTS_FILE, POLL_INTERVAL)

    last_hash = None          # 强制首轮执行
    last_cf_fetch = 0.0       # 上次拉取 CF 域名的时间
    domains = []

    while True:
        try:
            now = time.time()
            # 域名列表：首轮、或刷新间隔到期、或 IP 变化时都会刷新
            need_domains = (not domains) or (CF_API_TOKEN and now - last_cf_fetch >= CF_REFRESH_INTERVAL)
            if need_domains:
                domains = resolve_domains()
                last_cf_fetch = now
                if not domains:
                    log.warning("未获取到任何域名，跳过本轮")
                    time.sleep(POLL_INTERVAL)
                    continue
                log.info("目标域名 %d 个：%s%s", len(domains),
                         ", ".join(domains[:5]),
                         " ..." if len(domains) > 5 else "")

            h = file_hash(IP_FILE)
            if h != last_hash:
                last_hash = h
                best = read_best_ip(IP_FILE)
                if not best:
                    log.warning("IP 文件为空或没有合法 IP：%s", IP_FILE)
                else:
                    desired = build_block(best, domains)
                    current = read_block(HOSTS_FILE)
                    if current == desired:
                        log.info("无变化（%s -> %d 个域名），跳过写入", best, len(domains))
                    else:
                        write_block(HOSTS_FILE, desired)
                        log.info("已写入 %s：%s -> %d 个域名",
                                 HOSTS_FILE, best, len(domains))
        except Exception as exc:          # 单次异常不应中断守护循环
            log.error("处理出错：%s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
