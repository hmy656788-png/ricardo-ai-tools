"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import logging
import os
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（fallback，优先使用 fingerprint.generate_fingerprint() 生成的值）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

# TLS 握手瞬断的识别标记（与 AuthFlow._is_tls_error 保持同一套口径）
_TLS_ERROR_MARKERS = ("curl: (35)", "tls connect error", "openssl_internal", "sslerror")
# curl (97) 是 SOCKS 协商阶段被上游关断；(56) 常是同一阶段的 recv/reset。
# 两者发生在 HTTP 请求尚未送出之前，和 (35) 一样可安全地原 session 有限重试。
_SOCKS_HANDSHAKE_ERROR_MARKERS = (
    "curl: (97)",
    "failed to receive socks response",
    "proxy closed connection",
    "curl: (56)",
    "recv failure",
)


def _is_tls_handshake_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TLS_ERROR_MARKERS)


def _is_retryable_transport_error(exc: Exception) -> bool:
    """只识别请求尚未到达站点前的 TLS/SOCKS 连接层短断。"""
    msg = str(exc).lower()
    return (
        any(marker in msg for marker in _TLS_ERROR_MARKERS)
        or any(marker in msg for marker in _SOCKS_HANDSHAKE_ERROR_MARKERS)
    )


class _TlsRetrySession:
    """给 session 的 get/post 套一层连接层瞬断重试，其余属性原样透传。

    ── 为什么要有这东西 ──
    代理链路会偶发 `curl: (35) TLS connect error ... OPENSSL_internal`，
    连 HTTP 请求都没发出去就炸。2026-08-10 实测（148 轮扫描）：

        发生率            5.4%（8/148）
        与指纹的关系      无 —— chrome146/142/136、safari18_0/15_3、firefox133 都中过
        与域名的关系      无 —— chatgpt.com 3/25、auth.openai.com 1/25，
                          且见过同一轮两个域一起炸（那一路出口链路整个坏了）

    换句话说这是**链路级瞬断**，不是风控、不是指纹问题，摘掉任何一个指纹都没用。

    ── 为什么必须原 session 重试，不能重建 ──
    warmup 那处的重试是重建 session（换出口 IP），因为那时还没 cookie。
    但链路中后段（auth_oauth_init / sentinel / authorize_continue …）session 里
    已经装着 warmup 种的 oai-did 和 csrf，**一重建就全丢，直接变 409 invalid_state**
    —— 那正是上一轮刚修好的病。所以这里只重试，绝不碰 session。

    实测原 session 重试的效果（8 次 TLS35 事件全部捕获后立即重试）：

        恢复 8/8，全部**第 1 次重试就成功**，恢复后 oai-did 仍在 8/8

    ── 为什么包在 session 层，而不是逐个调用点加 try ──
    这个错能打在链上**任意一步**。主人 2026-08-10 那批 10 个号的两次失败就分别
    炸在 `[3/10] auth_oauth_init` 和 `[4/10] sentinel`（后者还被 sentinel_quickjs
    的 catch-all 吞成 "QuickJS 失败/主 token 缺失"，真因全被掩盖）。auth_flow 里
    有 35 处 session.get/post，且 sentinel.py 是直接拿 session 对象自己发请求的，
    逐点打补丁既治不完也漏得到 —— 包在出口这一层才是一次覆盖全部。

    ── 透传安全性（已实测）──
    全项目在 session 上访问的非 get/post 属性只有 cookies(20处) / trust_env(3) /
    proxies(3) / mount(2) / headers(1)，实测包装后全部行为一致：
    cookies.get_dict() / cookies.get() / cookies.jar / 迭代 / __setattr__ 透传均 OK。
    （注：迭代 session.cookies 产出的是 str 而非 Cookie 对象、拿不到 .name，
    这是 curl_cffi **原生行为**，包装前后一致，与本类无关。）
    """

    def __init__(self, inner, retries: int = 2, backoff: float = 1.5):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_retries", max(0, int(retries)))
        object.__setattr__(self, "_backoff", float(backoff))

    # 除 get/post 外的一切读写都直达真 session
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_inner"))

    def _call_with_retry(self, method: str, *args, **kwargs):
        import time

        inner = object.__getattribute__(self, "_inner")
        retries = object.__getattribute__(self, "_retries")
        backoff = object.__getattribute__(self, "_backoff")
        fn = getattr(inner, method)

        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                # 只兜 TLS/SOCKS 协商阶段瞬断：HTTP 错误码、超时、业务异常一律原样抛，
                # 免得把"服务端明确拒绝"也变成重试，反而更像异常流量。
                if not _is_retryable_transport_error(e) or attempt >= retries:
                    raise
                wait = backoff * (attempt + 1)
                url = args[0] if args else kwargs.get("url", "?")
                error_kind = "SOCKS 握手瞬断" if any(
                    marker in str(e).lower() for marker in _SOCKS_HANDSHAKE_ERROR_MARKERS
                ) else "TLS 瞬断"
                logger.warning(
                    "%s，%.1fs 后原 session 重试 (%d/%d): %s",
                    error_kind, wait, attempt + 1, retries, str(url)[:80],
                )
                time.sleep(wait)

    def get(self, *args, **kwargs):
        return self._call_with_retry("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call_with_retry("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call_with_retry("put", *args, **kwargs)


# ── 当前线程的链路桥登记（供 IMAP 等原生 socket 通道复用同一条出口） ──

_chain_proxy_ctx = threading.local()


def register_current_chain_proxy(url: str) -> None:
    """登记当前线程活跃的链路桥代理地址（栈式，后建先用）。"""
    stack = getattr(_chain_proxy_ctx, "stack", None)
    if stack is None:
        stack = []
        _chain_proxy_ctx.stack = stack
    if url and url not in stack:
        stack.append(url)


def unregister_current_chain_proxy(url: str) -> None:
    stack = getattr(_chain_proxy_ctx, "stack", None)
    if stack and url in stack:
        stack.remove(url)


def get_current_chain_proxy() -> str:
    """当前线程最近创建的链路桥 socks5://…；没有返回空串。"""
    stack = getattr(_chain_proxy_ctx, "stack", None)
    return stack[-1] if stack else ""


def system_proxy_url() -> str:
    """Windows 系统代理（FlClash 开「系统代理」时通常是 http://127.0.0.1:7890）。

    页面「留空直连」要走的是本机当前出口，不是绕开 FlClash 的运营商 IP。
    curl_cffi 的 trust_env 对 libcurl 不够，必须显式写进 proxies。

    双击 start_webui.py 起的进程往往没有 HTTP_PROXY 环境变量，
    但 FlClash 已经写进了 Windows 系统代理（WinINet）。环境变量没有时
    再读注册表，否则会误判成「无系统代理」走运营商 IP。
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if int(enabled or 0) == 1:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    server = str(server or "").strip()
                    if server:
                        if "://" not in server:
                            server = "http://" + server
                        return server
        except OSError:
            pass
    return ""


def create_http_session(
    proxy: Optional[str] = None,
    preproxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。
    """
    bridge = None
    if preproxy:
        if not proxy:
            raise ValueError("设置前置 SOCKS 时必须同时设置外部 SOCKS")
        from socks_chain import SocksChainBridge

        bridge = SocksChainBridge(proxy, preproxy)

    try:
        if _HAS_CFFI:
            session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
            session.trust_env = False
            if proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
                normalized_proxy = bridge.client_proxy_url if bridge else proxy
                if normalized_proxy.startswith("socks5://"):
                    normalized_proxy = "socks5h://" + normalized_proxy[len("socks5://"):]
                    logger.info("代理协议已标准化: socks5:// -> socks5h://")
                session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
            else:
                # 留空 = 跟随系统代理（FlClash 系统代理），不是绕开它走运营商 IP。
                # trust_env=False 对 libcurl 不够，必须把地址写进 proxies。
                system_proxy = system_proxy_url()
                if system_proxy:
                    logger.info(f"未指定代理，跟随系统代理: {system_proxy}")
                    session.proxies = {"https": system_proxy, "http": system_proxy}
                else:
                    session.proxies = {"https": "", "http": ""}
                    logger.info("未指定代理，且无系统代理，走运营商直连")
        # 代理链路 5.4% 偶发 TLS 瞬断，原 session 重试实测 8/8 一次即恢复。
        # 包在这里才能同时覆盖 auth_flow 的 35 处调用和 sentinel（它直接拿 session 自己发请求）。
            wrapped = _TlsRetrySession(session)
        else:
            session = requests.Session()
            session.trust_env = False
            retry = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "POST"],
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            if proxy:
                normalized_proxy = bridge.client_proxy_url if bridge else proxy
                session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
            else:
                system_proxy = system_proxy_url()
                if system_proxy:
                    logger.info(f"未指定代理，跟随系统代理: {system_proxy}")
                    session.proxies = {"https": system_proxy, "http": system_proxy}
                else:
                    session.proxies = {"https": "", "http": ""}
                    logger.info("未指定代理，且无系统代理，走运营商直连")
            session.headers["User-Agent"] = user_agent or USER_AGENT
            wrapped = session

        if bridge is not None:
            # 让调用方在流程结束时可释放 bridge；仍透传所有标准 session 属性。
            wrapped._socks_chain_bridge = bridge
            # 登记到当前线程，IMAP 等原生 socket 通道可复用同一条出口。
            register_current_chain_proxy(bridge.client_proxy_url)
        return wrapped
    except Exception:
        if bridge is not None:
            bridge.close()
        raise


def close_http_session(session) -> None:
    """关闭 HTTP session 及可选的双 SOCKS 本地桥；重复调用安全。"""
    if session is None:
        return
    bridge = getattr(session, "_socks_chain_bridge", None)
    bridge_url = ""
    if bridge is not None:
        try:
            bridge_url = bridge.client_proxy_url
        except Exception:
            bridge_url = ""
    try:
        close = getattr(session, "close", None)
        if callable(close):
            close()
    finally:
        if bridge is not None:
            if bridge_url:
                unregister_current_chain_proxy(bridge_url)
            bridge.close()
