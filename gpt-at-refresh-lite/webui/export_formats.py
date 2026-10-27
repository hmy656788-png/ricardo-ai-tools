"""批量导出格式注册表。

**以后要加导出格式，只改这一个文件**：往 `FORMATS` 里加一条就行。
后端路由、前端下拉框都是照着这张表自动长出来的，一行都不用动。

两种 mode：
  - `text`     一行一条记录，前端弹窗预览 + 复制 + 下载（`render` 逐行）
  - `download` 整份文档，前端拿到直接下载、不弹预览（`render_all` 返回 bytes）

约定（主人定的）：
- **不跳行**。勾了几个号就出几行，字段为空就留空，
  分隔符照样保留（`邮箱----`），方便主人自己在文本里对齐、补齐。
- 行序 = 「注册结果」表格里的顺序（created_at 倒序），好核对。

注：CPA / SUB2API 的**手动导出已移除**（2026-08-06）。这两个面板都是
「一次只吃一个号」的接口形态，而且必须先用 refresh_token 把 access_token
换成 Codex 风格才认，手动导出满足不了，实测导出来用不了。
要往这两个面板送号请用**自动推送**（注册完成后由 `exporter.run_exports` 直接 POST），
在「导出配置」里开就行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportFormat:
    id: str                                       # 前端 command 用的唯一标识
    label: str                                    # 下拉菜单里显示的名字
    filename: str                                 # 下载的文件名
    mode: str = "text"                            # "text" | "download"
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None          # mode=text：一行记录 -> 一行文本
    render_all: Optional[Callable[[list], bytes]] = None    # mode=download：整批 -> 文件字节
    note: str = ""                                # 下拉菜单里的灰色小字说明


def _s(row: dict, key: str) -> str:
    """取字段并转成干净字符串（None / 非 str 都兜住）。"""
    v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


# ──────────────────────── CPA JSON（带账密）导出 ────────────────────────
# 函数必须先于 FORMATS 定义（ExportFormat 的 render_all 直接引用）。


def _cpa_json_for(row: dict) -> dict:
    """组装新版单行 CPA JSON（对齐 codex_accounts_cpa_* 样例），source_line 收尾。

    样例（D:\\downloads\\临时下载目录\\codex_accounts_cpa_08_*.json）的字段序：
      access_token, account_id, disabled, email, expired, id_token,
      last_refresh, refresh_token, type —— 紧凑单行；
    last_refresh / expired 保持 build_cpa_token_json 的 '+08:00' 原样输出
    （样例正是这个格式，老版的 UTC Z 转换已废弃）；
    source_line = 邮箱----密码----2FA 照例插在最后一个键，方便对账。
    """
    from .exporter import build_cpa_token_json

    email = _s(row, "email")
    cred = {
        "email": email,
        "access_token": _s(row, "access_token"),
        "refresh_token": _s(row, "refresh_token"),
        "id_token": _s(row, "id_token"),
    }
    base = build_cpa_token_json(cred)

    return {
        "access_token": base.get("access_token", ""),
        "account_id": base.get("account_id", ""),
        "disabled": False,
        "email": email,
        "expired": base.get("expired", ""),
        "id_token": base.get("id_token", ""),
        "last_refresh": base.get("last_refresh", ""),
        "refresh_token": base.get("refresh_token", ""),
        "type": "codex",
        "source_line": (
            f'{_s(row, "email")}----{_s(row, "password")}----{_s(row, "totp_secret")}'
        ),
    }


def _render_cpa_json_zip(rows: list) -> bytes:
    """每号一个 <email>.cpa.json 打进 zip，内容为新版紧凑单行 JSON。

    文件名沿用老的 <email>.cpa.json（主人定的：新版长文件名太麻烦）。
    单个号失败不拖垮整批（跳过）。
    """
    import io
    import json as _json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows or []:
            try:
                data = _cpa_json_for(r)
                email = (data.get("email") or "unknown").strip().lower()
                zf.writestr(
                    f"{email}.cpa.json",
                    _json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                )
            except Exception:
                continue
    return buf.getvalue()


# ──────────────────────── 注册表 ────────────────────────


FORMATS: list[ExportFormat] = [
    ExportFormat(
        id="at",
        label="access_token",
        filename="AT.txt",
        render=lambda r: _s(r, "access_token"),
    ),
    ExportFormat(
        id="email_pw_2fa_at",
        label="邮箱----密码----2FA----access token",
        filename="账号密码2FAaccess_token.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----'
            f'{_s(r, "totp_secret")}----{_s(r, "access_token")}'
        ),
        note="包含密码、2FA secret 和 access token，请妥善保管",
    ),
    # 2FA secret 只在绑定那一刻下发一次、服务端取不回，丢了这个号就永久锁死，
    # 所以必须有能把它带出去的导出格式。没绑 2FA 的号照约定留空、分隔符保留。
    ExportFormat(
        id="email_pw_2fa",
        label="邮箱----密码----2FA",
        filename="账号密码2FA.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----{_s(r, "totp_secret")}'
        ),
        note="secret 仅下发一次，取不回，务必留存",
    ),
    # 比上面那条多一段中转取件链接。
    # ⚠️ relay_url 不在 registered 表里，是 db.list_registered_full /
    #    list_registered_by_emails 从号池表（outlook_accounts）LEFT JOIN 带出来的。
    #    所以：① 只有 icloud_relay 这类「一号一条取件链接」的号有值；
    #          ② 号池那行被删掉了就是空 —— 照约定留空、分隔符保留，不跳行。
    #    链接里嵌着 token，等于这个邮箱的收件权限，导出来的文件请当密码保管。
    ExportFormat(
        id="email_pw_2fa_relay",
        label="邮箱----密码----2FA----取件url",
        filename="账号密码2FA取件url.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----'
            f'{_s(r, "totp_secret")}----{_s(r, "relay_url")}'
        ),
        note="取件链接含 token，等同收件权限，妥善保管",
    ),
    # CPA JSON（带 RT、带账密）：每号一个 <email>.cpa.json 打进 zip 下载。
    # 内容为紧凑单行 JSON，字段序严格对齐主人 2026-08-30 给的新样例
    # （codex_accounts_cpa_08_*.json：access_token/account_id/disabled/email/expired/
    #  id_token/last_refresh/refresh_token/type，+08:00 时间），source_line = 邮箱----密码----2FA
    # 照例收尾。文件名按主人要求沿用老的 <email>.cpa.json，不跟新样例的长命名。
    ExportFormat(
        id="cpa_json",
        label="cpa json----邮箱----密码----2FA",
        filename="cpa-json带账密2fa.zip",
        mode="download",
        mime="application/zip",
        render_all=_render_cpa_json_zip,
        note="完整的cpa json带rt、带账密导出（每号一个json，zip打包）",
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    """给前端的精简清单（不含 render 函数）。"""
    return [
        {
            "id": f.id,
            "label": f.label,
            "filename": f.filename,
            "mode": f.mode,
            "mime": f.mime,
            "note": f.note,
        }
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[ExportFormat]:
    return _BY_ID.get((fmt_id or "").strip())


def render_text(rows: list, fmt: "ExportFormat | str") -> str:
    """mode=text：一行一条记录。

    单条渲染炸了不整体失败 —— 那一行留空，其余照常导出。
    """
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render:
        raise RuntimeError(f"格式 {f.id} 不是文本格式")

    lines = []
    for r in rows or []:
        try:
            lines.append(f.render(r))
        except Exception:
            lines.append("")
    return "\n".join(lines)


def render_bytes(rows: list, fmt: "ExportFormat | str") -> bytes:
    """mode=download：整份文件字节。"""
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render_all:
        raise RuntimeError(f"格式 {f.id} 不是下载格式")
    return f.render_all(rows or [])


# 兼容旧调用名
def render(rows: list, fmt: "ExportFormat | str") -> str:
    return render_text(rows, fmt)
