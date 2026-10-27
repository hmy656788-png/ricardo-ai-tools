"""通用“邮箱网页/API 接码”号池 provider。

每行只需要固定邮箱和它的网页/API 收件链接。普通网页和 JSON 收件箱复用
iCloud 中转的解析逻辑；直接返回验证码的签名 API 则在本模块单独适配。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from .base import MailProviderError, register, validate_email
from .icloud_relay import ICloudRelayProvider

logger = logging.getLogger(__name__)


# 输入常见形态：
#   smsbower.page/api/mail/getCodeBySignature?s=...
# 只自动补全明确的域名 URL，避免把任意文本误当成链接。
_SCHEMELESS_WEB_URL = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
    r"(?::\d{1,5})?(?:/[^\s]*)?$",
    re.I,
)
_OTP6 = re.compile(r"^\s*(\d{6})\s*$")


def normalize_webmail_url(value: str) -> str:
    """规范化网页/API 取码链接；裸域名链接默认使用 HTTPS。"""
    url = (value or "").strip()
    if url.lower().startswith(("https://", "http://")):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if _SCHEMELESS_WEB_URL.fullmatch(url):
        return f"https://{url}"
    raise ValueError(
        "第 2 段必须是网页/API 取码链接；可写完整 http(s):// 链接，"
        "或直接写域名开头的链接（会自动补 https://）"
    )


def _direct_code(value) -> str:
    """只接受独立的 6 位值，避免把响应中的订单号等数字当成验证码。"""
    if isinstance(value, bool) or value is None:
        return ""
    match = _OTP6.fullmatch(str(value))
    return match.group(1) if match else ""


def parse_direct_code_api(raw: str) -> Optional[list[dict]]:
    """解析 code/all_codes 型直接接码 API。

    返回 None 代表不是此类 API；返回空列表代表 API 正常，但此刻尚未收到码。
    这类接口不提供发件人/邮件时间，链接本身是私有签名凭据，故把 code
    作为当前验证码并优先于 all_codes。
    """
    raw = (raw or "").strip()
    if not raw or raw[0] not in "[{":
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not ({"code", "all_codes"} & set(data)):
        return None

    # ★ 信封式响应不是直接接码 API：顶层 code 是状态协议（0 / 200 / 错误码），
    #   不是 6 位验证码。实测 mail.no-replyca.xyz 返回
    #   {"code":0,"message":"success","data":{...邮件内容...}} —— 之前被顶层
    #   code 键劫持进 direct-code 模式，拿状态码 0 匹配不上 6 位码、返回空
    #   列表后把取件源锁死成 direct code API，永远「解析到 0 封」。
    #   顶层 code 不是 6 位码、也没有 all_codes 列表 → 交回父类通用邮件扫描
    #   （JSON 深扫：认 时间 + OpenAI/ChatGPT 品牌词 + 独占行 6 位码）。
    if not _direct_code(data.get("code")) and not isinstance(
        data.get("all_codes"), (list, tuple)
    ):
        return None

    codes: list[str] = []
    current = _direct_code(data.get("code"))
    if current:
        codes.append(current)
    history = data.get("all_codes")
    if isinstance(history, (list, tuple)):
        for value in history:
            candidate = _direct_code(value)
            if candidate and candidate not in codes:
                codes.append(candidate)

    return [
        {
            "sender": "webmail_api (direct code endpoint)",
            "subject": "(direct code API)",
            "body": "",
            "date_str": f"direct-code:{hashlib.sha1(code.encode()).hexdigest()[:16]}",
            "ts": None,
            "layout": "direct-code-api",
            "otp": code,
            "uid": f"direct-code:{hashlib.sha1(code.encode()).hexdigest()[:16]}",
        }
        for code in codes
    ]


@register
class WebMailApiProvider(ICloudRelayProvider):
    """任意可通过网页或同源 JSON API 读取验证码的固定邮箱。"""

    kind = "webmail_api"
    display_name = "邮箱网页/API 接码（通用）"
    pooled = True
    ephemeral = False
    line_segments = 2
    import_hint = "email----网页/API 取码链接（可省略 https://）"
    import_placeholder = (
        "name@example.com----mail.example.com/api/mail/getCodeBySignature?s=TOKEN"
    )
    config_fields = []

    # 此类地址可能已注册；允许走 passwordless OTP 登录继续拿凭证。
    accepts_existing_account = True

    def __init__(self, email: str, relay_url: str, timeout: int = 20):
        super().__init__(email, normalize_webmail_url(relay_url), timeout)
        self._direct_code_api = False

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """email----网页/API 取码链接；缺协议时自动补 https://。"""
        parts = [part.strip() for part in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（email----网页/API 取码链接），实际 {len(parts)} 段"
            )
        email, relay_url = parts
        validate_email(email)
        return {
            "email": email.lower(),
            "kind": cls.kind,
            "relay_url": normalize_webmail_url(relay_url),
        }

    def _fetch_direct_endpoint(self) -> str:
        """按原样请求直接接码 API，不能附加父类网页解析用的 all/n 参数。"""
        req = urllib.request.Request(
            self.relay_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404, 410):
                raise MailProviderError(
                    f"网页/API 取码链接无效（HTTP {exc.code}）—— 签名可能过期或链接填错",
                    fatal=True,
                    kind=self.kind,
                ) from exc
            raise

    def _load_direct_code_api(self) -> Optional[list[dict]]:
        return parse_direct_code_api(self._fetch_direct_endpoint())

    def _load(self) -> list[dict]:
        # 已确定是直接接码 API 后，每轮只请求一次原始 endpoint。
        if self._direct_code_api:
            return self._load_direct_code_api() or []

        direct = self._load_direct_code_api()
        if direct is not None:
            self._direct_code_api = True
            self._source = "direct code API"
            if direct:
                logger.info("[%s] 取件源=直接验证码 API", self.kind)
            return direct

        # 并非 code/all_codes 型 JSON，继续使用网页、同路径 JSON 和常规接口探测。
        return super()._load()

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """等待直接接码 API 的当前验证码。

        这类 API 通常只返回 code/all_codes，不提供邮件 ID 或到达时间。父类只能
        以 6 位码本身作为指纹去重。首次验证码提交失败后，OpenAI 的 resend
        可能再次投递相同的 6 位值；新一轮等待若继续保留旧指纹，会把这个可能
        已重新生效的码永久跳过，最后错误地报超时。

        因此只对直接验证码 API，在每次新的 wait_for_otp 调用前清掉它自己的
        代码指纹。网页/邮件型来源仍保留原有严格的邮件 ID 去重逻辑。
        """
        if self._direct_code_api:
            before = len(self._seen)
            self._seen = {
                fp for fp in self._seen
                if not fp.startswith("uid:direct-code:")
            }
            removed = before - len(self._seen)
            if removed:
                logger.info(
                    "[%s] 新发码等待轮次：直接验证码 API 无邮件 ID，"
                    "允许相同 6 位码重新验证一次",
                    self.kind,
                )
        return super().wait_for_otp(
            email_addr,
            timeout=timeout,
            issued_after=issued_after,
        )

    def _looks_like_openai(self, message: dict) -> bool:
        # 此类 API 只返回纯验证码，不含发件人/主题。导入的私有签名链接本身
        # 即收件权限；其余网页/API 仍沿用父类的 OpenAI 发件人检查。
        if message.get("layout") == "direct-code-api":
            return True
        return super()._looks_like_openai(message)
