#!/usr/bin/env bash
# 构建安卓 App。
#
#   bash build.sh                  # → app/build/outputs/apk/debug/app-debug.apk
#   bash build.sh assembleRelease  # 用 debug keystore 签名，可直接安装
#   bash build.sh assembleRelease testDebugUnitTest
#
# 前置（三样都要）：
#   JDK 17        设 JAVA_HOME，或让 java 在 PATH 上
#   Android SDK   设 ANDROID_HOME / ANDROID_SDK_ROOT，
#                 或在 app/local.properties 里写一行 sdk.dir=/path/to/sdk
#                 需要 build-tools 34.0.0 与 platforms/android-34
#   Gradle 8.x    仓库带 wrapper 时用 ./gradlew；否则用 PATH 上的 gradle
#
# 三样都由环境提供时，本脚本不含任何硬编码路径 —— 换机器直接能跑。

set -euo pipefail

cd "$(dirname "$0")"

# ── MSYS / Git-Bash 路径转换 ────────────────────────────────────────────
# /c/Users/x 这种路径交给原生 java.exe / gradle 会被解释成 C:\c\Users\x，
# 报错是「找不到 SDK」。给原生程序传路径一律用 Windows 形式（C:/Users/x）。
to_win_path() {
    case "$1" in
        /[a-zA-Z]/*)
            _drive=$(printf '%s' "$1" | cut -c2 | tr 'a-z' 'A-Z')
            printf '%s' "${_drive}:$(printf '%s' "$1" | cut -c3-)"
            ;;
        *) printf '%s' "$1" ;;
    esac
}

# ── SDK 定位：环境变量优先，其次 app/local.properties ──────────────────
if [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ]; then
    if [ -f local.properties ]; then
        _sdk=$(sed -n 's/^sdk\.dir=//p' local.properties | head -1 | tr -d '\r')
        # local.properties 里可能是 C\:\\Users\\... 这种转义写法，先还原
        _sdk=$(printf '%s' "$_sdk" | sed 's/\\\\/\//g; s/\\:/:/g')
        if [ -n "$_sdk" ]; then
            export ANDROID_HOME="$_sdk"
            export ANDROID_SDK_ROOT="$_sdk"
        fi
    fi
fi

if [ -n "${ANDROID_HOME:-}" ]; then
    ANDROID_HOME=$(to_win_path "$ANDROID_HOME")
    export ANDROID_HOME
    export ANDROID_SDK_ROOT="$ANDROID_HOME"
    echo "[build] ANDROID_HOME=$ANDROID_HOME"
else
    echo "[build] 未找到 Android SDK：请设 ANDROID_HOME，或在 local.properties 写 sdk.dir=" >&2
    exit 1
fi

# ── Gradle ─────────────────────────────────────────────────────────────
if [ -x ./gradlew ]; then
    GRADLE=./gradlew
elif command -v gradle >/dev/null 2>&1; then
    GRADLE=gradle
else
    echo "[build] 找不到 gradle：装一个 Gradle 8.x，或给仓库补上 wrapper" >&2
    exit 1
fi

if [ -n "${JAVA_HOME:-}" ]; then
    echo "[build] JAVA_HOME=$JAVA_HOME"
else
    echo "[build] 未设 JAVA_HOME，将直接调用 PATH 上的 java"
fi

# ── 任务 ───────────────────────────────────────────────────────────────
TASKS=("$@")
if [ ${#TASKS[@]} -eq 0 ]; then
    TASKS=(assembleDebug)
fi

echo "[build] $GRADLE ${TASKS[*]}"
exec "$GRADLE" "${TASKS[@]}"
