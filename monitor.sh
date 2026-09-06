#!/bin/sh
# Cloudflare 优选 IP 监控器（纯 POSIX sh / busybox ash 版，无需 Python）
# 监控「每行一个 IP」的文本文件，变化时把第一行最优 IP 写入 hosts 标记区块。
# 适合 Alpine 等未安装 Python 的环境，也可在 FROM alpine 的镜像里运行。

IP_FILE="${IP_FILE:-/data/ip_list.txt}"
HOSTS_FILE="${HOSTS_FILE:-/host/hosts}"
TARGET_DOMAIN="${TARGET_DOMAIN:-}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
BEGIN="# >>> cf-best-ip >>>"
END="# <<< cf-best-ip <<<"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] $*"; }

md5_of() { md5sum "$1" 2>/dev/null | awk '{print $1}'; }

# 取第一个合法 IP（IPv4 或 IPv6），跳过空行与 # 注释
read_best_ip() {
  while IFS= read -r line; do
    ip=$(printf '%s' "$line" | awk '{print $1}')
    [ -z "$ip" ] && continue
    case "$ip" in \#*) continue ;; esac
    case "$ip" in
      *.*) echo "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' && { echo "$ip"; return; } ;;
      *:*) echo "$ip" | grep -Eq '^[0-9a-fA-F:]+$'        && { echo "$ip"; return; } ;;
    esac
  done < "$1"
}

# 当前 hosts 区块中 TARGET_DOMAIN 对应的 IP
current_ip() {
  grep -F "$TARGET_DOMAIN" "$HOSTS_FILE" 2>/dev/null | grep -v '^#' | awk '{print $1; exit}'
}

update_hosts() {
  best="$1"
  start=$(grep -n -F "$BEGIN" "$HOSTS_FILE" 2>/dev/null | head -1 | cut -d: -f1)
  end=""
  if [ -n "$start" ]; then
    end=$(awk -v s="$start" -v e="$END" 'NR>s && index($0,e){print NR; exit}' "$HOSTS_FILE")
  fi
  tmp=$(mktemp)
  if [ -n "$start" ] && [ -n "$end" ]; then
    # 保留 BEGIN 之前、END 之后的内容，中间整段替换
    if [ "$start" -gt 1 ]; then head -n $((start-1)) "$HOSTS_FILE" > "$tmp"; else : > "$tmp"; fi
    printf '%s\n%s\t%s\n%s\n' "$BEGIN" "$best" "$TARGET_DOMAIN" "$END" >> "$tmp"
    tail -n +$((end+1)) "$HOSTS_FILE" >> "$tmp" 2>/dev/null
  else
    # 首次：追加区块
    cp "$HOSTS_FILE" "$tmp" 2>/dev/null
    printf '\n%s\n%s\t%s\n%s\n' "$BEGIN" "$best" "$TARGET_DOMAIN" "$END" >> "$tmp"
  fi
  cat "$tmp" > "$HOSTS_FILE"
  rm -f "$tmp"
  log "已写入 $HOSTS_FILE -> $best (域名 $TARGET_DOMAIN)"
}

main() {
  if [ -z "$TARGET_DOMAIN" ]; then
    log "ERROR: 必须设置 TARGET_DOMAIN，例如 -e TARGET_DOMAIN=cdn.example.com"
    exit 1
  fi
  log "启动监控 | IP=$IP_FILE | hosts=$HOSTS_FILE | 域名=$TARGET_DOMAIN | 间隔=${POLL_INTERVAL}s"
  last=""
  while true; do
    h=$(md5_of "$IP_FILE")
    if [ "$h" != "$last" ]; then
      last="$h"
      best=$(read_best_ip "$IP_FILE")
      if [ -z "$best" ]; then
        log "IP 文件为空或无可解析 IP: $IP_FILE"
      else
        cur=$(current_ip)
        if [ "$cur" = "$best" ]; then
          log "IP 未变化 ($best)，跳过"
        else
          update_hosts "$best"
        fi
      fi
    fi
    sleep "$POLL_INTERVAL"
  done
}

# 仅在被直接执行时启动；CF_MONITOR_LIB=1 时仅加载函数（便于测试 / source）
if [ -z "$CF_MONITOR_LIB" ]; then
  main "$@"
fi
