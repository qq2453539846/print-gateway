# 扫码打印网关 —— 容器镜像
#
# 设计取舍
#   1) 用发行版的 python3-reportlab / python3-pil，**不装 pip**。
#      与裸机部署保持同一条技术路线，也避开 armv7 上编译 wheel 的麻烦。
#   2) Ghostscript 只通过子进程调用（不链接其库），所以镜像分发
#      不受 AGPL 传染，与项目的 CC BY-NC 4.0 相容。
#   3) 多架构：本镜像在 linux/arm/v7 上同样是目标平台 ——
#      项目本身就跑在一台 1GB 内存的 armv7 小盒子上。
#
# 依赖分组（对照 README 的依赖表）
#   cups / cups-filters / cups-client  打印队列（CUPS 2.4.2，与实测版本一致）
#   ghostscript / poppler-utils        PDF 归一化与栅格化
#   python3-reportlab / python3-pil    拼版合成、水印与页码的位图装饰
#   fonts-noto-cjk                     中文水印与页码（不装会渲染成空白）
#   avahi-daemon / dbus                mDNS 广播，iOS / macOS 自动发现队列

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# 国内网络直连 deb.debian.org 经常超时（实测 armv7 设备上直接 connection timed out）。
# 构建时换镜像源即可：
#   docker build --build-arg DEBIAN_MIRROR=mirrors.aliyun.com -t print-gateway .
# 用 compose 的话写在 docker-compose.yml 的 build.args 里。
ARG DEBIAN_MIRROR=deb.debian.org

RUN set -eux; \
    if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s|deb.debian.org|$DEBIAN_MIRROR|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        cups cups-filters cups-client cups-ppdc \
        ghostscript poppler-utils \
        python3 python3-reportlab python3-pil \
        fonts-noto-cjk \
        avahi-daemon dbus \
        ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*

# 网关本体：全部是标准库 + 上面这些系统包，没有 requirements.txt
COPY server/ /opt/gateway/server/
COPY docker/cupsd.conf /opt/gateway/cupsd.conf
COPY docker/entrypoint.sh /opt/gateway/entrypoint.sh
RUN chmod +x /opt/gateway/entrypoint.sh

# 队列配置（PPD / printers.conf）与作业存放，挂出来才能重建容器不丢
VOLUME ["/etc/cups", "/var/spool/print-gateway"]

# 8080 手机扫码入口；631 CUPS/IPP（局域网其他电脑加打印机走这个）
EXPOSE 8080 631

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["/opt/gateway/entrypoint.sh"]
