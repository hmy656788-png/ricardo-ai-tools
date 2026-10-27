"""「导入账号」专用补 refresh 板块（2026-09-14 独立开板，零 mail 依赖）。

为什么单独开一个模块，而不是往 registrar._do_backfill_login 里继续打补丁：
  · 导入账号（imported 表：email----密码----2FA）是**独立设计**的数据 ——
    它不在邮箱号池、没有取件链接，也不该有；补 refresh 的登录链是
    密码 → TOTP 2FA → Codex OAuth 授权，全程**不需要邮箱接码**。
  · 号池版 _do_backfill_login 为注册号设计，急切创建 mail provider、
    换号重驱动登录也硬性要求 mail_provider —— 这些对导入号全是障碍
    （实测：起跑就抛「是号池型」；换号时「无 mail_provider 无法重驱动」）。
  · 往共享代码里塞 if imported 分支会越改越纠缠，还容易炸回注册路径。
    所以这里开一个**平行板块**：登录链复用 AuthFlow 的协议方法（只读），
    但用子类把「重驱动登录需要 mail」的闸门拆掉；worker 全程不碰 mail。

原版代码（auth_flow.py / registrar.py / 号池版 backfill 端点）一行未动。
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # gpt-auto-register-0.5.6/
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow, SmsBindingError, is_ban_signal  # noqa: E402

from . import db  # noqa: E402
# 复用号池板块的 run 管道（SSE 队列 / 日志 handler / 状态广播 / SMS 租号回调 /
# 错误分类）—— 都是纯管道，不含任何 mail 逻辑，共用不产生行为耦合。
from .registrar import (  # noqa: E402
    LOG_DIR,
    QueueLogHandler,
    _build_sms_callback,
    _current_run,
    _emit_status,
    _lock,
    _run_queues,
    classify_error,
)

logger = logging.getLogger("imported_backfill")


class ImportedAuthFlow(AuthFlow):
    """导入号专用 AuthFlow：只拆一个闸门 —— 换号重驱动登录不再要求 mail_provider。

    原版 _add_phone_reenter（auth_flow.py:1184-1196）在 authorize 回落 /log-in 时
    硬性检查 mail_provider 非 None 才肯重驱动登录。但 _codex_drive_login_from_log_in
    的密码+TOTP 分支**根本不碰 mail_provider**（只有 email_otp_verification 分支用，
    且 None 时已有优雅告警返回）。导入号没有接码渠道 → 原版闸门把明明能走的
    密码+2FA 重驱动也一起堵死（实测日志反复刷「authorize 回落 /log-in 但无
    mail_provider，无法重驱动登录」→ 换号 send 一直 Invalid authorization step）。

    这里重写该方法：其余逻辑与原版逐行一致，仅把「有 mail 才重驱动」改成
    「无条件重驱动（mail 传 None）」。真被 OpenAI 弹到邮箱验证码时，
    _codex_drive_login_from_log_in 自己会告警返回，不会卡死也不会乱拿码。
    """

    def _add_phone_reenter(self, continue_url: str = "") -> str:
        auth_url = (getattr(self, "_oauth_auth_url", "") or "").strip()
        redirect_uri = (getattr(self, "_oauth_redirect_uri", "") or "").strip()
        if auth_url and redirect_uri:
            try:
                callback_url, final_url = self._follow_authorize_for_callback(
                    auth_url, redirect_uri, "add_phone_reentry"
                )
                if callback_url:
                    logger.info("[sms] authorize 重进直接拿到授权码（本轮无需绑手机）")
                    return "completed"
                fu = final_url or ""
                if self._is_add_phone_state(page_type="", continue_url=fu):
                    logger.info("[sms] authorize 重进直达 add-phone 入口")
                    return "add_phone"
                if "/log-in" in fu:
                    # ★ 与原版唯一差异：不再要求 mail_provider —— 密码+2FA 直接重驱动
                    logger.info("[sms] authorize 重进回落 /log-in，重驱动登录获取全新 add-phone 事务（无邮箱接码模式）...")
                    cu = self._codex_drive_login_from_log_in(
                        mail_provider=None, stop_at_add_phone=True
                    )
                    if self._is_add_phone_state(page_type="", continue_url=cu or ""):
                        logger.info("[sms] ✅ 重驱动登录成功，已拿到全新 add-phone 状态")
                        return "add_phone"
                    logger.info("[sms] 重驱动登录后未落在 add-phone: %s", (cu or "")[:140])
                else:
                    logger.info("[sms] authorize 重进后落在: %s", fu[:160])
            except Exception as e:
                logger.info("[sms] authorize 重进异常: %s", e)

        # 兜底：直接 GET add-phone 页（与原版一致）
        candidates = []
        if (continue_url or "").strip():
            candidates.append(continue_url.strip())
        candidates.append("https://auth.openai.com/add-phone")
        for url in candidates:
            try:
                resp = self.session.get(url, timeout=30, allow_redirects=True)
                final_url = self._normalize_continue_url(getattr(resp, "url", "") or url)
                if self._is_add_phone_state(page_type="", continue_url=final_url):
                    logger.info("[sms] GET 回到 add-phone 页（状态是否全新由 send 结果验证）")
                    return "add_phone"
                logger.info("[sms] GET %s 落在 %s（非 add-phone）", url[:100], final_url[:100])
            except Exception as e:
                logger.info("[sms] GET %s 异常: %s", url[:100], e)
        return "failed"


def _write_back(email: str, flow, cred_source: str) -> dict:
    """merge 写回 imported 表：只覆盖本轮拿到的字段，其余保留旧值。

    与号池版同规则（plus_check 不冲掉），但**不管 oauth 返回 True/False**，
    只要 flow.result 里有凭证就落库 —— 修复「✅ Codex OAuth 交换成功
    access=有 refresh=有 但页面不显示」：交换函数拿到了 token 后若后续
    小步失败返回 False，号池版 while 循环 `if ok and rt` 判定不成立 →
    直接抛错，**到不了写回**，token 白白丢弃。
    """
    new = flow.result
    old = db.get_registered(email, source=cred_source) or {}
    old_extra = old.get("extra") or {}
    merged = {
        "email": email,
        "password": old.get("password") or new.password or "",
        "access_token": new.access_token or old.get("access_token", ""),
        "session_token": new.session_token or old.get("session_token", ""),
        "refresh_token": new.refresh_token or old.get("refresh_token", ""),
        "id_token": new.id_token or old.get("id_token", ""),
        "device_id": new.device_id or old.get("device_id", ""),
        "csrf_token": new.csrf_token or old.get("csrf_token", ""),
        "cookie_header": new.cookie_header or old.get("cookie_header", ""),
        "totp_secret": old.get("totp_secret") or new.totp_secret or "",
        "totp_factor_id": old.get("totp_factor_id", ""),
    }
    if old_extra.get("plus_check") is not None:
        merged["plus_check"] = old_extra["plus_check"]
    merged["exit_ip"] = (getattr(flow, "_exit_ip", "") or "").strip()
    merged["country"] = (getattr(flow, "_country_code", "") or "").strip()
    _fp = getattr(flow, "_fingerprint", None) or {}
    merged["fingerprint"] = " / ".join(
        str(x) for x in [
            _fp.get("browser_type", ""),
            _fp.get("screen", ""),
            _fp.get("lang", ""),
            _fp.get("timezone", ""),
            _fp.get("hardware_concurrency", ""),
        ] if x
    ) or ""
    db.save_registered(merged, source=cred_source)
    return merged


def _do_imported_backfill(run_id: str, email: str, options: dict, log_file: Path) -> None:
    """导入账号补 RT worker：密码+2FA 登录 → Codex OAuth → 拿 RT 写回 imported 表。

    与号池版 _do_backfill_login 的差异（全部是"去掉 mail"和"拿到就存"）：
      · 不创建任何 mail provider（oauth_codex_rt_exchange(mail_provider=None)）；
      · 换号重驱动登录用 ImportedAuthFlow（无 mail 闸门）；
      · 只要 flow.result 拿到 refresh_token 就写回（哪怕 oauth 返回 False）；
      · 环境类失败自动重试 1 次；SmsBindingError 不重试（登录已过，重试白烧钱）。
    """
    _current_run.run_id = run_id
    email = (email or "").strip().lower()
    cred_source = options.get("cred_source") or "imported"

    gate = options.get("gate")
    gate_acquired = False
    if gate is not None:
        try:
            _emit_status(run_id, "phase", {"phase": "queued", "message": "排队等待并发额度..."})
            logger.info(f"[imported-backfill] 排队等待并发额度: {email}")
            gate.acquire()
            gate_acquired = True
            _emit_status(run_id, "phase", {"phase": "starting", "message": "拿到并发额度，开始执行"})
        except Exception:
            gate_acquired = False

    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    flow = None
    try:
        cred = db.get_registered(email, source=cred_source) or {}
        if not (cred.get("password") or "").strip():
            raise RuntimeError(
                f"[{email}] imported 表没有该号的密码 —— 导入账号补 refresh 需要"
                " email----密码----2FA 完整导入"
            )

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None
        cfg.preproxy = (options.get("preproxy") or "").strip() or None

        env_overrides = {
            "WEBUI_ALLOW_LOGIN": "1",
            "OTP_TIMEOUT": str(int(options.get("otp_timeout") or 180)),
            "OAUTH_CODEX_RT_EXCHANGE": "1",
            "OAUTH_CODEX_RT_ALLOW_RETRY": "1",
            "OAUTH_PHONE_REQUIRED": "1",
        }

        def _account_callback(e: str) -> dict:
            try:
                d = db.get_registered(e, source=cred_source)
                if d:
                    return {
                        "password": d.get("password", ""),
                        "totp_secret": d.get("totp_secret", ""),
                    }
            except Exception:
                pass
            return {}

        def _make_flow() -> ImportedAuthFlow:
            # 重试必须重建 flow：旧会话 cookie/授权 state 已污染
            f = ImportedAuthFlow(
                cfg,
                sms_callback=_build_sms_callback(run_id, parallel=bool(options.get("sms_parallel"))),
                env_overrides=env_overrides,
                account_callback=_account_callback,
            )
            f.result.email = email
            return f

        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logger.info(f"[imported-backfill] 导入账号纯登录补 RT 开始（无邮箱接码模式）: {email}")

        flow = _make_flow()
        attempt = 0
        ever_sms_bound = False
        while True:
            try:
                ok = flow.oauth_codex_rt_exchange(mail_provider=None)
            except SmsBindingError as sbe:
                sbe_text = sbe.args[0] if sbe.args else str(sbe)
                hint = ""
                if "suspicious behavior" in (sbe.last_err or "").lower():
                    hint = "🔴【号段风控】当前国家号段已被 OpenAI 拉黑，请在 SMS 设置里【切换国家/号段】后重试！"
                raise RuntimeError(
                    f"[{email}] 接码失败（登录/2FA 已通过）：{sbe_text}。{hint}"
                    "重新勾选补 refresh 再发起即可换号重试（重新登录后直接进入绑号环节）"
                ) from sbe
            ever_sms_bound = ever_sms_bound or bool(getattr(flow, "sms_bound", False))

            new = flow.result
            # ★ 核心修复：拿到 refresh_token 就算成功 —— 不等 oauth 返回值。
            #   （交换成功后若 consent/workspace 等小步失败，原版会 return False，
            #    号池版判定 `ok and rt` 不成立 → 抛错 → token 丢弃不写回。）
            if (new.refresh_token or "").strip():
                break

            err_msg = flow.last_login_error or ""
            transient = (
                "curl (28)" in err_msg or "curl(28)" in err_msg
                or "curl (35)" in err_msg or "curl(35)" in err_msg
                or "timed out" in err_msg.lower()
                or "timeout" in err_msg.lower()
                or "sentinel" in err_msg.lower()
                or "connection" in err_msg.lower()
            )
            if attempt < 1 and transient and not is_ban_signal(err_msg):
                attempt += 1
                bound_note = "（接码/绑号已生效，重登不会再扣接码费）" if ever_sms_bound else ""
                logger.warning(
                    f"[imported-backfill] [{email}] 疑似环境失败{bound_note}（{err_msg[:120]}），"
                    f"3 秒后自动重试（第 {attempt}/1 次）"
                )
                _emit_status(run_id, "phase", {
                    "phase": "retry",
                    "message": f"[{email}] 疑似网络/代理超时，自动重试（{attempt}/1）",
                })
                time.sleep(3.0)
                try:
                    flow.close()
                except Exception:
                    pass
                flow = _make_flow()
                continue

            # 失败收尾：哪怕没 RT，拿到的 AT/ST 也是有效凭证 —— 落库不浪费
            if (new.access_token or new.session_token or "").strip():
                try:
                    _write_back(email, flow, cred_source)
                    logger.info(f"[imported-backfill] [{email}] 未拿到 RT，但已写回本轮 AT/ST（不浪费）")
                except Exception as wb_err:
                    logger.warning(f"[imported-backfill] [{email}] AT/ST 写回失败: {wb_err}")
                raise RuntimeError(
                    f"[{email}] 登录成功但未拿到 refresh_token"
                    + (f"（{err_msg[:150]}）" if err_msg else "")
                    + "。已写回拿到的 AT/ST；补 RT 可稍后重新发起（重登不会再走 add-phone、不重复扣接码费）"
                )

            # 封号细分（与号池版同判据）
            if is_ban_signal(err_msg):
                try:
                    db.update_plus_check(email, {
                        "status": "banned", "label": "已封号",
                        "checked_at": time.time(),
                        "note": "导入账号补refresh时 OpenAI 返回账号已删除/停用",
                    }, source=cred_source)
                except Exception as tag_err:
                    logger.warning(f"[imported-backfill] [{email}] 封号标签写入失败: {tag_err}")
                raise RuntimeError(
                    f"[{email}] 🚫 已封号（OpenAI 返回：账号已删除或停用，不可恢复）—— 已打「封号」标签。"
                )
            raise RuntimeError(
                f"[{email}] 登录失败，未拿到凭证"
                + (f"（{err_msg[:180]}）" if err_msg else "")
                + "。导入账号补 refresh 走 密码→2FA 链，常见原因："
                "① 代理不通（看 curl 日志）；② 密码/2FA 错；"
                "③ OpenAI 要求邮箱验证码（导入号无接码渠道，需把该邮箱导入号池后走号池版补 refresh）。"
            )

        # ─ 成功：merge 写回 imported 表 ─
        merged = _write_back(email, flow, cred_source)
        result_summary = {
            "email": email,
            "password": merged.get("password") or "",
            "access_token_len": len(merged.get("access_token") or ""),
            "session_token_len": len(merged.get("session_token") or ""),
            "refresh_token_len": len(merged.get("refresh_token") or ""),
            "totp_secret": merged.get("totp_secret") or "",
            "partial": False,
        }
        _emit_status(run_id, "done", result_summary)
        logger.info(
            f"[imported-backfill] 完成 email={email} rt={result_summary['refresh_token_len']} "
            f"at={result_summary['access_token_len']} st={result_summary['session_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, "")
        logger.error(f"[imported-backfill] 失败 (category={category}): {err}")
        if category not in ("account", "banned"):
            logger.error(traceback.format_exc())
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        if gate is not None and gate_acquired:
            try:
                gate.release()
            except Exception:
                pass
        try:
            if flow is not None:
                flow.close()
        except Exception:
            pass
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)
        _current_run.run_id = None


def start_imported_backfill(email: str, options: dict) -> str:
    """启动一次导入账号补 RT 任务，返回 run_id（SSE/状态端点与号池版共用）。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, email, str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_imported_backfill,
        args=(run_id, email, options, log_file),
        daemon=True,
        name=f"imported-backfill-{run_id}",
    )
    th.start()
    return run_id
