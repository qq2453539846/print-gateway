# 更新日志

本项目遵循「面板徽标 = 实际部署版本」的惯例。面板徽标（`/` 与 `/admin`）和
`/healthz` 的 `version` 字段取自**同一个常量** `VERSION`（`server/print_gateway.py` 顶部），
不存在单独的「接口协议版本」—— 所以这三处要么同时是新版、要么同时是旧版，
拿哪一处判断构建有没有生效都行。发布新版本时只需改这一处。
Android App 同理：扫码页与设置框都显示 `App <版本> · 构建 <时间戳>`，
就是为了不再靠「感觉还是不行」来回猜装没装上。

## [v3.11] — Docker 部署、界面截图与样例文件修复

### 新增

- **Docker / Compose 部署**（`Dockerfile`、`docker-compose.yml`、`docker/`）。
  默认容器内自带 CUPS，USB 打印机经 `devices: /dev/bus/usb` 直通；
  设了 `CUPS_SERVER` 则切到「只跑网关、连宿主 CUPS」，避免两套队列互相打架。
  `entrypoint.sh` 起来之前先跑 `cupsd -t` 做配置语法自检 —— 配置写错的表现是
  「队列在、但打不出来」，比直接退出难查得多
- README 补界面截图，落在 `docs/screenshots/`
- **`app/build.sh`** —— `app/README` 一直在讲 `bash build.sh`，仓库里却没有这个文件。
  补上，且**不含硬编码路径**：SDK 取 `ANDROID_HOME` 或 `local.properties`，
  Gradle 优先用 wrapper。顺带处理 MSYS 路径 —— `/c/Users/x` 交给原生 `java.exe`
  会被解释成 `C:\c\Users\x`，报错却是「找不到 SDK」

### 修复

- **`sample_multipage.pdf` 不是合法 PDF** —— `make_testfiles.py` 把 `/Info` 指向
  **0 号对象**，还把 Info 正文塞进了 `0 0 obj`。0 号在 xref 里是空闲链表头，
  不是可引用对象。Ghostscript 于是走「修复」路径，**三分页被压成 1 页**，
  网关的归一化保护（页数变化即拒）立刻把预览挡成 400。
  这条恰好堵死了 README 里「没有实体打印机也能试」的那条路。
  修掉后实测：3 页过 gs 仍是 3 页，且不再出现 repair 告警
- README 概览表的「单元测试 364 项」是旧数字，实跑为 **427 项**
- **compose 默认映射 `631:631`，在「宿主已装 CUPS」的机器上开箱即失败**。
  实测报 `Error starting userland proxy: bind: address already in use` —— 而
  「宿主有 CUPS」偏偏是本项目最常见的场景（网关本来就是包着 CUPS 的）。
  改成**默认注释掉**，并在注释里写清两件事：想给局域网其他电脑按 IPP 加打印机时怎么打开；
  以及那种情况更该直接用 `CUPS_SERVER` 模式，让容器别再起第二套 cupsd
- CHANGELOG 开头称「`/healthz` 的 `version` 是硬编码的接口协议版本，不用来判断构建是否生效」，
  与实际代码不符：三处徽标（`/`、`/admin`、`/healthz`）都取自同一个 `VERSION` 常量，
  没有独立的协议版本号。已改为如实描述
- **新增 `.gitattributes`，锁定换行符为 LF**。Windows 上 Git for Windows 默认
  `core.autocrlf=true`，检出时会把文本文件写成 CRLF；而 `docker build` 送进镜像的是
  **工作区**文件，于是 `docker/entrypoint.sh` 首行变成 `#!/bin/sh\r`，容器里
  `ENTRYPOINT ["/opt/gateway/entrypoint.sh"]` 直接起不来，报
  `sh: 1: /opt/gateway/entrypoint.sh: not found` —— 报错与换行符看不出关联。
  同容器内对照实测：CRLF 脚本 `not found`，LF 脚本正常。同一条也保护 `app/build.sh`

### 验证

- 服务端单元测试实跑 427 项全绿，分项 108 / 157 / 23 / 33 / 67 / 14 / 25
- App `PrefsTest` 20 项全绿（`assembleRelease testDebugUnitTest`）
- **Docker 镜像实机构建 + 容器实跑**（armv7 设备，Amlogic S805 / Armbian）：
  镜像 331MB；容器起来后 `entrypoint.sh` 的 `cupsd -t` 自检通过、容器内 cupsd
  `scheduler is running`；`/healthz` 返回 200；容器内依次实测
  上传（`/api/upload` → 200，`pages: 3`）、预览（`/api/preview` → 200，
  `mode: vector`，纸面 595.28×841.89 点）、取图（`/img` → 200，
  16.9KB 合法 PNG，596×842 RGB）。容器内 `gs` `pdftoppm` `pdfinfo` `lp`
  `lpstat` `python3` `cupsd` 均在位 —— 依赖表与 Dockerfile 实际装的东西一致

### 同期并入：E2E 落盘队列改为按需建删

> 本条不涉及服务端与 App 代码，设备上部署的仍是 v3.10；改动集中在验证脚本与文档。

### 变更

- `verify_e2e.py` 新增 `--setup` / `--teardown`：落盘队列 `GW_TEST` **平时不留在设备上**，
  跑端到端时按需创建、跑完删除。队列缺失时的提示也不再只说「无法验证」，
  而是给出两条可执行路径（脚本自建 / 手工建）
- 建队列必须带 `-m raw`。不带 PPD，CUPS 只搬运格式、不跑滤镜，落盘的 `.prn`
  才是排版引擎的真实产物；省了它 CUPS 会转去「尽力自动配驱动」，
  观测到的东西就不可信了 —— 测试等于没测

### 背景：测试队列为什么不能常驻

它会从**两个通道**同时泄露出去，而两个通道的后果都不轻：

1. **mDNS 自动发现** —— CUPS 把每个队列都做成实例（`GW_TEST @ <主机名>`），
   macOS / Windows 的添加打印机列表里就多出一条看不出区别的条目。
   它的 TXT 是 `ty=Unknown` / `product=Unknown`，这是唯一能分辨它的破绽
2. **网关自己的网页面板** —— 队列下拉来自 `list_printers()` 的 `lpstat -p -d`，
   抓的是**全部**队列，前端不做过滤。手滑选中它的结果是作业落进 `/tmp/gwtest`：
   **CUPS 报成功、页数也对，就是物理不出纸**，属于最难自查的一类失败

后端文件 `gwtest` 自身不广播、不出现在任何队列列表里，留着零副作用。
所以「**后端常驻、队列按需**」是这套组合里最省事的姿势。

### 文档

- README「没有实体打印机也能试」：修正 device URI 写法为 `gwtest:/test`
  （后端约定是 `gwtest:/任意后缀`，落盘目录由 `GW_TEST_OUTDIR` 决定、默认 `/tmp/gwtest`，
  原先写成 `gwtest:/tmp/gwtest` 容易被误读成「后缀即目录」），并补上 `-m raw` 的理由

## [v3.10] — 扫码判定稳健化 + 二维码可放大

### 修复

- **扫码报「不是有效的连接二维码」，但粘贴同一个链接就能连上** ——
  App 之前开了 ML Kit 的 `enableAllPotentialBarcodes()`：它会连「检测到但**没能解码**」
  的候选一起返回，而这类候选的 `rawValue` 是**空串而不是 null**。旧代码取
  `firstOrNull { it.rawValue != null }`，于是挑中一个空串直接判失败，而且识别到一次
  就退出，**没有第二次机会**。改为遍历全部候选、只认能解析成连接码的那些；
  都不合格就继续扫下一帧（不报错、不退出），并把**实际扫到的内容**显示在屏幕上
- **管理页二维码扫不动** —— 预览被压在 132px，而公网码 64 字符 = QR 版本 5（37 模块）
  + 静区，折算只有 3.2px/模块（实测栅格化到 96px 就已经解不出来）。
  预览图支持**点开放大到 320px**（7.8px/模块），斜拍、隔远都还有余量
- `verify_img_auth.py` 把版本号写死成 `v3.8 0919`，之后每发一版都会误报失败 ——
  改为判定「版本 ≥ v3.8」

### 新增

- **App 自证构建版本**。起因是一次真实排查：扫码修复早就写进源码，但**从未重新构建**，
  手机上跑的还是旧包，「修复」在原样复发。让包自己报出版本之后，
  「装没装上」从一次来回猜测变成一眼可见

### 测试

- 真解码验证改为**双解码器**：以 zxing-cpp（严格标准实现）为准，OpenCV 只作补充。
  原因：OpenCV 的 `QRCodeDetector` 有盲区 —— 一个**完全合法**的矩阵（8 个掩码里
  只有罚分最优的那个），zxing-cpp 读得出、OpenCV 读不出，两者差别仅是 payload 里
  一个数字。拿它当唯一判据，测试会随数据飘
- 新增 `verify_admin_qr.py`：带 Cookie 抓 `/admin` → 取页面上的预览图 → 抓 SVG →
  还原成位图 → 真解码。验的是「浏览器拿到的东西」，而不是「服务端算出来的矩阵」

## [v3.9] — 二维码贴纸

管理页多一张卡片：把**内网 / 公网 / App 下载**三个连接码排成贴纸印出来，
贴在打印机旁让人扫。

### 新增

- `pg_sticker.py` —— 贴纸排版与出纸。三种版式（整页 1 张 / A5 两张 / A6 四张），
  排布自适应（满排优先，只有码小到一定程度才允许「2+1」那种孤格）
- `POST /admin/api/sticker` 生成贴纸 PDF 并**注册成一个打印作业**，前端带着 job id
  跳进打印面板 —— 不绕过面板直接推队列，预览 / 选纸盒 / 改份数这些能力面板里本来就有
- `GET /admin/qr.svg?kind=lan|wan|app` 单码预览，内容**由服务端按 kind 现算**，
  不接受前端传 URL（否则这就是个开在管理页后面的任意二维码接口）

### 关键取舍

- **用 PIL 栅格而不是 reportlab 矢量**：设备上的 Noto CJK 是 CFF/PostScript 轮廓，
  reportlab 的 `TTFont` 只认 TrueType 的 `glyf` 表，直接 `TTFError`。走 PIL 顺带
  还赚一条 —— 整数像素方块对二维码比矢量细线更稳，低分辨率光栅化抹不掉整条线
- 出纸存 **1-bit + CCITT G4**（灰度会被 PIL 存成有损 JPEG，模块边缘起振铃），
  页面 `resolution` 反算成 299.96 而**不是 300**（整数 300 得到 595.20pt，
  差 0.08pt 就够打印驱动做一次全局重采样）

## [v3.8] — 外网预览不再裂图、App 协议不再被降级

### 修复

- **外网打开打印页整片裂图** —— 页面里预览图的 `<img src="/img?…">` 是唯一一个
  没走 `api()` 补口令的请求，公网端口上必然 401。补上口令后闭环
  （顺带澄清一个容易误判的点：`/admin/qr.svg` 虽然也在 `/admin` 路径下，
  但管理页靠 **Cookie** 鉴权，`<img>` 能正常带上凭据，**不是 401**）
- **App 把 `https://` 降级成 `http://`** —— 地址规范化原先把协议钉死成 http，
  于是扫码配好的公网地址（8443 是 TLS 端口）被拿明文去打，握手失败、连接被关，
  表现为**整页黑屏**。改为**显式写了协议就照用**（大小写不敏感，输出统一小写），
  没写才补 http —— 老配置（没写协议）行为完全不变
- **相机取景框全黑，还提示「这台设备没有可用的相机」** ——
  `ProcessCameraProvider.bindToLifecycle` 内部第一件事就是主线程校验，而
  `addListener` 传的是自建线程池（变量名还叫 `mainExecutor`，其实是后台线程），
  每次绑定都抛异常被 `catch` 吞掉、把预览设成 `GONE`。改为
  `ContextCompat.getMainExecutor`

## [v3.7] — 公网入口不再被扫描器打死

### 修复

- **TLS 端口在听、却永远连不上** —— 标准库 `SSLSocket.accept()` 是
  「accept + 同步握手」，而握手被放在了 accept 循环里：一条**连上来就不发
  ClientHello** 的连接（扫描器每天都在干）能把整个循环卡死，公网入口就此永久失效，
  重启前谁都进不来。改为 **accept 循环只做 accept，握手落在每连接线程里做并限时 8 秒**
- **`request_queue_size` 一直没生效** —— `TCPServer.__init__` 内部走
  `server_bind()` → `self.listen(self.request_queue_size)`，构造完成之后再赋值已经晚了。
  改为**类属性** `SERVER_BACKLOG`

## [v3.6] — 公网 HTTPS 访问

新增域名 + Let's Encrypt 证书 + DDNS 的完整链路，让手机在**蜂窝网**下也能扫码打印。

### 新增

- `pg_dns.py` — DNS 客户端。公网 IP 探测多源兜底；DNSPod 旧版 Token 与腾讯云 TC3 签名两套实现。
  **只提供「查一条 / 写一条」**，不提供列全部记录的能力（根域记录可能有别的程序在维护）
- `pg_admin.py` — 管理端支撑：凭据存储与掩码回显、acme.sh 封装、证书状态解析、DDNS 同步、`/admin` 页面
- `pg_ddns.py` — 定时任务入口（DDNS 同步 / 证书续期 / 状态查询）
- 网关新增 `--admin-token` / `--tls-port` / `--tls-bind` / `--tls-cert` / `--tls-key`
- 双栈 TLS 监听（显式关闭 `IPV6_V6ONLY`，否则只收 IPv6 而端口映射是 IPv4）
- 口令策略分离：公网端口强制口令，内网明文免密（二维码装不下口令）

### 修复

- **状态页 TLS 误报未启用** —— `collect_status` 的 `tls_enabled` 默认 `False` 且调用方都不传，
  导致证书签好、端口在听，CLI 仍显示 `enabled:false`。改为**实况探测**：从服务单元抽端口 + 实连一次，
  连得上才算启用
- **历史报错赖着不走** —— 凭据填错时记下的报错，在改对之后仍永远挂在状态页。
  改为每轮跑通即清除（`save_state` 支持传 `None` 表示删键）
- 证书剩余天数按 **UTC** 计算（原先用 `mktime` 按本地时区解释 GMT，差 8 小时）

### 已知限制

- DNSPod 免费版 TTL 下限 600 秒 → 家宽重拨后最坏约 10 分钟公网访问不可用
- 证书只能走 DNS-01（家宽 80/443 被封），需要域名商支持

## [v3.5] — 小册子排版修复

修掉两条会让小册子**印错**的硬伤。

### 修复

- **选纵向时整本缩小 50%** — `build_plan` 里 `if booklet: rotate_sheet=True` 被随后的
  `elif orientation == "portrait": rotate_sheet=False` 推翻，纸面变竖、拼版随之缩半。
  改为小册子强制横向，不被方向选项推翻
- **小册子印成散页** — 双面档位原样透传，用户不显式选双面就按单面印，骑马订的背面逻辑全废。
  改为 `validate()` 归一化强制 `two-sided-short-edge`（翻面轴必须与折线平行）；
  只打正面 / 只打背面的子集则强制单面。界面同步锁住方向与双面控件并显示实际取值

## [v3.4] — 小册子真预览提速

预览的**拼版分辨率与出纸分辨率分离**。

- 预览按 `PREVIEW_BUILD_DPI`（= 2× 预览 dpi = 144）构建，出纸按 `spec.dpi`（默认 300）
- 10 页小册子预览耗时 **20.4s → 7.0s**，预览图尺寸完全不变，99.4% 像素完全相同
- 代价：先预览再打印时，那次出纸档构建躲不掉（约 18s），只是从预览时刻挪到了打印时刻

## [v3.3] — 小册子界面图标化

- 装订方向与打印面改用**图标按钮**，旁边实时给出缩略示意（页号取自与实印同一个
  `booklet_sides()`，图不可能与印出来的东西不一致）
- 图标约定：实线纸 = 这一面会印、虚线纸 = 不印、橙色竖条 = 订口所在侧
- 新增 `verify_booklet_ui.js`（27 项）：不需浏览器，从页面里抠出前端函数块套 DOM 桩核对

## [v3.2] — 预览并行渲染

- 预览按页拆片**并行渲染**（默认 3 片 —— 4 核特意留 1 核给 HTTP 服务，另有一道全局闸限制
  同时在跑的 `pdftoppm` 总数）
- 10 页 A4：3.4s → 1.5s；40 页：13.0s → 4.8s，且输出与单进程**逐字节一致**
- 页数少时自动退回单进程（避免进程启动开销），结果按设置哈希缓存

## [v3.1] — 首次开源

- 服务端 `print_gateway.py` + 引擎三模块（`pg_engine` / `pg_layout` / `pg_decor`）
- 纯标准库二维码生成器 `gen_qr.py`
- 安卓自助打印 App：WebView 壳 + 相机扫码连接 + 「打开方式」接收 PDF/图片
- 服务端托管 App 下载（`GET /app`），形成「扫码 → 下载 → 安装 → 扫码连接」闭环

---

## 早期版本（未开源）

- **v2.0** — 完整对齐 WPS 打印面板：拼版 / 小册子 / 页边距 / 水印页码 / 裁剪 / 分割 /
  批量合并 / 实时预览；自建拼版引擎取代有问题的 `pdftopdf`
- **v1.x** — 起步：扫码上传、单页送 CUPS、基础面板
