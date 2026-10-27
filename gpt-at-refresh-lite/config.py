"""最小化 Config（仅 browser_register.py 用到的字段）。

剥离自原 CTF-reg/config.py，去掉 card / billing / stripe / captcha 等支付相关字段，
仅保留注册阶段必需的 proxy 字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """ChatGPT 注册最小配置。"""
    # 出口代理 URL，例：socks5://user:pass@host:port  或  socks5://127.0.0.1:18899
    # 留 None 走系统直连
    proxy: Optional[str] = None
    # 可选前置 SOCKS。用于 TUN + 外部 SOCKS 的双层链路：应用先连本机 FlClash
    # SOCKS，再由它建立到外部 SOCKS 的隧道；留空保持原有的直连外部 SOCKS 行为。
    preproxy: Optional[str] = None
