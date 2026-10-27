#!/usr/bin/env python3
"""一键启动 Ricardo AI 工具的本地服务。

如果根目录带有 ``muse-register``，启动器会自动把 Muse 作为第四个服务
加入；没有该目录的精简包仍可只启动原有三个服务。

默认只启动尚未运行的服务，不会终止或重启用户已有进程：

    python start_all.py              # 启动并等待健康检查通过
    python start_all.py --open-browser
    python start_all.py --foreground # 当前终端持续监管子进程

服务仍保持各自的目录、虚拟环境、数据库和端口，启动器只负责编排。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "runtime" / "logs"


def _change_2fa_directory() -> Path:
    """定位独立 5033 项目；可用 CHANGE_2FA_DIR 覆盖默认位置。"""
    override = os.environ.get("CHANGE_2FA_DIR", "").strip()
    candidates = [
        Path(override) if override else None,
        ROOT / "change-2fa-password-gpt-auto",
        ROOT / "Change-2fa-Password-GPT-Auto",
        ROOT.parent / "change-2fa-password-gpt-auto",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "server.py").is_file():
            return candidate
    # 留下一个明确的可诊断路径；_start() 会输出“目录不存在”。
    return ROOT / "change-2fa-password-gpt-auto"


@dataclass(frozen=True)
class Service:
    name: str
    directory: Path
    script: str
    port: int
    health_path: str
    extra_args: tuple[str, ...] = ()

    @property
    def python(self) -> Path:
        if os.name == "nt":
            candidate = self.directory / ".venv" / "Scripts" / "python.exe"
        else:
            candidate = self.directory / ".venv" / "bin" / "python"
        return candidate if candidate.exists() else Path(sys.executable)


SERVICES = (
    Service(
        "主面板",
        ROOT / "gpt-at-refresh-lite",
        "start_webui.py",
        8768,
        "/api/health",
        ("--no-browser",),
    ),
    Service(
        "GPT 注册机",
        ROOT / ".audit-turb-gpt-free-register" / "repo",
        "web.py",
        5050,
        "/",
    ),
    Service(
        "密码/2FA",
        _change_2fa_directory(),
        "server.py",
        5033,
        "/api/health",
        ("--no-browser",),
    ),
)

MUSE_DIRECTORY = ROOT / "muse-register"
if (MUSE_DIRECTORY / "web.py").is_file():
    SERVICES += (
        Service(
            "Muse 注册机",
            MUSE_DIRECTORY,
            "web.py",
            8801,
            "/api/health",
        ),
    )


def _url(service: Service) -> str:
    return f"http://127.0.0.1:{service.port}{service.health_path}"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _healthy(service: Service) -> bool:
    try:
        with urllib.request.urlopen(_url(service), timeout=1.5) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def _start(service: Service) -> subprocess.Popen[bytes] | None:
    if _port_open(service.port):
        state = "健康" if _healthy(service) else "端口已占用（健康检查未通过）"
        print(f"[{service.name}] 已运行：{service.port}（{state}）")
        return None

    if not service.directory.is_dir():
        print(f"[{service.name}] 跳过：目录不存在：{service.directory}")
        return None

    script_path = service.directory / service.script
    if not script_path.is_file():
        print(f"[{service.name}] 跳过：找不到启动文件：{script_path}")
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{service.port}.log"
    log = log_path.open("ab", buffering=0)
    command = [
        str(service.python),
        str(script_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(service.port),
        *service.extra_args,
    ]
    kwargs: dict[str, object] = {
        "cwd": str(service.directory),
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "close_fds": os.name != "nt",
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        log.close()
        print(f"[{service.name}] 启动失败：{exc}")
        return None
    # 子进程已独立接管日志文件；父进程不再持有文件句柄。
    log.close()
    print(f"[{service.name}] 启动中：端口 {service.port}，日志 {log_path}")
    return process


def _wait_for_health(service: Service, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(service):
            print(f"[{service.name}] 健康检查通过：{_url(service)}")
            return True
        time.sleep(0.4)
    print(f"[{service.name}] 健康检查超时，请查看 runtime/logs/{service.port}.log")
    return False


def _run_foreground(children: list[subprocess.Popen[bytes]]) -> None:
    if not children:
        print("没有由本次启动器创建的新进程，退出。")
        return
    print("前台监管中，按 Ctrl+C 可停止本次启动器创建的服务。")
    try:
        while True:
            dead = [p for p in children if p.poll() is not None]
            if dead:
                print("有服务进程已退出，请查看对应日志。")
                return
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止本次启动器创建的服务……")
        for process in children:
            if process.poll() is None:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except OSError:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="一键启动 Ricardo AI 工具的本地服务")
    parser.add_argument("--open-browser", action="store_true", help="健康检查通过后打开主面板")
    parser.add_argument("--foreground", action="store_true", help="保持当前终端监管本次创建的服务")
    args = parser.parse_args()

    created: list[subprocess.Popen[bytes]] = []
    for service in SERVICES:
        child = _start(service)
        if child is not None:
            created.append(child)

    # 对所有服务逐一等待，不能只筛选当前已监听的端口：新启动的进程需要几秒
    # 才会开始监听，否则启动失败会被误判成“无需检查”。
    health_results = [_wait_for_health(service) for service in SERVICES]
    healthy_main = all(health_results)
    if args.open_browser and healthy_main:
        webbrowser.open("http://127.0.0.1:8768/")
    if args.foreground:
        _run_foreground(created)
    return 0 if healthy_main else 1


if __name__ == "__main__":
    raise SystemExit(main())
