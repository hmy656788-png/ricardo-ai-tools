#!/usr/bin/env python3
"""WebUI 一键启动脚本（GPT AT Refresh 精简版：导入账号 + 代理池）：装依赖 → 跑 uvicorn。

用法：
    python start_webui.py             # 默认 127.0.0.1:8768
    python start_webui.py --port 9000 # 自定义端口
    python start_webui.py --host 0.0.0.0 --port 8768  # 内网监听
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from pathlib import Path

# Windows 控制台 GBK 编码兼容：强制 UTF-8 输出
if sys.platform.startswith("win"):
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent


def _port_is_in_use(host: str, port: int) -> bool:
    """启动前给双击场景一个可见提示，而不是让 uvicorn 因端口冲突直接闪退。"""
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_before_exit(message: str) -> None:
    print(f"\n[!] {message}")
    if sys.platform.startswith("win") and sys.stdin.isatty():
        try:
            input("按 Enter 关闭窗口...")
        except (EOFError, KeyboardInterrupt):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8768, help="监听端口 (默认 8768)")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--reload", action="store_true", help="开发模式 (代码改动自动重启)")
    args = ap.parse_args()

    def _missing_runtime_deps() -> list[str]:
        """WebUI 启动真正需要的包：页面框架 + HTTP 客户端。"""
        missing: list[str] = []
        for mod in ("fastapi", "uvicorn", "pydantic", "requests"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            missing.append("curl_cffi")
        return missing

    missing = _missing_runtime_deps()
    if missing:
        print(f"[!] 缺少依赖 {', '.join(missing)}，正在按 requirements.txt 安装 ...")
        import subprocess
        req = ROOT / "requirements.txt"
        try:
            if req.is_file():
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req)])
            else:
                subprocess.check_call([
                    sys.executable, "-m", "pip", "install",
                    "fastapi", "uvicorn[standard]", "pydantic>=2",
                    "requests>=2.31.0", "curl-cffi>=0.7.0",
                ])
        except subprocess.CalledProcessError as exc:
            _wait_before_exit(f"依赖安装失败: {exc}")
            return
        still = _missing_runtime_deps()
        if still:
            _wait_before_exit(
                "依赖安装后仍缺少: "
                + ", ".join(still)
                + "。请手动执行: pip install -r requirements.txt"
            )
            return

    sys.path.insert(0, str(ROOT))
    import uvicorn

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    if _port_is_in_use(args.host, args.port):
        _wait_before_exit(
            f"端口 {args.port} 已被占用。若这是已启动的 WebUI，请直接打开 {url}；"
            "请勿重复双击启动脚本。"
        )
        return

    print(f"\n🔔 GPT AT Refresh WebUI 启动中...")
    print(f"   访问: {url}\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        uvicorn.run(
            "webui.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
        )
    except OSError as exc:
        _wait_before_exit(f"WebUI 启动失败: {exc}")


if __name__ == "__main__":
    main()
