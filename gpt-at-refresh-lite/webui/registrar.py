"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
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

ROOT = Path(__file__).resolve().parents[1]  # gpt-outlook-register/
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow, SmsBindingError, is_ban_signal  # noqa: E402
from mail_providers import (  # noqa: E402
    MailProviderError,
    create_mail_provider,
    get_provider_class,
)
from sms_provider import PhoneCallbackController  # noqa: E402

from . import db  # noqa: E402

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
_lock = threading.Lock()

# 当前线程正在跑哪个 run。
# ⚠️ 为什么需要这个：QueueLogHandler 是挂在 **root logger** 上的，而 root logger
#    是进程全局的。auto_loop 并发时 N 个 run 各挂一个 handler，每条日志会被
#    广播进**所有** run 的文件和 SSE 流 —— 实测 2026-08-04 三 worker 并发，
#    一个号的记录同时出现在 3 个 .log 里，WebUI 上三个号的日志搅在一起，
#    而 "[4/10] 获取 Sentinel Token..." 这类行不带邮箱，根本分不清是谁的。
#
#    注册链路（auth_flow / mail_providers / sentinel）内部不开任何线程，
#    一个 run 的日志全在自己那条线程上产生，所以线程绑定就能干净切开。
_current_run = threading.local()

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class QueueLogHandler(logging.Handler):
    """把 logging 记录扔进 run queue + 写 log 文件。

    只收**本 run 线程**产生的日志，见 emit 里的过滤。
    """

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            # emit 是在**打日志的那条线程**里同步跑的，所以这里读到的就是
            # 日志产生者的 run_id。别人 run 的日志直接丢掉。
            rid = getattr(_current_run, "run_id", None)
            if rid is not None and rid != self.run_id:
                return
            # rid is None = 不属于任何 run（webui 请求线程、启动期日志等）。
            # 这类照旧广播给所有 handler —— 宁可多收也不能丢，日志文件
            # 开头那句 "webui: [run] xxx -> email@..." 就是这么来的。
            msg = self.format(record)
            self._fh.write(msg + "\n")
            self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                q.put(msg)
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """分类错误：banned / network / account / rate_limit / verification / unknown。

    banned：OpenAI 明文说账号已删除或停用。必须先于 network，
    否则「TOTP 验证失败: 403」会先命中 403 forbidden 被当成网络错误。
    对应导入账号下拉「已封号」。

    mail_source 用来问 provider 要不要豁免某些模式 —— 比如 iCloud 中转号
    本来就是买的老号，"已有账号"是正常流程不是失败（见
    MailProvider.accepts_existing_account）。留空则按最严格的规则判。
    """
    s = (err or "").lower()
    if is_ban_signal(err):
        return "banned"

    account_patterns = [
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "service abuse", "aadsts70000", "被封禁",
        "已有账号", "账号被", "refresh_token 失效",
    ]
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # 未知来源 —— 按默认最严格规则走
        # ⚠️ 用 if-in 而不是裸 remove()：上面的模式表将来被人改动/重排后，
        #    remove 抛的 ValueError 会跟 get_provider_class 的错混在同一个
        #    except 里被一起吞掉，豁免静默失效且没人看得出来。
        if exempt and "已有账号" in account_patterns:
            account_patterns.remove("已有账号")

    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in account_patterns):
        return "account"
    if any(p in s for p in ("429", "rate limit", "rate_limit", "too many requests", "请求过于频繁")):
        return "rate_limit"
    if any(p in s for p in ("captcha", "验证码", "手机验证", "phone verification", "2fa required", "otp required")):
        return "verification"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
        sms_parallel: bool   # 多并发批量时启用 SMS 并行接码（与补 refresh 链路对齐）
    """
    # 先认领本线程，再挂 handler —— 顺序不能反：中间要是有日志产生，
    # 没打标记的话会被广播到其他并发 run 的日志里去。
    _current_run.run_id = run_id

    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # 第一次需要的话提到 INFO 级别
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    # 提前读取，避免在 try 块前异常时 except 引用未定义
    mail_source = db.get_setting("mail_source", "outlook")
    # 要不要操作号池（mark_done / mark_failed / release）由 provider 声明的
    # pooled 决定。未知 kind 时保守当池化处理 —— 号池里真有这行的话
    # 至少不会漏掉状态回写，把号永远卡在 in_use。
    flow = None
    try:
        is_pooled = get_provider_class(mail_source).pooled
    except MailProviderError:
        is_pooled = True

    try:
        # 本次注册专属的配置覆盖。
        # ⚠️ 以前是写 os.environ + finally 还原，但 auto_loop 并发跑多个 worker，
        #    os.environ 是**进程全局**的：A 设的 OTP_TIMEOUT/WEBUI_ALLOW_LOGIN 会被
        #    B 读到，B 跑完还原成 A 之前的值，A 后半程就用上别人的配置了。
        #    现在整个 dict 直接传给 AuthFlow，只挂在实例上，谁都污染不到谁。
        env_overrides = {}
        # outlook 接码邮箱常被 OpenAI 走 passwordless_signup 流程（新号收码而非设密码），
        # auth_flow 会误判为"已有账号"分支 → 不设 WEBUI_ALLOW_LOGIN 会 fast-fail。
        # 单号 WebUI 场景下 fast-fail 没意义（批量跑才需要"跳过被识别的号"），故强制 ON。
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
        env_overrides["OTP_TIMEOUT"] = str(int(options.get("otp_timeout") or 180))
        # 用户不要 refresh_token → 直接跳过 Codex OAuth（每次都失败浪费 ~10s + 一堆告警）
        if not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # PROXY 走 cfg.proxy，无需 env

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None
        cfg.preproxy = (options.get("preproxy") or "").strip() or None

        # ─ 邮箱来源路由 ─
        # 原来是 if cf_temp / else outlook 的写死分支，加一种邮箱就得回来改。
        # 现在交给注册表工厂：provider 自己从 settings + account 里取需要的字段。
        mail = create_mail_provider(mail_source, db.get_mail_settings(), account)
        logging.getLogger("registrar").info(
            f"[register] 邮箱来源: {mail_source} ({mail.display_name})"
        )

        # ─ 2FA 绑定钩子：插在「拿到 session」和「Codex 授权」之间 ─
        #   主人指定的顺序：注册完 → 绑 2FA → Codex 授权 → 接码。
        #   2FA 必须有 access_token 才能打 mfa/enroll，而 at 只能从 get_auth_session 拿，
        #   所以这是唯一「已有 at 且 Codex 还没跑」的位置（见 auth_flow.py 那处注释）。
        #   钩子里绑成了就把结果存进 _tfa_box，run_register 返回后直接取，不再重绑。
        _tfa_box: dict = {}

        def _bind_2fa_hook(_flow, at: str) -> None:
            # ⚠️ 这里**不查密码**。快路径 bind_totp_2fa_inline 只拿 access_token 打
            #    mfa_info / enroll / activate，全程不碰密码（two_factor.py:153）。
            #    以前拿 flow.result.password 当门禁，把**重跑的老号全挡在门外**：
            #    老号被 OpenAI 认成已有账号 → 本轮不走 register_password →
            #    内存里密码是空的（真密码在库里，靠下面那段回读补），于是 at 明明齐活
            #    也绑不上（实测一个重跑的老号：at 长度 1762 齐活，却被跳过）。
            from .two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(_flow, at)
            if info and info.get("secret"):
                _tfa_box.update(info)
                # ★ 一拿到 secret 立刻落盘，别等后面 Codex 授权 + 接码那几分钟。
                #   接码太久用户一关进程，_tfa_box 内存里的 secret 就永久没了，
                #   而 secret 一次性下发、服务端取不回（跟 _save_password_early 同理）。
                #   ⚠️ 必须用【真正的注册邮箱】flow.result.email，绝不能用外层 email：
                #      非池化 provider（CF 等）外层 email 是占位符
                #      xxx_placeholder_N@placeholder.local，用它落盘会跟后面 save_registered
                #      的真实邮箱对不上 —— 库里凭空多出一条占位垃圾行（两行）。
                #      run_register 一开头就设了 result.email（auth_flow.py:3102），
                #      走到这个钩子时它必然已是真实邮箱；取不到再退回外层 email 兜底。
                #   这里绝不能拖垮注册，包一层 try：落盘失败也还有 _tfa_box 兜着。
                try:
                    real_email = getattr(getattr(_flow, "result", None), "email", "") or email
                    db.save_totp_early(real_email, info["secret"], info.get("factor_id", ""))
                    logging.getLogger("registrar").info(
                        f"[register] 2FA secret 已早落盘 email={real_email}"
                    )
                except Exception as e:
                    logging.getLogger("registrar").warning(
                        f"[register] 2FA secret 早落盘失败（内存仍保留）: {e}"
                    )

        def _account_callback_for_flow(email: str) -> dict:
            """从数据库加载账号凭证（密码和 totp_secret）供 AuthFlow 登录时使用。

            用于既有账号登录场景：当服务端返回 mfa-challenge 时，AuthFlow 需要
            totp_secret 来计算 6 位动态码完成 2FA 验证。
            """
            try:
                data = db.get_registered(email)
                if data:
                    return {
                        "password": data.get("password", ""),
                        "totp_secret": data.get("totp_secret", ""),
                    }
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] account_callback 异常: {e}")
            return {}

        flow = AuthFlow(
            cfg,
            # 与补 refresh 链路对齐：批量并发时传 sms_parallel，让每个号独立租号
            # 独立等码、不持全局 _SMS_VERIFY_LOCK —— 否则 3 个 worker 的绑号
            # 会在这把进程级锁上排队，录屏里"一个号接完才轮下一个"就是这个原因。
            sms_callback=_build_sms_callback(run_id, parallel=bool(options.get("sms_parallel"))),
            env_overrides=env_overrides,
            on_password=_save_password_early,
            on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None,
            account_callback=_account_callback_for_flow,
        )
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] 开始: {email}")

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RuntimeError as e:
            # 部分凭证也算成功（OTP 验证通过 + create_account 成功 → flow.result 有 token）
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # 用户勾选的凭证全拿到 → 算正常完成（不视为 partial）
            wanted_ok = (
                (not need_access or d.get("access_token"))
                and (not need_session or d.get("session_token"))
                and (not need_refresh or d.get("refresh_token"))
            )
            has_any = bool(
                d.get("access_token") or d.get("refresh_token") or d.get("session_token")
            )
            if wanted_ok and has_any:
                logging.getLogger("registrar").warning(
                    f"[register] 流程末段异常但用户勾选的凭证已齐: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] 部分凭证 (缺用户勾选的某项): {e}"
                )
            else:
                raise

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # ─ 密码回读：必须在 2FA 之前 ─
        # ⚠️ d 是**本轮内存里**的结果，它不一定知道这个号有密码：
        #    重跑一个之前设过密码的邮箱时，OpenAI 会认成已有账号 → passwordless_login
        #    → register_password 根本不执行 → d["password"] 是空的，
        #    但上一轮 save_password_early 存的密码还在库里。
        #    两个下游都要它：① 2FA 慢路径要用密码重走 login 链；
        #    ② 前端 done 事件 `v-if="lastRunResult.password"` 判空会把密码行
        #       连同两个复制按钮一起藏掉，主人会以为密码丢了。
        #    以前这段在 2FA **之后**，于是老号在 2FA 眼里永远"无密码"→ 被跳过。
        #    只在 d 里密码为空时查一次，正常路径零额外开销。
        if not (d.get("password") or "").strip():
            try:
                _saved = db.get_registered(d.get("email") or "")
                _pw = ((_saved or {}).get("password") or "").strip()
                if _pw:
                    d["password"] = _pw
                    logging.getLogger("registrar").info(
                        "[register] 本轮未设密码，沿用库中已存密码（上一轮 register_password 留下的）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(f"[register] 回读已存密码失败: {e}")

        # ─ 可选：绑定 TOTP 2FA（仅用户勾选 want_2fa 时才跑） ─
        #   正常情况上面的 on_session_ready 钩子已经在【Codex 授权之前】绑完了，
        #   这里只是兜底：钩子没跑到（run_register 中途抛异常走 partial 分支、
        #   或那时 access_token 还是空）时再补一次。
        #   兜底本身也是先快后慢两条路（见 two_factor.py 模块头）：
        #     快 bind_totp_2fa_inline —— 直接复用刚跑完注册的 flow + access_token，
        #        6.2s 搞定，零 PoW 零邮件（实测 2026-08-08 <测试号>@<自建域>
        #        四个请求全 200，mfa_enabled=true）。
        #     慢 bind_totp_2fa —— 新起 AuthFlow 重走 login 正式链，约 40s + 一次 PoW
        #        + 一封验证码邮件。只在快路径没成时兜底。
        #   失败仅告警、绝不废掉已注册成功的号；secret 一次性下发，成功即随 d 落库+推前端。
        #   ⚠️ 入口条件**不查密码**：快路径只要 access_token。密码只是慢路径
        #      （重走 login 链）的前提，所以判断挪到回落那一步再做。
        if options.get("want_2fa"):
            _emit_status(run_id, "phase", {"phase": "binding_2fa", "email": d.get("email")})
            try:
                from .two_factor import bind_totp_2fa, bind_totp_2fa_inline
                # 钩子（Codex 授权之前那次）已经绑好就直接用，别再打一遍 enroll
                tinfo = dict(_tfa_box) if _tfa_box.get("secret") else None
                if not tinfo:
                    tinfo = bind_totp_2fa_inline(flow, full.get("access_token", ""))
                if not (tinfo and tinfo.get("secret")):
                    # 慢路径要拿密码重登一次，没密码就只能到此为止
                    if (d.get("password") or "").strip():
                        logging.getLogger("registrar").info(
                            "[register] 2FA 快路径未成，回落重走登录链..."
                        )
                        tinfo = bind_totp_2fa(
                            cfg, d.get("email", ""), d.get("password", ""),
                            mail_provider=mail, env_overrides=env_overrides,
                        )
                    else:
                        logging.getLogger("registrar").warning(
                            "[register] 2FA 快路径未成，且该号无密码（库里也没有），"
                            "慢路径走不了，跳过绑定"
                        )
                if tinfo and tinfo.get("secret"):
                    d["totp_secret"] = tinfo["secret"]
                    d["totp_factor_id"] = tinfo.get("factor_id", "")
                    logging.getLogger("registrar").info(
                        f"[register] 2FA 绑定成功 email={d.get('email')}"
                    )
                    _emit_status(run_id, "phase", {"phase": "2fa_bound", "email": d.get("email")})
                else:
                    logging.getLogger("registrar").warning(
                        "[register] 2FA 绑定未成功（账号仍有效，仅未绑 2FA）"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] 2FA 绑定异常（账号仍有效）: {e}"
                )
        # ─ 注册出口/画像：随凭证一并落库 ─
        # save_registered 会把 d 里未列出的字段自动打包进 extra_json，
        # 这里保存 Cloudflare trace 探测到的真实出口 IP、地区码和指纹摘要。
        # proxy 是代理池的连接串，不等于出口 IP，不能当作出口展示。
        # 补 RT 重跑老号时这些也会刷新（新出口/新指纹），符合"这轮就是从哪出口跑的"。
        d["exit_ip"] = (getattr(flow, "_exit_ip", "") or "").strip()
        d["country"] = (getattr(flow, "_country_code", "") or "").strip()
        _fp = getattr(flow, "_fingerprint", None) or {}
        d["fingerprint"] = " / ".join(
            str(x) for x in [
                _fp.get("browser_type", ""),
                _fp.get("screen", ""),
                _fp.get("lang", ""),
                _fp.get("timezone", ""),
                _fp.get("hardware_concurrency", ""),
            ] if x
        ) or ""
        # 落库（密码已在 2FA 之前回读补齐，这里 d 里该有的都有了）
        db.save_registered(d)
        # 非池化 provider 的 email 是虚拟占位（xxx_placeholder_N@placeholder.local），
        # 号池里根本没这行，不能去 mark。判据用 provider 的 pooled，不写死 kind。
        if is_pooled:
            db.mark_done(email)

        # ─ 可选：导出到 CPA / SUB2API 面板（仅勾选启用时才执行） ─
        _try_export_to_panels(run_id, d)

        # ─ 自动检测 Plus 状态（注册完成就顺带，不用事后手动复检） ─
        # 拿刚拿到的 access_token 直接打 chatgpt.com 的 check/v4 接口。
        # 失败不拖垮注册流程（只记日志，该号仍是"未检测"状态，可手动补检）。
        try:
            _auto_check_plus(
                access_token=d.get("access_token", ""),
                proxy=cfg.proxy,
                preproxy=cfg.preproxy,
                email=d.get("email", ""),
            )
        except Exception:
            pass  # 日志已由 _auto_check_plus 内部写，这里不干扰主流程

        result_summary = {
            "email": d.get("email"),
            # 密码走明文推给前端：token 只给长度是因为太长且必须点按钮复制，
            # 但密码是随机 16 位、用户注册完第一件事就是拿去登录，
            # 藏在「查看凭证」弹窗里等于每次都要多点两下。
            # 这是本机自用工具，SSE 只发给本地浏览器，不外传。
            "password": d.get("password") or "",
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            # 2FA secret 一次性下发、服务端取不回，明文推前端让用户当场导入验证器
            # （理由同密码；本机自用工具，SSE 只发本地浏览器）。未绑则为空串。
            "totp_secret": d.get("totp_secret") or "",
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"pw={d.get('password') or '(无)'} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        # ⚠️ 密码是在 register_password 里现生成的，只活在内存里。
        #    走到这里说明 save_registered 没执行过 —— 但 POST user/register 可能**已经成功**，
        #    OpenAI 那边账号连同这个密码已经建好了，只是后续步骤（发码/验证/建账户）挂了。
        #    不打出来的话这个号就成了谁也登不进去的孤儿。这里只写日志不落库，
        #    避免把没有任何 token 的半成品塞进「注册结果」表里。
        try:
            _pw = (flow.result.password or "").strip()
            if _pw:
                logging.getLogger("registrar").error(
                    f"[register] 该号已生成密码，请自行留存: {flow.result.email or email} / {_pw}"
                )
        except Exception:
            pass  # flow 还没建出来（异常发生在 AuthFlow 之前），没密码可救
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # 非池化 provider 没有号池记录，不操作
        if is_pooled:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            else:
                # ⚠️ 已有成功凭证的号不能被标 failed：它注册已经成功了，
                #    失败的是「补充凭证」这一步（重登 / 补 RT / 中途再跑）。
                #    实测 2026-08-21：stag_tellers.5x 注册成功（at/st/2FA 齐全）后
                #    用旧版补 RT 重跑，create_mail_provider 因无取件链接抛错 →
                #    mark_failed 把号池行从 done 覆盖成 failed，号池里"已用成功"变"失败"。
                #    判据：registered 表里有任一 token = 注册成功过。
                _saved = db.get_registered(email)
                _has_real_cred = bool(
                    _saved and (
                        _saved.get("access_token")
                        or _saved.get("session_token")
                        or _saved.get("refresh_token")
                    )
                )
                if _has_real_cred:
                    db.mark_done(email)
                    logging.getLogger("registrar").warning(
                        f"[register] {email} 已有成功凭证，本轮失败不标 failed（保持 done）: {err}"
                    )
                else:
                    db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # env 覆盖现在只挂在 AuthFlow 实例上，随实例一起回收，无需还原。
        try:
            if flow is not None:
                flow.close()
        except Exception:
            pass
        # 关闭 handler
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)  # sentinel: 流结束
        # 线程标记清掉。理论上线程跑完就回收了，但 threading.local 是绑在
        # 线程对象上的，万一以后换成线程池复用线程，残留的 run_id 会让下一个
        # 任务的日志全被投递到上一个 run 的（已关闭的）文件里去。
        _current_run.run_id = None


def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _save_password_early(email: str, password: str) -> None:
    """AuthFlow 的 on_password 回调：密码在 OpenAI 侧一生效就落盘。

    以前密码只在流程**全部**跑通后才随 save_registered 一起入库，
    中间任何一步失败（实测最常见的是 OTP 超时）密码就只剩一行 ERROR 日志兜底 ——
    换台机器、日志轮转、或者干脆没人去翻，号就废了。

    这里存的是"有密码、无凭证"的半成品行，跑通后 save_registered 会用
    同一个 email 主键覆盖补全，不会多出一行对不上的记录。
    """
    log = logging.getLogger("registrar")
    try:
        db.save_password_early(email, password)
        log.info(f"[register] 密码已落盘: {email}（凭证待补）")
    except Exception as e:
        # 落盘失败不能影响注册；下面 except 里那行 ERROR 日志仍然是兜底
        log.warning(f"[register] 密码落盘失败，仅剩日志兜底: {e}")


def _build_sms_callback(run_id: str, parallel: bool = False) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码或未配置 API key 时返回 None，flow 会回退到环境变量路径。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。
    parallel=True（补 refresh 多并发）：每任务独立租号独立等码，
    不持全局接码锁、不复用号码缓存，多个号可以同时绑手机。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    provider_key = (cfg.get("sms_provider") or "").strip().lower()
    api_key = (cfg.get("sms_api_key") or "").strip()
    # 独立手机号池不依赖接码平台的 API Key
    if provider_key not in ("custom_pool", "phone_pool") and not api_key:
        logging.getLogger("registrar").warning("[sms] 已启用接码但未配置 sms_api_key，跳过")
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "sms", "message": msg})
        except Exception:
            pass

    try:
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
            parallel=parallel,
        )
    except Exception as e:
        smslog.warning(f"[sms] 创建接码 controller 失败: {e}")
        return None


def start_registration(account: dict, options: dict) -> str:
    """启动一次注册任务，返回 run_id。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, account["email"], str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def run_session_login(email: str, options: dict) -> dict:
    """轻量协议登录直提会话（刷新AT 第三路径的执行体）。

    邮箱 + 密码(+2FA TOTP) 直接登录 → chatgpt.com/api/auth/session → 新 AT/ST。
    与补 refresh（_do_backfill_login）的本质区别：不开 Codex RT 交换、不绑
    手机号、不接码 —— 只要密码 + 2FA 能过登录就能拿到会话凭证，导入的
    「邮箱----密码----2FA」号首次刷新 AT 就走这里。

    登录链路由 run_protocol_login 驱动：密码验证 → mfa-challenge 自动从
    account_callback 读 totp_secret 提交 TOTP → 拿会话。若 OpenAI 强制要求
    邮箱验证码，需要号池行取件（导入号没有）→ 明确报错不糊弄。

    options:
        proxy / preproxy: str
        password: str          # 已知密码；不传则由 account_callback 从库里读
        cred_source: str       # registered / imported，读凭证的表
    同步阻塞（十几秒到几十秒），适合放在刷新线程池里跑。
    返回 {ok, access_token, session_token, error?}
    """
    email = (email or "").strip().lower()
    if not email:
        return {"ok": False, "error": "缺少邮箱"}
    cfg = Config()
    cfg.proxy = (options.get("proxy") or "").strip() or None
    cfg.preproxy = (options.get("preproxy") or "").strip() or None
    cred_source = options.get("cred_source") or "registered"

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

    env_overrides = {
        "WEBUI_ALLOW_LOGIN": "1",
        "OTP_TIMEOUT": "90",
        # 轻量直提只到 chatgpt.com 会话为止：Codex RT 交换那条支线会引向
        # 绑手机号/接码（烧钱的「补refresh」的活），这里全部关死。
        "OAUTH_CODEX_RT_EXCHANGE": "0",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
        "OAUTH_EXCHANGE_BEFORE_CALLBACK": "0",
        "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
    }
    flow = None
    try:
        flow = AuthFlow(
            cfg,
            sms_callback=None,
            env_overrides=env_overrides,
            account_callback=_account_callback,
        )
        flow.result.email = email
        try:
            res = flow.run_protocol_login(
                mail_provider=None, email=email,
                password=(options.get("password") or ""),
            )
        except AttributeError as ae:
            if "wait_for_otp" in str(ae):
                return {"ok": False, "error": (
                    "登录被要求邮箱验证码：该号不在邮箱号池（无取件链接），拿不到验证码。"
                    "可把该邮箱导入号池后重试，或走「补refresh」走完整登录链"
                )}
            raise
        at = (res.access_token or "").strip()
        st = (res.session_token or "").strip()
        if not at and not st:
            ban = (flow.last_login_error or "").strip()
            if is_ban_signal(ban):
                return {"ok": False, "banned": True, "error": ban[:300]}
            return {"ok": False, "error": (ban or "登录完成但未拿到会话凭证（AT/ST 均空）")[:300]}
        return {"ok": True, "access_token": at, "session_token": st}
    except Exception as e:
        ban = ""
        if flow is not None:
            ban = (flow.last_login_error or "").strip()
        if is_ban_signal(ban) or is_ban_signal(str(e)):
            return {"ok": False, "banned": True, "error": (ban or str(e))[:300]}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        try:
            if flow is not None:
                flow.close()
        except Exception:
            pass


def _do_backfill_login(run_id: str, email: str, options: dict, log_file: Path) -> None:
    """纯登录补 RT：跳过注册全套流程，直接 Codex OAuth 登录授权拿 refresh_token。

    与 _do_register（注册流程）的区别：
      · 不调 run_register —— 不碰 create-account / register-password 那堆请求，
        对存量老号更干净，触发 OpenAI 风控的面更小；
      · account 用 db.get_account(email) 取**真实号池行**（带 relay_url 取件链接），
        修复之前补 RT 用空 account 构造导致 icloud_relay "没有网页/API 取码链接" 的报错；
      · 登录链路由 AuthFlow 内部自动推进：密码 / TOTP 2FA 来自 account_callback
        （读库），邮箱 OTP 走 mail_provider（中转取件），加手机号走 SMS controller；
      · 成功只 merge 更新：拿不到的字段保留库里旧值（含 plus_check），失败不落库。

    options:
        proxy: Optional[str]
        otp_timeout: int
        sms_parallel: bool    # 多并发时启用 SMS 并行接码（独立租号不复用）
        gate: Semaphore       # 并发闸门：开头 acquire，finally release
    """
    _current_run.run_id = run_id

    # 并发闸门：先排队后干活。排队期间日志里写明，前端 run 流能看到。
    gate = options.get("gate")
    gate_acquired = False
    if gate is not None:
        try:
            _emit_status(run_id, "phase", {"phase": "queued", "message": "排队等待并发额度..."})
            logging.getLogger("registrar").info(f"[backfill] 排队等待并发额度: {email}")
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

    mail_source = db.get_setting("mail_source", "outlook")
    email = (email or "").strip().lower()
    flow = None
    try:
        # 号池行：icloud_relay 这类 provider 必须从号池行拿 relay_url（取件链接）才能收 OTP
        account = db.get_account(email) or {}
        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None
        cfg.preproxy = (options.get("preproxy") or "").strip() or None

        env_overrides = {
            "WEBUI_ALLOW_LOGIN": "1",
            "OTP_TIMEOUT": str(int(options.get("otp_timeout") or 180)),
            # 必须显式开 Codex RT 交换：auth_flow 默认可能被 SKIP_* 拦
            "OAUTH_CODEX_RT_EXCHANGE": "1",
            "OAUTH_CODEX_RT_ALLOW_RETRY": "1",
            # 纯登录补 RT：接码轮次耗尽直接细分上报，不回退 env 路径
            "OAUTH_PHONE_REQUIRED": "1",
        }
        mail = create_mail_provider(mail_source, db.get_mail_settings(), account)

        def _account_callback(e: str) -> dict:
            try:
                # cred_source：registered（本站注册）/ imported（导入账号）——
                # 密码/2FA 从对应表读，补出来的凭证也会写回同一张表。
                d = db.get_registered(e, source=options.get("cred_source") or "registered")
                if d:
                    return {
                        "password": d.get("password", ""),
                        "totp_secret": d.get("totp_secret", ""),
                    }
            except Exception:
                pass
            return {}

        flow = AuthFlow(
            cfg,
            sms_callback=_build_sms_callback(run_id, parallel=bool(options.get("sms_parallel"))),
            env_overrides=env_overrides,
            account_callback=_account_callback,
        )
        # 登录推进要读 self.result.email，AuthFlow 默认是空 —— 预先填上
        flow.result.email = email

        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[backfill] 纯登录补 RT 开始: {email}")

        # 执行 + 失败细分。SMS 绑号失败（SmsBindingError）不重试——登录已通过，
        # 重试只会重跑同样烧钱的接码轮次；环境类失败（Sentinel 超时 / 网络）自动重试 1 次。
        def _make_flow():
            # 重试必须重建 AuthFlow：旧会话 cookie/授权 state 已污染，复用会立刻撞回同一个错误
            f = AuthFlow(
                cfg,
                sms_callback=_build_sms_callback(run_id, parallel=bool(options.get("sms_parallel"))),
                env_overrides=env_overrides,
                account_callback=_account_callback,
            )
            f.result.email = email
            return f

        def _run_once(f):
            try:
                return f.oauth_codex_rt_exchange(mail_provider=mail)
            except SmsBindingError as sbe:
                # 细分报错：登录/2FA 已通过，卡在接码绑号；给主人明确的换号重试指引
                sbe_text = sbe.args[0] if sbe.args else str(sbe)
                hint = ""
                if "suspicious behavior from phone" in (sbe.last_err or "").lower() \
                        or "suspicious behavior" in (sbe.last_err or "").lower():
                    hint = "🔴【号段风控】当前国家号段已被 OpenAI 拉黑，请在 SMS 设置里【切换国家/号段】后重试！"
                raise RuntimeError(
                    f"[{email}] 接码失败（登录/2FA 已通过）：{sbe_text}。{hint}"
                    "重新勾选补 refresh 再发起即可换号重试（重新登录后直接进入绑号环节）"
                ) from sbe

        flow = _make_flow()
        ok = _run_once(flow)
        attempt = 0
        while True:
            new = flow.result
            if ok and (new.refresh_token or "").strip():
                break
            err_msg = flow.last_login_error or ""
            # 号段风控关键词：报错里给醒目提示（无论走到哪个细分分支）
            region_ban = (
                "suspicious behavior from phone" in err_msg.lower()
                or "suspicious behavior" in err_msg.lower()
                or "blocked_phone" in err_msg.lower()
                or "phone_number_blocked" in err_msg.lower()
            )
            region_hint = "🔴【号段风控】该国家号段已被 OpenAI 拉黑，请到 SMS 设置【切换国家/号段】后重试！" if region_ban else ""
            # 细分：有 at/st = 登录+2FA 已通过，卡在授权/绑号阶段
            if new.access_token or new.session_token:
                raise RuntimeError(
                    f"[{email}] 登录成功但 Codex OAuth 授权未拿到 refresh_token"
                    + (f"（{err_msg[:150]}）" if err_msg else "") +
                    ("。" + region_hint if region_hint else "") +
                    "。具体原因看上文日志："
                    "① 若看到 add-phone/SMS 接码流程 = 手机验证码收不到或号码无效，"
                    "请换号段/换代理后重试；"
                    "② 若看到 curl (28) 超时 = 授权请求网络超时，请检查代理稳定性。"
                )
            # 无 at/st：登录前段失败 → 判断可否自动重试（环境类）
            transient = (
                "curl (28)" in err_msg
                or "curl(28)" in err_msg
                or "timed out" in err_msg.lower()
                or "timeout" in err_msg.lower()
                or "sentinel" in err_msg.lower()
                or "connection" in err_msg.lower()
            )
            if attempt < 1 and transient:
                attempt += 1
                logging.getLogger("registrar").warning(
                    f"[backfill] [{email}] 登录前段疑似环境失败（{err_msg[:120]}），3 秒后自动重试（第 {attempt}/1 次）"
                )
                _emit_status(run_id, "phase", {
                    "phase": "retry",
                    "message": f"[{email}] 登录前段疑似网络/代理超时，自动重试（{attempt}/1）",
                })
                time.sleep(3.0)
                try:
                    flow.close()
                except Exception:
                    pass
                flow = _make_flow()
                ok = _run_once(flow)
                continue
            raise RuntimeError(
                f"[{email}] 登录失败，未拿到 access_token"
                + (f"（{err_msg[:180]}）" if err_msg else "")
                + ("。" + region_hint if region_hint else "")
                + "。常见原因："
                "① 代理无法连通 OpenAI（看上文 curl 超时日志）；"
                "② 密码/2FA 验证出错；"
                "③ 邮箱 OTP 取件失败。"
            )

        # ─ merge 更新：只覆盖本轮拿到的字段，其余保留库里的旧值 ─
        cred_source = options.get("cred_source") or "registered"
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
        # plus_check 是检查结论，补 RT 不该把它冲掉 —— 从旧 extra 带回来
        if old_extra.get("plus_check") is not None:
            merged["plus_check"] = old_extra["plus_check"]
        # 出口画像：保存真实出口 IP，不保存代理池连接串（两者不是同一件事）。
        # save_registered 会把未列名字段自动打包进 extra_json。
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
        logging.getLogger("registrar").info(
            f"[backfill] 完成 email={email} rt={result_summary['refresh_token_len']} "
            f"at={result_summary['access_token_len']} st={result_summary['session_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(f"[backfill] 失败 (category={category}): {err}")
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # 闸门放行下一个排队的号（不管本轮成败）
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
            q.put(None)  # sentinel: 流结束
        _current_run.run_id = None


def start_backfill_login(email: str, options: dict) -> str:
    """启动一次纯登录补 RT 任务，返回 run_id。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, email, str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_backfill_login,
        args=(run_id, email, options, log_file),
        daemon=True,
        name=f"backfill-{run_id}",
    )
    th.start()
    return run_id


def _auto_check_plus(
    access_token: str,
    proxy: str | None,
    preproxy: str | None,
    email: str,
) -> None:
    """注册完成后自动检测 Plus 状态，直接把结果写进 extra_json.plus_check。

    从 app.py 的 api_check_plus 提取核心逻辑，但做了精简：
      - 只检测一个号（刚注册完的这个）
      - 不依赖前端请求参数（proxy 从 cfg.proxy 取）
      - 失败只记日志，不抛异常（不拖垮注册流程）
    """
    if not (access_token or "").strip():
        return
    logger = logging.getLogger("registrar")
    email = (email or "").strip().lower()
    if not email:
        return

    sess = None
    try:
        from http_client import close_http_session, create_http_session
        from .exporter import _decode_jwt_payload, _get_auth

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/145.0.0.0 Safari/537.36")
        sess = create_http_session(
            proxy=proxy or None,
            preproxy=preproxy or None,
            impersonate="chrome110",
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        payload = _decode_jwt_payload(access_token)
        auth = _get_auth(payload)
        account_id = str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        import uuid as _uuid
        device_id = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"dango-check-plus:{email}"))
        headers["OAI-Device-Id"] = device_id

        resp = sess.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers=headers, timeout=15,
        )
        if resp.status_code == 401:
            # 未过期却 401 = 被吊销，但可能真是封号也可能只是 token 轮换
            body = (resp.text or "")[:200]
            if "deactivated" in body.lower() or "banned" in body.lower():
                db.update_plus_check(email, {"status": "banned", "label": "封号", "checked_at": time.time()})
            else:
                db.update_plus_check(email, {"status": "token_invalid", "label": "凭证失效", "checked_at": time.time()})
            return
        if resp.status_code != 200:
            # 非 401 不可用状态（500/403 等）写 error，不写库
            logger.warning(f"[auto_check] {email} HTTP {resp.status_code}，跳过")
            return

        data = resp.json()
        accts = data.get("accounts", {})
        if not accts:
            return
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})

        from .app import classify_plus_offer

        offer = classify_plus_offer(acct, ent, promo)
        offer["checked_at"] = time.time()
        db.update_plus_check(email, offer)
        logger.info(f"[auto_check] {email} → {offer['status']} ({offer['label']})")

    except Exception as e:
        logger.warning(f"[auto_check] {email} 检测失败（可手动补检）: {e}")
    finally:
        if sess is not None:
            try:
                close_http_session(sess)
            except Exception:
                pass


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)
