#!/bin/sh
# 扫码打印网关 —— 容器入口
#
# 两种模式，由 CUPS_SERVER 是否存在决定：
#   A) 未设 CUPS_SERVER —— 容器内自带 cupsd，USB 打印机直通进来（默认，一条命令可用）
#   B) 设了 CUPS_SERVER —— 宿主已装 CUPS，容器只跑网关，避免两套队列打架
#
# 无论哪种模式，最后都 exec 到 python3 print_gateway.py，让它是 PID 1 的子进程里
# 唯一的前台进程 —— 信号与日志都直通，不套 supervisord。

set -eu

PORT="${PORT:-8080}"
SPOOL_DIR="${SPOOL_DIR:-/var/spool/print-gateway}"
TITLE="${TITLE:-扫码打印}"

log() { echo "[entrypoint] $*"; }

# 显式要求口令的部署（如挂到公网的那份实例）：宁可起不来，也不能免密上线。
# 不能拿「TOKEN 是否为空」直接当判据 —— 内网那份实例本来就该允许空口令，
# 所以这里要的是一个由部署方式显式声明的开关。
if [ "${REQUIRE_TOKEN:-}" = "1" ] && [ -z "${TOKEN:-}" ]; then
    log "错误：REQUIRE_TOKEN=1 但 TOKEN 为空 —— 拒绝启动"
    log "      公开可达的实例不允许免密（compose 里这个值来自 PUBLIC_TOKEN）"
    exit 2
fi

mkdir -p "$SPOOL_DIR"

if [ -n "${CUPS_SERVER:-}" ]; then
    log "模式 B：使用外部 CUPS —— $CUPS_SERVER"
else
    log "模式 A：启动容器内 CUPS"

    mkdir -p /run/cups /run/dbus /var/spool/cups /var/cache/cups
    chown -R root:lp /var/spool/cups /var/cache/cups 2>/dev/null || true

    # /etc/cups 是挂载卷。首次启动（卷是空的）时铺一份最小配置进去；
    # 之后一律沿用卷里的那份，不覆盖用户改过的设置。
    if [ ! -f /etc/cups/cupsd.conf ]; then
        cp /opt/gateway/cupsd.conf /etc/cups/cupsd.conf
        log "已写入默认 /etc/cups/cupsd.conf"
    fi

    # 配置语法自检：写错了就别硬起，否则表现为「队列在但打不出来」，很难查
    if ! /usr/sbin/cupsd -t -c /etc/cups/cupsd.conf; then
        log "cupsd.conf 校验失败，容器退出"
        exit 1
    fi

    # mDNS 广播（iOS / macOS 自动发现队列）。失败不致命，只降级成「要手输 IP」。
    if command -v dbus-daemon >/dev/null 2>&1; then
        dbus-daemon --system --fork 2>/dev/null || log "dbus 未启动（忽略）"
    fi
    if command -v avahi-daemon >/dev/null 2>&1; then
        avahi-daemon --no-drop-root --daemon 2>/dev/null || log "avahi 未启动（忽略）"
    fi

    /usr/sbin/cupsd -c /etc/cups/cupsd.conf

    # 等 631 真的能应答再往下走。等不到就继续 —— 网关本身仍可服务，
    # /healthz 会如实报告队列数为 0，比这里直接崩掉更好排查。
    i=0
    while [ "$i" -lt 30 ]; do
        if lpstat -r >/dev/null 2>&1; then
            log "CUPS 就绪，发现 $(lpstat -p 2>/dev/null | grep -c '^printer' || true) 个队列"
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    if [ "$i" -ge 30 ]; then
        log "警告：等待 CUPS 就绪超时，仍继续启动网关"
    fi
fi

cd /opt/gateway/server

# 参数按需拼装。这里刻意不用 ${VAR:+...} —— 它在 POSIX sh 里不拆分引号，
# 带空格的 TITLE（如「扫码打印」以外的名字）会被当成多个参数传下去。
set -- --port "$PORT" --bind 0.0.0.0 --spool "$SPOOL_DIR" --title "$TITLE"

if [ -n "${PRINTER:-}" ]; then set -- "$@" --printer "$PRINTER"; fi
if [ -n "${TOKEN:-}" ]; then set -- "$@" --token "$TOKEN"; fi
if [ -n "${ADMIN_TOKEN:-}" ]; then set -- "$@" --admin-token "$ADMIN_TOKEN"; fi
if [ -n "${HOST_DISPLAY:-}" ]; then set -- "$@" --host-display "$HOST_DISPLAY"; fi
if [ -n "${APK:-}" ]; then set -- "$@" --apk "$APK"; fi
if [ -n "${MAX_MB:-}" ]; then set -- "$@" --max-mb "$MAX_MB"; fi
if [ -n "${DEFAULT_DPI:-}" ]; then set -- "$@" --default-dpi "$DEFAULT_DPI"; fi
if [ -n "${TLS_PORT:-}" ]; then set -- "$@" --tls-port "$TLS_PORT"; fi
if [ -n "${TLS_CERT:-}" ]; then set -- "$@" --tls-cert "$TLS_CERT"; fi
if [ -n "${TLS_KEY:-}" ]; then set -- "$@" --tls-key "$TLS_KEY"; fi
# 内网穿透 / 反向代理部署：TLS 在隧道边缘终结，这里只声明公网入口地址
if [ -n "${PUBLIC_URL:-}" ]; then set -- "$@" --public-url "$PUBLIC_URL"; fi
if [ "${TOKEN_ALWAYS:-}" = "1" ]; then set -- "$@" --token-always; fi
if [ "${VERBOSE:-}" = "1" ]; then set -- "$@" --verbose; fi

log "启动网关：$TITLE（端口 $PORT）"
exec python3 print_gateway.py "$@"
