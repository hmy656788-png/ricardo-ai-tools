# Ricardo AI 工具 · 2026-10-26 更新包

这是可分享的源码运行包，不是预装 Python 的独立安装程序。保留三个服务的独立进程、配置、数据库和任务队列。

## 本次更新

- 菜单顺序：导入账号 → 运行记录 → 代理池 → 密码 / 2FA → 2FA 验证码 → GPT 注册机。
- Ricardo AI 品牌及 Claude 风格暖白 / 陶土橙配色，支持深色。
- 新增浏览器本地 2FA 验证码工具，支持 Base32 和 otpauth 链接；密钥不上传、不持久化，离开页面即清空。
- 密码 / 2FA 与 GPT 注册机的嵌入界面统一主题、自动适配高度；展示消息不传账号或凭证。
- 删除 GPT 注册机的 TG 交流群入口，保留退出功能。

## 环境要求

- Python 3.11–3.13，建议使用 3.12。三个服务分别建立虚拟环境，不要混用依赖。
- Node.js 20+（满足注册机部分流程的 Node 运行要求，也可用于前端测试和重建）。
- 首次安装需要联网下载 Python 依赖；浏览器相关功能可能还需要下载 Camoufox / Playwright 浏览器。
- 已附带主站构建产物，单独运行主面板无需重建前端。

## 首次安装

在下面三个目录中**分别**打开终端执行安装命令：

- `gpt-at-refresh-lite`
- `turb-gpt-free-register`
- `change-2fa-password-gpt-auto`

macOS / Linux：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell（无需修改执行策略）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

注册机目录里把 `.env.example` 复制为 `.env`，再按需填写自己的配置；macOS / Linux 可运行 `cp .env.example .env`，Windows 可运行 `Copy-Item .env.example .env`。示例已启用仅回环地址免授权模式；远程使用前必须恢复认证并配置访问控制。

## 启动

### 一键启动（推荐）

在 macOS 上可双击包根目录的 `启动全部服务.command`；也可以在终端运行：

```bash
python3 start_all.py --open-browser
```

启动器会先探测 `8768 / 5050 / 5033`，已经运行且健康的服务不会重复启动；新启动服务的日志写入 `runtime/logs/端口.log`。如需让当前终端持续监管本次启动的进程，增加 `--foreground`。三个服务的数据库、虚拟环境、任务队列和配置仍完全独立。

如果 5033 项目不在包根目录，设置环境变量 `CHANGE_2FA_DIR` 指向包含 `server.py` 的目录即可。

分别打开对应终端，在对应目录运行（macOS / Linux 先激活该目录的虚拟环境；Muse 为可选第四个服务）：

```bash
# 在 turb-gpt-free-register 目录
python web.py --host 127.0.0.1 --port 5050

# 在 change-2fa-password-gpt-auto 目录
python server.py --host 127.0.0.1 --port 5033 --no-browser

# 在 gpt-at-refresh-lite 目录
python start_webui.py --host 127.0.0.1 --port 8768

# 可选：在独立的 muse-register 目录
python web.py --host 127.0.0.1 --port 8801
```

Windows 将上述每条命令的 `python` 替换为 `.\.venv\Scripts\python.exe`。

打开 <http://127.0.0.1:8768/>。只需导入账号、运行记录、代理池或本地 2FA 验证码时，只启动主面板即可。嵌入服务未启动时，对应模块不可用。

主站使用 `127.0.0.1:8768`，GPT 注册机嵌入地址保持 `localhost:5050`，密码 / 2FA 为 `127.0.0.1:5033`；可选的 Muse 注册机入口使用 `127.0.0.1:8801`。Muse 服务不随本更新包提供，需单独审计、安装和启动。不要随意把各独立服务的 hostname 改成主站 hostname，以保留现有 Cookie 隔离设计。

## 数据与安全

本包排除了原电脑数据库、真实 `.env`、账号池、导出文件、日志、虚拟环境和浏览器缓存；源码示例也沿用脱敏版本。首次运行创建新数据，不包含个人账号。

只用于自己拥有或明确获授权的账号。不要直接把管理服务暴露到局域网或公网。使用后的数据库、`.env`、导出文件都可能含敏感信息，不能再次直接转发整个文件夹。

已有安装请先备份自己的数据，再在独立新目录解压此包；不要用空配置覆盖自己的 `.env` 或数据库。停止旧服务且确认任务结束后，再切换到新目录。跨版本数据迁移需单独核对。

## 开发与验证

主站前端目录 `gpt-at-refresh-lite/webui/frontend`：运行 `npm ci`、`npm test`、`npm run build`。两个嵌入服务分别在仓库根目录运行 `node --test tests/embedded-ui.test.mjs`。

本次已通过：28 项 TOTP 测试、7 项密码 / 2FA 展示桥接测试、9 项注册机展示桥接测试、前端构建与包内 Python 语法检查。未验证接收者机器的完整依赖安装，也未触发注册、账号修改或付费接码任务。

各项目许可证保留在对应目录的 `LICENSE`；完整文件校验清单见 `MANIFEST.sha256`。
