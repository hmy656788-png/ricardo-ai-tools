# Ricardo AI Tools

公开源码版的本地 AI 工具集合，包含：

- `gpt-at-refresh-lite/`：本地账号凭证管理、访问令牌刷新、代理池、运行记录和浏览器端 TOTP 验证码工具。
- `turb-gpt-free-register/`：独立的本地注册机 WebUI（可选服务）。
- `muse-register/`：独立的 Muse 注册流程 WebUI（可选服务）。
- `start_all.py`：按端口启动本地服务的编排脚本。

## 安全边界

本仓库只适用于你拥有或明确获授权的账号与服务。它不提供绕过登录、绕过多因素认证或恢复他人账号的功能。请遵守目标服务的条款、当地法律和组织安全政策。

公开仓库不包含真实 `.env`、邮箱/代理/银行卡资料、账号导出、Token、数据库、日志、虚拟环境或浏览器缓存。首次运行前请从各目录的示例配置创建本机 `.env`，并确认管理界面只监听 `127.0.0.1`。

## 快速开始

需要 Python 3.11+；前端测试/构建需要 Node.js 20+。

```bash
git clone https://github.com/hmy656788-png/ricardo-ai-tools.git
cd ricardo-ai-tools

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r gpt-at-refresh-lite/requirements.txt

python start_all.py --open-browser
```

如果只使用主面板，也可以直接运行：

```bash
python gpt-at-refresh-lite/start_webui.py --host 127.0.0.1 --port 8768
```

首次运行会在本机生成数据库和日志；这些运行时文件已经被 `.gitignore` 排除。注册机和 Muse 服务需要分别安装各自 `requirements.txt`，并按对应 README 配置。

## 前端开发

```bash
cd gpt-at-refresh-lite/webui/frontend
npm ci
npm test
npm run build
```

## 发布前检查

```bash
git status --short
git ls-files
```

确认输出中没有 `.env`、数据库、日志、邮箱池、Token 或其他个人数据后再提交。若凭证曾经进入 Git 历史，请先撤销/轮换凭证并清理历史。
