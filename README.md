# Print Gateway（扫码上传打印网关 + 自助打印 App）

嵌入式 CUPS 打印网关：手机扫码/网页上传 PDF 与图片，服务端在局域网内完成
拼版（N 份/页）、小册子、页边距、镜像、灰度、页范围、水印页码、裁剪、
拉伸铺满等排版处理，再送 CUPS 打印队列。自带 WPS 风格打印面板网页
与安卓自助打印 App（WebView 壳 + 相机扫码连接 + 「打开方式」直接打印）。

## ⚠️ 使用限制：不得用于商用

本项目以 [CC BY-NC 4.0](LICENSE) 许可发布：**仅限非商业用途**（个人自用、
学习、开源贡献）。禁止将本代码用于收费服务、企业经营或嵌入商业化产品。
商业使用请自行取得作者许可。

## 仓库结构

```
server/   打印网关服务端（Python 3，仅标准库 + PIL + reportlab + gs 工具链）
app/      安卓自助打印 App（Kotlin + CameraX + ML Kit 离线条码）
```

## 服务端（server/）

单文件服务 `print_gateway.py` + 三个引擎模块（排版 `pg_layout.py`、
引擎 `pg_engine.py`、装饰 `pg_decor.py`），零 Web 框架、可单文件 scp 部署。

### 依赖（设备端）

- Python 3（`/usr/bin/python3`）、CUPS + cups-filters
- `gs`、`pdftoppm`、`pdfinfo`、`pdfseparate`、`pdfunite`（poppler/gs 工具链）
- `python3-reportlab`（拼版）、Pillow（图片处理）

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传文件 → 返回作业号、页数 |
| POST | `/api/preview` | 按设置生成最终 PDF 并渲染预览图（JSON body） |
| POST | `/api/print` | 生成最终 PDF 并送 CUPS 队列 |
| GET | `/api/printers` | 打印机列表与默认队列 |
| GET | `/api/job?id=` | 按作业号取回作业（App「打开方式」直达设置页） |
| GET | `/img?job=&k=&n=` | 预览图 |
| GET | `/app` | 下载安卓 App 包（`--apk` 指定；放在鉴权检查之前，扫码用户无口令也能下） |
| GET | `/healthz` | 健康检查（含 `app` 键、打印机数） |

### 部署

```bash
# 本地 Windows：一键上传 + 生成 systemd unit + 重启（dev host 即 SSH 别名）
python deploy_gateway.py --host gateway-host --hostip 192.168.1.100 --port 8080
# 托管 App 下载：加 --apk <本地 apk 路径>
python deploy_gateway.py --host gateway-host --port 8080 --apk /path/to/print-selfservice.apk
# 可选：访问口令（网页/App 连接时需带 ?t= 参数）
python deploy_gateway.py --host gateway-host --port 8080 --token 你的口令
```

### 测试

```bash
python3 -m unittest test_pg_engine test_print_gateway test_gen_qr   # 单元
python3 verify_e2e.py                                               # 端到端（建议打测试落盘队列，勿打真机）
```

- `make_testfiles.py`：生成测试用 PDF/PNG 样例
- `gen_qr.py`：零依赖二维码生成（终端/图片/SVG），含严格校验
- `gw.ppd`：参考 PPD 样例（按需替换为你的队列 PPD）

## App（app/）

Kotlin + ViewBinding，包名 `com.printgw.selfservice`（minSdk 26 / targetSdk 34）。

- **WebView 壳**：打开网关网页打印面板
- **相机扫码连接**（`ScanActivity`）：CameraX 预览 + ML Kit 离线全格式条码，
  扫「带口令连接码」（如 `http://IP:8080/?t=口令`）一步配好 host + 口令，不联网
- **「打开方式」接收方**：manifest 注册 PDF/图片 MIME，文件管理器/浏览器
  下载后直接选本 App 打印，原生读 `content://` 流上传（纯 WebView 壳做不到）
- **App 内下载 App**：服务端 `GET /app` 托管 APK，网页面板有下载按钮，
  形成「扫码 → 下载 → 安装 → 扫码连接」闭环

### 构建

标准 Gradle 工程（工程根在 `app/` 目录）：

```bash
cd app
./gradlew assembleDebug        # 或 gradle assembleDebug
./gradlew testDebugUnitTest    # Prefs 连接码解析纯函数单测（JVM 可跑）
```

国内网络已在 `settings.gradle` 配好阿里云镜像兜底。Android 9+ 明文 HTTP
需 `usesCleartextTraffic`（manifest 已设）。

## 许可与使用限制

- 代码许可：**CC BY-NC 4.0（仅限非商业用途）**，详见 [LICENSE](LICENSE)
- 保留许可声明与署名：`Copyright (c) 2026 print-gateway authors`
