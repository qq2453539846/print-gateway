# 自助打印 App（安卓）

把扫码打印网关包成一个安卓 App，让**系统里的 PDF 能经「更多打开方式」直接送打印**。

- 包名 `com.printgw.selfservice`　显示名「自助打印」
- minSdk 26（Android 8.0）　targetSdk 34
- 无第三方网络库，只有 WebView + 一次 `HttpURLConnection` 上传

## 两种入口

| 入口 | 路径 | 说明 |
|---|---|---|
| 扫码 / 桌面图标 | WebView 打开 `http://<服务器>/` | 完整自助打印面板，手动选文件 |
| 系统「更多打开方式」/ 分享 | `ACTION_VIEW` `ACTION_SEND(MULTIPLE)` | 原生读流上传，再跳 `/?job=<id>` 进设置页 |

第二种才是这个 App 存在的理由 —— **纯 WebView 壳做不到**：系统不会把 PDF 交给
浏览器类组件，WebView 里的 JS 也没有读 `content://` 的权限。所以必须由原生层
读文件流并上传，再把页面导航过去。

两条入口最终共用服务端同一个「设置 → 预览 → 打印」流程，功能完全一致。

## 与服务端的约定

App 只依赖三个既有/新增接口，不引入任何私有协议：

```
POST /api/upload         multipart/form-data，字段名 file（可多个）
                         → {id, filename, pages, files, size}
POST /api/print          {job, spec} → 提交打印
GET  /api/job?id=<id>    → {id, filename, pages, files}   ← 为直达入口新增
```

页面侧支持两个查询参数（`print_gateway.py` 的 `PAGE`）：

- `?job=<id>`　按作业号铺开设置界面，用户不必再选一次文件
- `?t=<口令>`　服务端开了 `--token` 时，页面内所有 API 请求都要带上它，
  否则页面能打开但每个请求都 401

## 构建

```bash
bash build.sh                 # → app/build/outputs/apk/debug/app-debug.apk
bash build.sh assembleRelease # 用 debug keystore 签名，可直接安装
```

工具链位置与「必须传 Windows 形式路径」的原因见 `build.sh` 内注释。

## 安装与配置

1. 把 APK 传到手机安装（需允许「未知来源」）
2. 首次打开会要求填服务器地址，预填 `192.168.1.100:8080`；网关未设口令则口令留空
3. 地址变更：右上角菜单 →「服务器设置」，**不需要重装 APK**

## 实现上必须守住的几条（都踩过）

1. **`android:usesCleartextTraffic="true"` 不能省。**
   网关只提供 HTTP，Android 9+ 默认禁明文，否则 WebView 和上传一起失败，
   而报错藏在 WebView 内部，界面上只看到白屏。
2. **上传必须带 `Content-Length`。**
   网关的 `_body()` 只读 `Content-Length`，不解析 `Transfer-Encoding: chunked`，
   所以绝不能用 `setChunkedStreamingMode()`。这里优先用
   `setFixedLengthStreamingMode()`（先查文件大小算出 multipart 总长），
   查不到大小就退回默认缓冲模式，让 HttpURLConnection 自己算。
3. **multipart 头要用 UTF-8 写。**
   `DataOutputStream.writeBytes()` 只取每个字符的低 8 位，中文文件名会变成乱码。
   一律 `String.toByteArray(UTF_8)` + `write()`。
4. **文件名里的 `"` 和换行要替换掉**，否则直接破坏 multipart 分帧。
5. **`launchMode="singleTask"`**：从分享进入时复用已有实例，走 `onNewIntent`，
   不会叠出一堆 Activity。
6. **`<input type="file">` 默认不弹选择器**，必须实现
   `WebChromeClient.onShowFileChooser`，否则网页里的选文件按钮没反应。
7. **Android 11+ 的 `<queries>`**：不声明时部分 ROM 不会把本 App 列进
   「打开方式」候选。

## 已知限制

- 只处理 `content://` / `file://` 的 PDF 与图片（网关本身也只支持这两类）。
- 微信、QQ 的内置预览器经常**不提供**「用其他应用打开」；文件管理器、
  WPS、浏览器下载完成后的 PDF 一般都有。
- 部分 ROM（MIUI / ColorOS）对第三方 App 出现在「打开方式」有额外限制，
  需要真机确认。

## 尚未验证的部分

- **真机上「打开方式」是否列得出本 App** —— 只能在手机上确认。
- 真机安装、HTTP 连通、上传大文件的实际耗时。
- 地址栏未做二维码扫描：目前靠手输，配好后不再需要。
