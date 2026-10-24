# AGENTS.md —— Agent 交接提示词

> 这份文件写给 AI Agent。用户把本项目交给你时，请按本文档执行安装与配置。
> 用户也可以直接把本文件内容粘贴给任意本地模型（Kimi Code / Cursor / Claude Code 等）作为指令。

---

## 本分支的 macOS fork 身份

本分支个人版 App 名称为 `Claude切换器-fork.app`,核心安装为 `~/.local/bin/claude-switch-fork.sh`,运行状态/账号库为 `~/.claude-switcher-fork/`。两版 Claude 活动数据统一为 `~/Library/Application Support/Claude/`,不传入 `--user-data-dir`,不再启动旧 `Claude-fork` 目录。

两版的账号库和 current 仍独立。所有账号切换必须经过 `~/.claude-switcher-sessions/session-coordinator.py`。公共 `active-edition` 标记默认目录的所有者;切换版本时先安检并完全退出 Claude,再把原活动现场移动到 `parked/<版本>/`,恢复目标现场到默认目录,最后调用该版账号切换。公共锁跨两版生效;发现 `transfer.json` 说明交接中断,须保留现场并先恢复,不得猜测/删除。

安装器将原版核心保存于 `~/.local/share/claude-switcher-upstream/claude-switch.sh`,在原版 `~/.local/bin/claude-switch.sh` 放协调转发入口。只允许封装入口,不得篡改原版后端的功能代码或账号存档。原版自更新写入后端位置,不得覆盖协调入口。上游安装器若重新覆盖入口,须重跑个人版安装器恢复协调层。

旧个人版活动目录 `~/Library/Application Support/Claude-fork/` 仅在安装迁移时完全退出 Claude 后移动到 `~/.claude-switcher-sessions/parked/fork/`,两个 accounts/ 均不迁移。`claude-switch-fork.sh release` 交还原版活动现场且不启动 Claude;卸载个人版必须先交还,保留协调组件、两版账号库与暂存现场。

三个服务的安装 Label 和 plist 文件名分别为 `local.claude-bodyguard-fork`、`local.tz-watchdog-fork`、`local.claude-update-fork`;仓库三个 plist 模板仍保留原文件名。root 脚本只安装于 `/Library/Application Support/claude-switcher-fork/`,必须为 root:wheel、755。保镖和看门狗仅在 active-edition 为 fork 时保护共享默认目录,后台不交换数据。更新源固定为 `RandallRain/claude-switcher:main-fork`,更新须同步并校验公共协调器,拒绝回退到旧的独立活动目录版本。

macOS 时区、Locale、IPv6、代理 TUN 和系统 Keychain 仍共享。修改共享设置时检查原版活跃状态和看门狗目标冲突;两个切换器轮流使用。绝不能绕过协调器分别交换同一个活动目录,也不要改变任何 accounts/ 内的存档结构。

## 你的任务

帮助用户在 macOS 上安装并配置「Claude 切换器」：一个带环境安检的 Claude 账号启动/切换工具。目标状态是：**用户双击桌面图标，14 项安全检查全部通过，可以正常启动 Claude**。

## 开始之前：必须先确认的前置条件

逐项检查，缺哪项就先补哪项：

1. **macOS**（Apple Silicon 或 Intel 均可）
2. **代理客户端**已安装且代理可用，支持 Clash Verge / Shadowrocket / sing-box / ClashX / Surge 任一（TUN 可用）。Clash Verge 的配置目录应存在于 `~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/`；Shadowrocket 以 VPN 配置形式存在（`scutil --nc list` 可见）
3. **Claude 桌面版** 在 `/Applications/Claude.app`
4. **python3** 可用（`python3 --version` 能输出版本号；没有则引导用户运行 `xcode-select --install`）
5. **当前出口 IP 所在国家不能是 CN**——先跑 `curl -s https://www.cloudflare.com/cdn-cgi/trace | grep loc=`，返回的 `loc` 不是 `CN` 才能继续。是 CN 就停下，让用户先把代理调好（安装程序会按出口 IP 自动锁定地区目标：国家/时区/地区格式三件套，裸连时装机会把它们锁错）

## 安装步骤

首选引导用户运行安装器（双击 `installer/安装.command`，或终端执行它）。如果安装器环境受限，按下面的手动等效步骤执行：

共享活动目录版本的手动安装还必须安装公共协调器、封装原版入口并执行 `prepare` (参见本文件开头和安装器用户态步骤);不能只复制旧版四个核心工件,也不得直接调用上游后端切换账号。

1. 把 `core/claude-switch.sh` 复制为 `~/.local/bin/claude-switch-fork.sh`（可执行），把 `core/stun-probe.py`、`core/clash-rules.py`、`core/clash-global-rules.js` 放到 `~/.claude-switcher-fork/`
2. 探测代理客户端并写进脚本顶部 `PROXY_MODE="..."`：存在 Clash Verge 配置目录 → `clash-verge`；否则 `scutil --nc list` 有 Shadowrocket VPN 配置 → `shadowrocket`；都没有则让用户在 clash-verge/shadowrocket/sing-box/clashx/surge/auto 中选择
3. Clash Verge 模式下，从 `clash-verge.yaml` 的 `proxy-groups:` 找第一个 `type: select` 的组名写进 `PROXY_GROUP="..."`（其他客户端跳过）
4. 按出口 IP 自动锁定地区目标：`curl -s ipinfo.io/json` 的 country → `COUNTRY_TARGET`，timezone → `TZ_TARGET`，country 经映射表（US→en_US、JP→ja_JP、TW→zh_TW、HK→zh_HK、SG→en_SG、GB→en_GB、DE→de_DE、FR→fr_FR、KR→ko_KR、AU→en_AU、CA→en_CA，其余→en_US）→ `LOCALE_TARGET`，三项都写进脚本头部
5. 参照 `daemons/` 目录的模板，物化保镖与时区看门狗脚本（把 `__HOME__` / `__TZ_TARGET__` / `__PROXY_MODE__` / `__USER_UID__` 替换为实际值），用 sudo 装入 **root 所有的** `/Library/Application Support/claude-switcher-fork/`（chown root:wheel、chmod 755，用户态不可写防提权）；`local.claude-bodyguard` / `local.tz-watchdog` 两个 LaunchDaemon (安装名称添加 `-fork`) 安装到 `/Library/LaunchDaemons/`（ProgramArguments 已指向个人版系统目录;复制时 plist 文件名也必须加 `-fork`）。自动更新 `local.claude-update` 是**用户态 LaunchAgent**：plist 以 `local.claude-update-fork.plist` 放 `~/Library/LaunchAgents/`，用 `launchctl bootstrap gui/$(id -u)` 注册，不走 root（模板 `daemons/local.claude-update.plist`，每 12 小时跑一次 `update --auto` (来源 `RandallRain/claude-switcher:main-fork`)）
6. 现场编译界面 App（需要 Xcode 命令行工具，必须带 `-target` 否则新 SDK 产物在老系统拒启动）：
   ```bash
   swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13.0" app/main.swift -o Claude切换器-fork.app/Contents/MacOS/ClaudeSwitcher-fork -framework SwiftUI -framework AppKit
   ```
   图标用 `assets/icon-2048.png` 经 sips 生成 iconset 再 `iconutil -c icns` 打进 `Contents/Resources/AppIcon.icns`，`Info.plist` 声明 `CFBundleIconFile`，最后 `codesign --force --deep -s -`
7. Clash Verge 模式运行 `claude-switch-fork.sh write-global-rules`，将规则写入「全局扩展脚本」(Global Extend Script / `profiles/Script.js`) 并重载。保留用户原 `main`，按订阅选择代理组；三条 STUN 端口代理规则后有 `REJECT` 后备，并包含双栈参考出口路由。不要再往全局 Merge 写 `prepend-rules`。其他客户端提示用户自行添加域名、STUN 端口及参考出口规则
8. 时区与 IPv6（已是目标值则跳过对应命令）：`systemsetup -settimezone <TZ_TARGET>`、`networksetup -setv6off Wi-Fi`（需管理员）
9. 收尾自检：运行 `~/.local/bin/claude-switch-fork.sh checkdetail` 确认各项状态

macOS 的 `check`、`checkdetail` 和 `fix` 共用一轮双栈出口/STUN 快照。出口 IP 在 UI 分别显示 IPv4、IPv6 及国家，单个地址族未获取会明确标注；任一观测出口国家异常或全部未获取均失败。时区、地区项显示实际系统时区与 Locale（包含地区名称），不是仅显示目标值。真实地址只用于本机 UI/私有缓存，不要复制到日志或交接输出。

WebRTC 按是否观测到非代理 IP 判色：同版本匹配、部分或全部探针无结果均为绿色，UI 必须说明已匹配的地址族及未取得结果的范围；全部无结果要注明 UDP 出口未验证，不得写成已确认“UDP 不出网、零暴露”。地址不匹配、响应格式异常、探针执行异常、有效地址缺少同版本参考或代理策略无法核验仍为红色。

日常维护命令：`check`（静默总检）/ `checkdetail`（14 项逐项）/ `fix`（一键修复）/ `switch A|B`（切换账号）/ `set-region`（换国家节点后重锁地区目标）/ `fingerprint`（设备指纹体检：字体地区特征/GPU/虚拟机；Canvas/WebGL 用 Web 检测页）/ `update [--auto]`（自动更新：`--auto` 由用户态 LaunchAgent 每 12 小时调用、输出落 `~/.claude-switcher-fork/update.log`；手动跑则直接打印。流程：拉取个人仓库 main-fork → 逐项 SHA256 校验工件（对照仓库 SHA256SUMS，不符即中止）→ 剔除 5 个配置行并连同辅助工件比对去重 → 备份核心与探针/全局规则/公共协调器工件 → 注入配置并同步辅助文件 → 守护脚本仅比对并提示 → 自检，含任何 `=fail` 整组回滚）

## 独立代理诊断

`bash core/claude-switch.sh diagnose-proxy --config /path/to/private-proxy.json` 只验证明确指定代理到 Claude 网页/API 的无认证传输，详见 README。它可直接从仓库运行，不适用上述安装/启动前置条件，不启动或关闭应用，不创建账号状态、不修改系统设置。输出 JSON；401/403 分别标注认证或访问状态，传输到达不等于账号可用、启动许可或防泄漏通过。不要为运行诊断先安装后台或改 TUN/时区。

隔离回归：`python3 -m unittest discover -s tests -v`。测试使用本机假 curl 和隔离 HOME，不使用真实登录资料或代理。

## 验收标准（全部满足才算装完）

- 终端执行 `~/.local/bin/claude-switch-fork.sh checkdetail` 输出 14 行且全部 `=ok`
- 双击桌面 App，界面全绿，能正常启动 Claude
- `launchctl print system/local.claude-bodyguard-fork`、`system/local.tz-watchdog-fork` 与 `gui/$(id -u)/local.claude-update-fork` 均能查到（服务已注册）
- `/Library/Application Support/claude-switcher-fork/` 下两个守护脚本属 root:wheel、权限 755，当前用户不可写
- 切换一次账号 B 再切回 A，A 的登录态完好

## 排障手册（以下全是实战中踩过的坑，按症状直接查）

| 症状 | 根因 | 处理 |
|---|---|---|
| TUN 网卡消失、出口变回国内 | **Clash Verge 更新/异常重启会把 `enable_tun_mode` 重置为 false**（生产环境已发生 4 次） | 退出 App → 改 `verge.yaml` 为 true → 重启 App。切换器的一键修复已内置此逻辑 |
| 时区总是自己变回去 | macOS 自动时区或未知程序改动（生产环境已抓 8 次） | 看门狗会自动回滚；查 `~/Library/Logs/tz-watchdog.log` 留痕 |
| 检查结果显示 0/11、数据为空 | 中文日志被 `cut -c` 按字节截断产生非法 UTF-8，Swift 严格解码整体失败 | 脚本必须 `export LC_ALL=en_US.UTF-8`；界面端用容错解码 `String(decoding:as:)` |
| 修改 App 后界面没变化 | 旧进程没死：App 关窗不退 + `pkill` 匹配中文路径会因 Unicode NFD 编码失配而静默失败 | 用 `pkill -x ClaudeSwitcher-fork`（按进程名）；App 必须实现 `applicationShouldTerminateAfterLastWindowClosed` |
| headless Chrome 做 WebRTC 检测无结果 | headless 模式 ICE 子系统不产出候选 | 不要走浏览器路线，用本项目的纯 Python STUN 探针 |
| 代理节点报错但流量没泄漏 | 节点被重置时隧道内请求失败≠泄漏；Fail-Closed 下流量只断不绕 | 换线路即可 |
| WebRTC 检测失败但 TUN 正常 | 国内 STUN 域名可能命中 DIRECT，或同版本参考不可达/出口分流 | 运行 `fix`，通过 Global Extend Script 添加三端口代理及阻断后备规则，并核验实际内核；勿使用旧 Merge prepend-rules。IPv4/IPv6 分别比较。无结果显示绿色“未发现非代理 IP”，同时注明未验证；格式/执行异常仍为红色 |
| 切换账号后对方看到"新设备" | 正常：快照隔离意味着每个账号有独立设备指纹，这正是设计目标 | 无需处理 |

## 安全红线（Agent 必须遵守）

1. **绝不在出口国家与目标不一致时启动 Claude**——任何时候先验 cloudflare trace 的 `loc` 与脚本头部 `COUNTRY_TARGET` 一致；换了国家节点先跑 `claude-switch.sh set-region` 再启动
2. 切换账号前必须经公共协调器确认所有权,并完全退出使用默认目录的 Claude 进程，再交换数据目录
3. 不要在日志、文档、Issue 里暴露用户的住宅/代理 IP 与任何凭据
4. 不要改动 `~/.claude-switcher-fork/accounts/` 下的存档结构
5. **维护纪律**：`core/`、`daemons/`、`installer/` 下任何文件改动后，提交前必须运行 `scripts/gen-sums.sh` 重新生成根目录 `SHA256SUMS` 并同提交——自动更新靠它校验工件完整性，清单与代码不一致会导致所有用户更新失败
6. **守护进程脚本只在 root 目录**：`/Library/Application Support/claude-switcher-fork/` 下的保镖/看门狗属 root:wheel，任何功能都不要改回写 `~/.local/bin/` 下的同名脚本（用户可写=提权面）；需要变更时引导用户重跑安装器

## Windows 版（windows/ 目录, Beta）

Windows 用户请使用 `windows/` 下的 PowerShell 版本。关键差异与注意事项：

- **文件编码**：所有 `.ps1` 文件必须保持 **UTF-8 with BOM**（Windows PowerShell 5.1 无 BOM 会把中文读成乱码）。编辑后若中文变乱码，重新加 BOM。
- **时区 ID 体系不同**：Windows 用 `Eastern Standard Time` 这类 ID，安装器负责把 IANA 时区（America/New_York）映射过去，见 `install.ps1` 的 `$tzMap`。
- **权限模型**：`install.ps1` 需要管理员运行（注册计划任务/锁时区/关 IPv6）；日常使用中「一键修复」通过 `Start-Process -Verb RunAs` 触发 UAC 弹窗。
- **保镖实现**：用计划任务（AtLogOn 触发）启动隐藏 PowerShell 常驻进程，10 秒巡检，与 Mac 版行为一致。
- **robocopy 退出码**：0-7 都是成功，8+ 才是失败——判断切换成败时不要按常规退出码判断。
- **账号数据目录**：`%APPDATA%\Claude`（不是 macOS 的 `~/Library/Application Support/Claude`）。
- **已知限制**：WPF 界面为第一版重写，未经大规模真机测试；Clash Verge 的 TUN 开关被更新重置后在 Windows 端需手动重开。
- SmartScreen 首次运行拦截属正常现象（未签名脚本），引导用户「更多信息 → 仍要运行」。

## Linux 版（linux/ 目录, Beta）

Linux 用户使用 `linux/` 下的 bash 版本。**开始之前必须先理解最大的前提差异**：

- **Claude 官方没有 Linux 桌面版**——使用形态是「网页版(浏览器) + Claude Code CLI」。因此：
  - 账号切换切换的是**浏览器配置目录**（`~/.config/google-chrome` 或 `~/.config/chromium`），即切换 Web 登录态；切换前必须完全退出浏览器
  - 保镖只终止 `claude` CLI 进程，**不要让它去杀浏览器**（网页端保护交给 TUN + 置顶规则）
- **多客户端**：`PROXY_MODE` 支持 clash-verge / mihomo / sing-box / v2raya / nekobox / hiddify / wireguard / other / auto；非 Clash 客户端不做配置改写，只做 fake-ip 行为检测并提示用户自行加 DOMAIN-SUFFIX 规则；`wireguard` / `other` 为通用虚拟网卡启发式判定
- **地区自适应**：`COUNTRY_TARGET` / `TZ_TARGET` / `LOCALE_TARGET` 由安装器按出口自动写入；`CN` 出口拒绝安装；换国家节点后手动改这三个值（Linux 版暂无 `set-region` 命令）
- **常驻服务**：systemd 用户单元（`~/.config/systemd/user/`，免 sudo、登录自启）
- **系统设置**：时区用 `timedatectl set-timezone`（sudo）；IPv6 用 `sysctl -w net.ipv6.conf.all.disable_ipv6=1` + `/etc/sysctl.d/` 持久化
- **DNS 查询工具**：`dig` → `nslookup` → `getent` 逐级回退（部分发行版默认无 dig，脚本已内置回退，勿改成 dig 硬依赖）
- **界面**：无 GUI，终端菜单（`claude-switch menu`）+ CLI 参数
- **发行版差异**：安装器不区分发行版，但依赖 systemd；非 systemd 发行版（如 Alpine/void 部分配置）需要手工替代保镖常驻方式
- 配置目录路径：Clash Verge Linux 版配置在 `~/.config/io.github.clash-verge-rev.clash-verge-rev/`（脚本已兼容两种常见路径）

## 文件索引

- 核心逻辑：`core/claude-switch.sh`（安检/切换/修复全在这一个文件）
- 泄漏探针：`core/stun-probe.py`
- Clash 全局规则：`core/clash-rules.py` / `core/clash-global-rules.js`（保留原用户脚本，适用于所有订阅）
- 界面：`app/main.swift`（单文件 SwiftUI）
- 守护进程模板：`daemons/`
- 安装器：`installer/`
- Windows 版：`windows/`（核心 `claude-switch.ps1` / 界面 `SwitcherUI.ps1` / 安装器 `install.ps1` / 保镖 `claude-bodyguard.ps1`）
- Linux 版：`linux/`（核心 `claude-switch.sh` / 保镖 `claude-bodyguard.sh` / 安装器 `install.sh`）
- Web 版：`docs/` 下的 index.html/app.js/style.css（纯静态环境检测页，GitHub Pages 从 docs/ 发布）

## 维护者本机纪律（Agent 必须遵守）

**除非维护者在当次对话中明确要求，否则不得更新/部署维护者本机的已安装版本。** 具体包括：

- 不得覆盖 `~/.local/bin/claude-switch-fork.sh`、`~/.local/bin/local.claude-*.plist`
- 不得重新编译或替换桌面 App（`~/Desktop/Claude切换器-fork.app`）
- 不得改动 `/Library/Application Support/claude-switcher-fork/`、`/Library/LaunchDaemons/local.claude-*`、`~/Library/LaunchAgents/local.claude-*`
- 不得触发本机 `update`、不得执行安装器/卸载器

日常开发默认流程：**只修改仓库文件 → 验证（本地只跑不影响安装态的测试/静态检查）→ 提交推送**。需要真机验证时，先询问维护者是否允许在本机部署。
