#!/usr/bin/env python3
"""
Cloudflare 优选 IP 监控器（Docker 版）

功能：
  持续监控一个「每行一个 IP」的文本文件。当文件内容发生变化时，
  读取其中的「最优 IP」（第一行合法 IP，支持 IPv4 / IPv6），
  并将其写入宿主机 /etc/hosts 的标记区块，使指定域名指向该 IP。

  例如 TARGET_DOMAIN=cdn.example.com 时，会在 /etc/hosts 中维护：
      # >>> cf-best-ip >>>
      104.27.200.69    cdn.example.com
      # <<< cf-best-ip <<<

设计要点：
  - 纯标准库，无需任何第三方依赖（镜像极小）。
  - 采用轮询（mtime + 内容哈希）而非 inotify，跨 bind mount / 网络存储都可靠。
  - 只改写标记区块，绝不触碰 hosts 文件其余内容。
  - IP 变化时才真正写入，避免无意义的文件改动。
"""

import os
import re
import sys
import time
import logging
import hashlib
import ipaddress

# ---------------------------------------------------------------------------
# 配置（均可用环境变量覆盖）
# ---------------------------------------------------------------------------
IP_FILE       = os.environ.get("IP_FILE", "/data/ip_list.txt")   # 优选 IP 列表（每行一个）
HOSTS_FILE    = os.environ.get("HOSTS_FILE", "/host/hosts")      # 宿主机的 /etc/hosts（bind mount 进容器）
TARGET_DOMAIN = os.environ.get("TARGET_DOMAIN", "")             # 要映射的域名，必须设置
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))    # 轮询间隔（秒）

BLOCK_BEGIN = "# >>> cf-best-ip >>>"
BLOCK_END   = "# <<< cf-best-ip <<<"

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
      104.27.200.69                          （裸 IP）
      91.110.174.190:8443#38.27MB/s-HKG-HK   （带端口 + 备注，优选工具常见导出）
      2606:4700::1111                        （IPv6）
    """
    token = token.strip()
    if not token or token.startswith("#"):
        return None
    token = token.split("#", 1)[0].strip()          # 去掉 # 及其后的备注
    if not token:
        return None
    # 1) 整体尝试（裸 IPv4 / IPv6）
    try:
        return str(ipaddress.ip_address(token))
    except ValueError:
        pass
    # 2) 形如 IP:端口 -> 取冒号前部分
    if ":" in token:
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


def current_block_ip(hosts_path: str):
    """读取标记区块内、TARGET_DOMAIN 对应的当前 IP（无则返回 None）。"""
    try:
        with open(hosts_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None

    in_block = False
    for line in lines:
        if BLOCK_BEGIN in line:
            in_block = True
            continue
        if BLOCK_END in line:
            break
        if in_block and TARGET_DOMAIN and TARGET_DOMAIN in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[0]
    return None


def update_hosts(best_ip: str):
    """把 best_ip -> TARGET_DOMAIN 写入 hosts 的标记区块（不存在则追加）。"""
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    begin_idx = end_idx = None
    for i, line in enumerate(lines):
        if BLOCK_BEGIN in line:
            begin_idx = i
        elif BLOCK_END in line:
            end_idx = i
            break

    new_block = [BLOCK_BEGIN, f"{best_ip}\t{TARGET_DOMAIN}", BLOCK_END]

    if begin_idx is not None and end_idx is not None:
        lines[begin_idx:end_idx + 1] = new_block
    else:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(new_block)

    with open(HOSTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    log.info("已写入 %s -> %s (域名 %s)", HOSTS_FILE, best_ip, TARGET_DOMAIN)


def main():
    if not TARGET_DOMAIN:
        log.error("必须设置环境变量 TARGET_DOMAIN，例如 -e TARGET_DOMAIN=cdn.example.com")
        sys.exit(1)

    log.info("启动监控 | IP文件=%s | hosts=%s | 域名=%s | 间隔=%ss",
             IP_FILE, HOSTS_FILE, TARGET_DOMAIN, POLL_INTERVAL)

    last_hash = None  # 强制首轮执行一次
    while True:
        try:
            h = file_hash(IP_FILE)
            if h != last_hash:
                last_hash = h
                best = read_best_ip(IP_FILE)
                if not best:
                    log.warning("IP 文件为空或没有合法 IP：%s", IP_FILE)
                else:
                    current = current_block_ip(HOSTS_FILE)
                    if current == best:
                        log.info("IP 未变化（%s），跳过写入", best)
                    else:
                        update_hosts(best)
        except Exception as exc:  # 单次异常不应中断守护循环
            log.error("处理出错：%s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
