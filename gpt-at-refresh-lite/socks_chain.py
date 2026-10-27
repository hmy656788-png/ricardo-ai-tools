"""将一个 SOCKS5 出口安全地串到另一个 SOCKS5 前置代理。

Windows TUN + Fake-IP 下，部分程序直接连外部 SOCKS 的 Fake-IP 会偶发卡在
SOCKS greeting。这个桥仅处理第一跳：应用连接本机监听端口，桥通过前置
SOCKS（例如 FlClash 的 127.0.0.1:7890）连到外部 SOCKS，之后的所有字节
原样转发。因此外部 SOCKS 仍然是最终出口，也不会改写 TLS 指纹或 HTTP 内容。
"""
from __future__ import annotations

import logging
import select
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger(__name__)


def _redact_proxy(value: str) -> str:
    """日志只保留协议和主机，绝不写出用户名/密码。"""
    try:
        p = urlsplit(value)
        if p.scheme and p.hostname:
            return f"{p.scheme}://***@{p.hostname}:{p.port or '?'}"
    except ValueError:
        pass
    return "<invalid proxy>"


@dataclass(frozen=True)
class _SocksAddress:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def _parse_socks5_proxy(value: str, *, label: str) -> _SocksAddress:
    try:
        parsed = urlsplit((value or "").strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label}格式无效") from exc
    if scheme not in {"socks5", "socks5h"} or not host or not port:
        raise ValueError(f"{label}必须是 socks5:// 或 socks5h:// 地址")
    return _SocksAddress(
        scheme=scheme,
        host=host,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("SOCKS peer closed connection")
        data.extend(chunk)
    return bytes(data)


def _consume_socks_reply(sock: socket.socket) -> int:
    head = _recv_exact(sock, 4)
    if head[0] != 5:
        raise ConnectionError("前置 SOCKS 返回了非法版本")
    atyp = head[3]
    if atyp == 1:
        _recv_exact(sock, 6)  # IPv4 + port
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
    elif atyp == 4:
        _recv_exact(sock, 18)  # IPv6 + port
    else:
        raise ConnectionError("前置 SOCKS 返回了非法地址类型")
    return head[1]


def _open_via_preproxy(preproxy: _SocksAddress, target: _SocksAddress) -> socket.socket:
    """经前置 SOCKS 建到外部 SOCKS 的字节流；失败时关闭半开的 socket。"""
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection((preproxy.host, preproxy.port), timeout=10)
        sock.settimeout(15)

        methods = b"\x00\x02" if preproxy.username else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        hello = _recv_exact(sock, 2)
        if hello[0] != 5 or hello[1] == 0xFF:
            raise ConnectionError("前置 SOCKS 不接受可用认证方式")
        if hello[1] == 2:
            if not preproxy.username:
                raise ConnectionError("前置 SOCKS 要求用户名密码认证")
            user = preproxy.username.encode("utf-8")
            password = preproxy.password.encode("utf-8")
            if not (1 <= len(user) <= 255 and len(password) <= 255):
                raise ValueError("前置 SOCKS 用户名或密码长度无效")
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            auth = _recv_exact(sock, 2)
            if auth != b"\x01\x00":
                raise ConnectionError("前置 SOCKS 用户名或密码被拒绝")
        elif hello[1] != 0:
            raise ConnectionError("前置 SOCKS 选择了不支持的认证方式")

        # 以域名形式交给前置 SOCKS 解析，避免本机 TUN Fake-IP 介入外部 SOCKS 域名。
        host = target.host.encode("idna")
        if len(host) > 255:
            raise ValueError("外部 SOCKS 主机名过长")
        request = b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", target.port)
        sock.sendall(request)
        status = _consume_socks_reply(sock)
        if status != 0:
            raise ConnectionError(f"前置 SOCKS 连接外部 SOCKS 失败（状态 {status}）")
        sock.settimeout(None)
        return sock
    except Exception:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        raise


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "SocksChainBridge" = self.server  # type: ignore[assignment]
        upstream: Optional[socket.socket] = None
        try:
            upstream = _open_via_preproxy(server.preproxy, server.outer)
            self.request.setblocking(False)
            upstream.setblocking(False)
            peers = (self.request, upstream)
            while not server.is_closed:
                readable, _, exceptional = select.select(peers, [], peers, 300)
                if exceptional or not readable:
                    return
                for source in readable:
                    try:
                        payload = source.recv(65536)
                    except BlockingIOError:
                        continue
                    if not payload:
                        return
                    (upstream if source is self.request else self.request).sendall(payload)
        except Exception as exc:
            # 客户端会收到本地连接关闭，并由 curl 报出可重试的 SOCKS 握手错误。
            logger.warning("SOCKS 链路桥连接失败（%s）: %s", _redact_proxy(server.outer_url), exc)
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class SocksChainBridge(socketserver.ThreadingTCPServer):
    """每个 HTTP session 独占一个本地监听端口的 SOCKS 字节流桥。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, outer_proxy: str, preproxy: str):
        self.outer_url = outer_proxy
        self.outer = _parse_socks5_proxy(outer_proxy, label="外部 SOCKS")
        self.preproxy = _parse_socks5_proxy(preproxy, label="前置 SOCKS")
        self._closed = False
        super().__init__(("127.0.0.1", 0), _BridgeHandler, bind_and_activate=True)
        self._thread = threading.Thread(
            target=self.serve_forever,
            name="socks-chain-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "SOCKS 链路桥已启用: 本地 127.0.0.1:%d -> 前置 %s -> 外部 %s",
            self.server_address[1],
            _redact_proxy(preproxy),
            _redact_proxy(outer_proxy),
        )

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def client_proxy_url(self) -> str:
        userinfo = ""
        if self.outer.username:
            userinfo = quote(self.outer.username, safe="")
            if self.outer.password:
                userinfo += ":" + quote(self.outer.password, safe="")
            userinfo += "@"
        return f"{self.outer.scheme}://{userinfo}127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.shutdown()
        self.server_close()

