"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import backup, db, export_formats, registrar  # noqa: E402
from auth_flow import is_ban_signal  # noqa: E402
from . import imported_backfill  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

# 号码池：启动时清掉上次进程强退留下的 in_use 占用
try:
    _pool_busy = db.pool_reset_in_use()
    if _pool_busy > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_pool_busy} 个占用中的号码池号码")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] pool_reset_in_use 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT AT Refresh WebUI", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _static_no_cache_middleware(request: Request, call_next):
    """静态资源强制协商缓存（no-cache）。

    前端是编译产物、热修是改文件不换名，必须让浏览器每次刷新都回源校验，
    否则启发式缓存会把旧 bundle 吃几天，表现为「明明改了页面却没变化/白屏」。
    未变更的文件走 304，本地开销可忽略。
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "但 Outlook 和 Gmail 都是 4 段，猜不出来，建议前端必填",
    )


class BackupCreateReq(BaseModel):
    reason: str = Field("manual", max_length=80)


class BackupRestoreReq(BaseModel):
    # Deliberate friction: restoring replaces the local database and is never
    # inferred from a button click or an update check.
    confirm: str = Field(..., description="必须填写 RESTORE 以确认回滚")


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    preproxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


# 聚合页只需要知道四个本地服务是否可达；这里刻意不转发响应体、Cookie、
# 授权码或任何业务数据。地址固定为回环地址，避免把这个探活接口变成任意
# URL 请求代理（SSRF）。5050 没有公开 health 路由，因此探测其登录页；
# 5033 和 8801 使用专门的 health 路由。
_LOCAL_SERVICE_PROBES = (
    {"id": "main", "name": "主面板", "url": "http://127.0.0.1:8768/api/health", "port": 8768},
    {"id": "gpt-register", "name": "GPT 注册机", "url": "http://127.0.0.1:5050/", "port": 5050},
    {"id": "change-2fa", "name": "密码 / 2FA", "url": "http://127.0.0.1:5033/api/health", "port": 5033},
    {"id": "muse-register", "name": "Muse 注册机", "url": "http://127.0.0.1:8801/api/health", "port": 8801},
)


def _probe_local_service(item: dict[str, object]) -> dict[str, object]:
    """返回安全的本地服务摘要，不读取或返回响应正文。"""
    started = time.perf_counter()
    req = urllib.request.Request(
        str(item["url"]),
        headers={"Accept": "application/json,text/html;q=0.8", "Cache-Control": "no-cache"},
        method="GET",
    )
    result: dict[str, object] = {
        "id": item["id"],
        "name": item["name"],
        "port": item["port"],
        "url": item["url"],
        "up": False,
        "latency_ms": None,
        "http_status": None,
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=1.2) as response:
            # 只读状态码，不读取正文，避免将凭证/任务数据带入主面板。
            result["http_status"] = int(response.status)
            result["up"] = 200 <= int(response.status) < 500
    except urllib.error.HTTPError as exc:
        # 401/403 仍说明服务已启动；其它 4xx 也算已连接但服务异常。
        result["http_status"] = int(exc.code)
        result["up"] = 400 <= int(exc.code) < 500
        result["error"] = "HTTP_ERROR"
    except (urllib.error.URLError, TimeoutError, OSError):
        result["error"] = "UNREACHABLE"
    finally:
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@app.get("/api/services/status")
def api_services_status():
    """检查本机四个服务的连通性；只返回状态摘要，不返回任何业务数据。"""
    services = [_probe_local_service(item) for item in _LOCAL_SERVICE_PROBES]
    return {
        "ok": all(bool(item["up"]) for item in services),
        "checked_at": time.time(),
        "services": services,
    }


@app.get("/api/backups")
def api_list_backups():
    """List local snapshots only; this endpoint never contacts GitHub."""
    return {"ok": True, "backups": backup.list_backups(), "local_only": True}


@app.post("/api/backups")
def api_create_backup(req: BackupCreateReq):
    """Create a manual local snapshot before a user-approved update."""
    try:
        return {"ok": True, "backup": backup.create_backup(reason=req.reason)}
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, f"创建备份失败：{type(exc).__name__}") from exc


@app.post("/api/backups/{backup_id}/restore")
def api_restore_backup(backup_id: str, req: BackupRestoreReq):
    """Restore only after an explicit RESTORE confirmation string."""
    if req.confirm.strip().upper() != "RESTORE":
        raise HTTPException(400, "请填写 RESTORE 以确认回滚；回滚会替换本地数据库")
    try:
        return backup.restore_backup(backup_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "找不到备份") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(500, f"回滚失败：{type(exc).__name__}") from exc


@app.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = "", search: str = ""
):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind, search=search)
    total = db.count_accounts(status=status, kind=kind, search=search)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    preproxy: str = Field("", description="可选前置 SOCKS（例如 FlClash 本地端口）")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import close_http_session, create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()
    preproxy = (req.preproxy or "").strip() or None

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        sess = None
        try:
            sess = create_http_session(proxy=proxy, preproxy=preproxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}
        finally:
            close_http_session(sess)

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        account = db.claim_next(kind=mail_source)
        if not account:
            raise HTTPException(
                400,
                f"号池里没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "preproxy": req.preproxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs")
def api_runs(limit: int = 50, category: str = ""):
    return {"ok": True, "items": db.list_runs(limit=limit, category=category)}


@app.get("/api/runs/status")
def api_runs_status(ids: str = ""):
    """按 run_id 批量查运行状态（补 refresh 横幅轮询用）。

    ids=逗号分隔的 run_id 列表。补 refresh 启动接口已把每个号的 run_id
    返回给前端，前端每 3s 轮询本接口聚合进度：status!=='running' 即终态
    （done/failed 由 finish_run 写入）。返回条目里查不到的 id 视为已被清库，
    前端按返回条目为准。
    """
    wanted = [s.strip() for s in (ids or "").split(",") if s.strip()][:200]
    return {"ok": True, "items": db.get_runs_status(wanted)}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all", search: str = "",
                   source: str = "registered"):
    """source=registered（本站注册，默认）/ imported（导入账号，独立表）。"""
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter,
                               search=search, source=source)
    total = db.count_registered(filter_rt=filter, search=search, source=source)
    return {"ok": True, "items": items, "total": total}


@app.get("/api/registered/{email}")
def api_registered_one(email: str, source: str = "registered"):
    row = db.get_registered(email, source=source)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str, source: str = "registered"):
    ok = db.delete_registered(email, source=source)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False
    source: str = Field("registered", description="registered / imported")


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    src = req.source
    if req.all:
        n = db.delete_all_registered(source=src)
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails, source=src)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 导入账号（外部注册的号） ────────────────────────


class ImportAccountsReq(BaseModel):
    text: str = Field(..., description="粘贴的账号文本，每行一条：邮箱----密码----2FA")


@app.post("/api/imported/import")
def api_imported_import(req: ImportAccountsReq):
    """导入别处注册的账号（三段式：邮箱----密码----2FA，与导出前三列同构）。

    - 逐行解析，容忍空行/首尾空白；多于三段的行忽略多余段（兼容导出格式粘贴）。
    - 邮箱自动小写；2FA 过 base32 校验（与手填凭证同一套 normalize）。
    - upsert：新邮箱插入；已有邮箱只更新密码/2FA，**保留已补出的 token 与原
      导入时间**（重复导入绝不能把补 refresh 拿到的凭证冲没）。
    - 落 imported 独立表，与本站注册的 registered 物理隔离。
    """
    text = req.text or ""
    inserted, updated, errors = 0, 0, []
    seen: set[str] = set()
    for ln_no, raw in enumerate(text.splitlines(), 1):
        ln = raw.strip()
        if not ln:
            continue
        parts = [p.strip() for p in ln.split("----")]
        if len(parts) < 2:
            errors.append(f"第{ln_no}行：格式不对（需要 邮箱----密码----2FA，「----」四个减号）")
            continue
        email = parts[0].lower()
        password = parts[1]
        totp = parts[2] if len(parts) > 2 else ""
        if not email or "@" not in email or " " in email:
            errors.append(f"第{ln_no}行：邮箱不合法「{parts[0][:40]}」")
            continue
        if email in seen:
            errors.append(f"第{ln_no}行：{email} 在本次粘贴中重复，已跳过")
            continue
        seen.add(email)
        try:
            res = db.upsert_imported(email, password, totp)
        except ValueError as e:
            errors.append(f"第{ln_no}行：{email} —— {e}")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"第{ln_no}行：{email} —— 写库失败 {type(e).__name__}: {e}")
            continue
        if res == "inserted":
            inserted += 1
        else:
            updated += 1
    logger.info(f"[imported] 导入完成：新增 {inserted}，更新 {updated}，错误 {len(errors)}")
    return {"ok": True, "inserted": inserted, "updated": updated, "errors": errors}


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部（跨页），忽略 emails")
    source: str = Field("registered", description="registered / imported")


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.all:
        rows = db.list_registered_full(limit=100000, source=req.source)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails, source=req.source)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
        # 这一批导出的 email 原样带回去 —— 前端「下载并删除」照着它删，删得准。
        # ⚠️ 必须由后端给：`all=true` 时前端手里只有当前页那 20 行，
        #    自己凑列表会漏删；而用 all/status 那种"全清"接口去删号池，
        #    会把**还没跑过的号**一起清掉。所以这里回传精确列表。
        "emails": [(r.get("email") or "") for r in rows],
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


class BackfillRTReq(BaseModel):
    emails: list[str] = Field(..., description="要补 refresh_token 的 email 列表（库里已有凭证的号）")
    proxy: Optional[str] = Field(None, description="本次重登使用的代理（留空=沿用注册时的默认）")
    otp_timeout: int = Field(180, description="验证码超时秒数")
    concurrency: int = Field(1, description="并发数 1-20；>1 时必须配合 proxy_pool 一起用")
    proxy_pool: Optional[str] = Field(
        None,
        description="多代理池文本（每行一个，与批量页同格式）。"
                    "给了就按序轮询分配给各号；为空时回落到 proxy 单代理/直连。",
    )
    preproxy: Optional[str] = Field("", description="可选前置 SOCKS（TUN 环境填本地端口，走链路桥）")
    source: str = Field("registered", description="registered / imported —— 凭证读取与成功写回的数据源")


@app.post("/api/registered/backfill_rt")
def api_backfill_rt(req: BackfillRTReq):
    """对缺 refresh_token 的号补跑登录授权，拿新 RT 写回。

    轻量化方案：**不改协议、不新写登录链** —— 复用 AuthFlow 的 Codex OAuth
    登录分支（oauth_codex_rt_exchange）：
      · 只登录不注册：跳过 run_register 的 create-account / register-password
        那堆请求，对存量老号更干净、触发风控的面更小；
      · 登录所需密码 / TOTP 2FA 由 account_callback 从 registered 表自动回读，
        邮箱 OTP 走号池行的中转取件链接，加手机号走 SMS controller；
      · 成功只 merge 更新（拿不到的字段保留旧值，plus_check 不丢），
        完全失败不落库。
      · 并发：concurrency>1 时用信号量限流，N 个号最多同时跑 N 线程，
        其余排队等前面的跑完接着上（每个号仍是独立 run，日志/状态照旧分流）；
        并发同时启用 SMS 并行接码（每任务独立租号，不复用缓存不串号）。

    返回每个号的 run_id，前端拿去订阅 /api/runs/{run_id}/stream 看进度。
    """
    cleaned = [e.strip().lower() for e in (req.emails or []) if e and e.strip()]
    if not cleaned:
        raise HTTPException(400, "emails 不能为空")

    # 并发数：1-20；勾号只有 1 个时并发无意义，自动降为 1
    concurrency = max(1, min(20, int(req.concurrency or 1)))
    concurrency = min(concurrency, len(cleaned))

    # 代理分配优先级：proxy_pool（轮询）> proxy 单代理（全体共用）> 直连。
    # 之前这里读 settings.proxy_pool，但那个设置全项目没有任何写入入口，
    # 是死配置 —— 现在由前端把代理池页的文本直接传上来（和批量页同源）。
    proxy_list: list[str] = []
    pool_text = (req.proxy_pool or "").strip()
    single_proxy = (req.proxy or "").strip()
    if pool_text:
        proxy_list = [p.strip() for p in pool_text.splitlines() if p.strip()]
        logger.info(
            f"[backfill] 代理池 {len(proxy_list)} 个代理轮询分配给 {len(cleaned)} 个号"
            f"（并发 {concurrency}）"
        )
    elif single_proxy:
        proxy_list = [single_proxy]
        logger.info(
            f"[backfill] 全部 {len(cleaned)} 个号共用选定代理（并发 {concurrency}）"
        )
    else:
        logger.info(f"[backfill] 未指定代理，{len(cleaned)} 个号走直连（并发 {concurrency}）")

    # 多并发安全性闸门：只在「明确选了恰好 1 个代理」时降为串行。
    # 留空直连（proxy_list 为空）是用户主动要走本机 IP，不再压成 1，
    # 与刷新 AT 同一口径：勾 >2 个就按所选并发一起跑。
    if concurrency > 1 and len(proxy_list) == 1:
        concurrency = 1
        logger.warning("[backfill] 只选了 1 个代理，并发降为 1（防止同 IP 风控关联）")

    # 并发模式下启用 SMS 并行接码：每个任务独立租号独立等码，不再全局串行。
    # 走 per-run options 传递，避免模块级全局在多请求并发时互相踩。
    sms_parallel = concurrency > 1

    runs = []
    if concurrency <= 1:
        # 原行为：一口气全起（无并发限制需求时保持轻量）
        for i, email in enumerate(cleaned):
            proxy = proxy_list[i % len(proxy_list)] if proxy_list else None
            options = {
                "proxy": proxy,
                "otp_timeout": int(req.otp_timeout),
                "cred_source": req.source,
            }
            try:
                run_id = registrar.start_backfill_login(email, options)
                runs.append({"email": email, "run_id": run_id, "proxy": proxy or "直连"})
            except Exception as e:
                runs.append({"email": email, "run_id": None, "error": str(e)[:200], "proxy": proxy or "直连"})
    else:
        # 并发模式：信号量作为并发闸门塞进每个 run 的 options，
        # 由 run 线程自己在开头 acquire、结束时 release —— 这样 API 立刻
        # 返回全部 run_id（排队的也立刻有号），而实际同时在跑的最多
        # concurrency 个，跑完一个自动放行下一个。
        gate = threading.Semaphore(concurrency)
        for i, email in enumerate(cleaned):
            proxy = proxy_list[i % len(proxy_list)] if proxy_list else None
            options = {
                "proxy": proxy,
                "otp_timeout": int(req.otp_timeout),
                "sms_parallel": sms_parallel,
                "gate": gate,
                "cred_source": req.source,
            }
            try:
                run_id = registrar.start_backfill_login(email, options)
                runs.append({"email": email, "run_id": run_id, "proxy": proxy or "直连"})
            except Exception as e:
                runs.append({"email": email, "run_id": None, "error": str(e)[:200], "proxy": proxy or "直连"})
    return {"ok": True, "runs": runs, "count": len(runs), "concurrency": concurrency}


@app.post("/api/imported/backfill_rt")
def api_imported_backfill_rt(req: BackfillRTReq):
    """「导入账号」专用补 refresh —— 零邮箱接码依赖。

    导入号（imported 表，email----密码----2FA）没有也不该有取件链接，
    补 RT 走 密码→TOTP 2FA→Codex OAuth 纯登录链，全程不创建 mail provider；
    换号重驱动登录也无需邮箱接码。拿到 refresh_token 即写回 imported 表。
    代理池轮询 / 并发闸门规则与号池版一致，worker 换成
    imported_backfill.start_imported_backfill。SSE 进度沿用 /api/runs/{
id}/stream。
    """
    cleaned = [e.strip().lower() for e in (req.emails or []) if e and e.strip()]
    if not cleaned:
        raise HTTPException(400, "emails 不能为空")
    concurrency = max(1, min(20, int(req.concurrency or 1)))
    concurrency = min(concurrency, len(cleaned))
    proxy_list: list[str] = []
    pool_text = (req.proxy_pool or "").strip()
    single_proxy = (req.proxy or "").strip()
    if pool_text:
        proxy_list = [p.strip() for p in pool_text.splitlines() if p.strip()]
        logger.info(f"[imported-backfill] 代理池 {len(proxy_list)} 个代理轮询分配给 {len(cleaned)} 个号（并发 {concurrency}）")
    elif single_proxy:
        proxy_list = [single_proxy]
        logger.info(f"[imported-backfill] 全部 {len(cleaned)} 个号共用选定代理（并发 {concurrency}）")
    else:
        logger.info(f"[imported-backfill] 未指定代理，{len(cleaned)} 个号走直连（并发 {concurrency}）")
    if concurrency > 1 and len(proxy_list) == 1:
        concurrency = 1
        logger.warning("[imported-backfill] 只选了 1 个代理，并发降为 1（防止同 IP 风控关联）")
    sms_parallel = concurrency > 1
    preproxy_opt = (req.preproxy or "").strip() or None
    runs = []
    gate = threading.Semaphore(concurrency) if concurrency > 1 else None
    for i, email in enumerate(cleaned):
        proxy = proxy_list[i % len(proxy_list)] if proxy_list else None
        options = {"proxy": proxy, "preproxy": preproxy_opt, "otp_timeout": int(req.otp_timeout), "cred_source": "imported"}
        if gate is not None:
            options["gate"] = gate
            options["sms_parallel"] = sms_parallel
        try:
            run_id = imported_backfill.start_imported_backfill(email, options)
            runs.append({"email": email, "run_id": run_id, "proxy": proxy or "直连"})
        except Exception as e:
            runs.append({"email": email, "run_id": None, "error": str(e)[:200], "proxy": proxy or "直连"})
    return {"ok": True, "runs": runs, "count": len(runs), "concurrency": concurrency}


class RefreshATReq(BaseModel):
    emails: list[str] = Field(..., description="要刷新 access_token 的 email 列表（库里已有 refresh_token 的号）")
    proxy: Optional[str] = Field("", description="单代理（全部共用；留空=直连）")
    concurrency: int = Field(1, description="并发数 1-20；建议配合 proxy_pool 使用")
    proxy_pool: Optional[str] = Field("", description="多代理池文本（每行一个），按序轮询分配给各号")
    source: str = Field("registered", description="registered / imported —— 凭证读取与写回的数据源")


@app.post("/api/registered/refresh_at")
def api_refresh_at(req: RefreshATReq):
    """三路径刷新 access_token（不碰邮箱接码，不烧接码额度）。

    路径1（有 refresh_token）：POST /oauth/token refresh_token grant → 新 Codex
    AT + RT 滚动写回。注意 RT 只有绑过手机号的号才有。
    路径2（免 RT）：session_token → GET chatgpt.com/api/auth/session → 新
    accessToken —— 与注册链路发 AT 的是同一条会话端点（auth_flow.get_auth_session
    同款），不需要 refresh_token、不需要绑手机号；NextAuth 会话滚动下发的新
    session-token 自动写回（不写回旧 ST 迟早过期）。
    路径3（免 RT/ST）：邮箱+密码(+2FA) 协议登录 → 会话直提（registrar.
    run_session_login，run_protocol_login 驱动、不开 Codex RT 交换、不绑手机号）。
    导入的「邮箱----密码----2FA」号首次刷新 AT 走这里；较慢（单个约几十秒）。

    单号级联：有 RT 先走路径1，RT 失效自动落到路径2；无 RT 直接路径2；
    RT/ST 都没有但有密码 → 路径3；RT/ST 都失效也用密码兜底一把。
    与「补 refresh」共用同一套并发 + 代理池策略（显式传入的 proxy_pool 轮询 >
    单代理共用 > 直连；明确只选 1 个代理且没传池时并发自动降为 1。本页下拉留空
    不传池，按所选并发走本机 IP；下拉选中且池里 ≥2 个、勾选 >2 个时才传整池轮询）。
    失败不落库，原凭证保留。
    """
    # 去重保序
    seen: set[str] = set()
    cleaned: list[str] = []
    for e in (req.emails or []):
        e = (e or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            cleaned.append(e)
    if not cleaned:
        raise HTTPException(400, "emails 不能为空")

    concurrency = max(1, min(20, int(req.concurrency or 1)))
    concurrency = min(concurrency, len(cleaned))

    # 代理分配：与 backfill_rt 同规则
    proxy_list: list[str] = []
    pool_text = (req.proxy_pool or "").strip()
    single_proxy = (req.proxy or "").strip()
    if pool_text:
        proxy_list = [p.strip() for p in pool_text.splitlines() if p.strip()]
        logger.info(f"[refresh_at] 代理池 {len(proxy_list)} 个代理轮询分配给 {len(cleaned)} 个号（并发 {concurrency}）")
    elif single_proxy:
        proxy_list = [single_proxy]
        logger.info(f"[refresh_at] 全部 {len(cleaned)} 个号共用选定代理（并发 {concurrency}）")
    else:
        logger.info(f"[refresh_at] 未指定代理，{len(cleaned)} 个号走直连（并发 {concurrency}）")
    if concurrency > 1 and len(proxy_list) == 1:
        concurrency = 1
        logger.warning("[refresh_at] 只选了 1 个代理，并发降为 1（防同 IP 关联）")

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def _merge_save(email: str, **token_fields) -> dict:
        """只覆盖本轮拿到的 token 字段，其余保留库里的旧值。"""
        old = db.get_registered(email, source=req.source) or {}
        old_extra = old.get("extra") or {}
        merged = {
            "email": email,
            "password": old.get("password") or "",
            "access_token": old.get("access_token", ""),
            "session_token": old.get("session_token", ""),
            "refresh_token": old.get("refresh_token", ""),
            "id_token": old.get("id_token", ""),
            "device_id": old.get("device_id", ""),
            "csrf_token": old.get("csrf_token", ""),
            "cookie_header": old.get("cookie_header", ""),
            "totp_secret": old.get("totp_secret", ""),
            "totp_factor_id": old.get("totp_factor_id", ""),
        }
        merged.update(token_fields)
        if old_extra.get("plus_check") is not None:
            merged["plus_check"] = old_extra["plus_check"]
        db.save_registered(merged, source=req.source)
        return old

    def _refresh_via_rt(email: str, refresh_token: str, proxy: Optional[str]) -> dict:
        """路径1（有 RT）：POST /oauth/token（refresh_token grant）→ 新 Codex AT + RT 滚动。"""
        from http_client import close_http_session, create_http_session

        sess = create_http_session(proxy=proxy or None)
        try:
            client_id = (os.getenv("OAUTH_CODEX_CLIENT_ID", "") or "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
            scope = (os.getenv("OAUTH_CODEX_SCOPE", "") or "").strip() or "openid email profile offline_access"
            resp = sess.post(
                "https://auth.openai.com/oauth/token",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": UA,
                },
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "scope": scope,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                body = (getattr(resp, "text", "") or "")[:200].replace("\n", " ")
                if "invalid_grant" in body:
                    return {"ok": False, "fatal": True, "error": "RT 已失效/被吊销"}
                return {"ok": False, "fatal": False, "error": f"HTTP {resp.status_code}: {body}"}
            data = resp.json() if resp is not None else {}
            new_at = (data.get("access_token") or "").strip()
            new_rt = (data.get("refresh_token") or "").strip()
            if not new_at:
                return {"ok": False, "fatal": True, "error": "响应缺 access_token"}
            extra: dict = {
                "access_token": new_at,
                "refresh_token": new_rt or refresh_token,
                "id_token": data.get("id_token") or "",
            }
            # RT grant 只换 Codex AT/RT，不下发 chatgpt.com 的 session-token。
            # 库里已有 ST 不重复打，避免滚动掉旧会话。
            old_row = db.get_registered(email, source=req.source) or {}
            if not (old_row.get("session_token") or "").strip():
                filled = _fill_session_from_at(sess, new_at, old_row.get("device_id") or "")
                if filled.get("session_token"):
                    extra["session_token"] = filled["session_token"]
                if filled.get("cookie_header"):
                    extra["cookie_header"] = filled["cookie_header"]
            _merge_save(email, **extra)
            return {"ok": True, "at_len": len(new_at), "rt_rotated": bool(new_rt)}
        except Exception as e:
            return {"ok": False, "fatal": False, "error": f"{type(e).__name__}: {e}"[:200]}
        finally:
            try:
                close_http_session(sess)
            except Exception:
                pass

    def _fill_session_from_at(sess, access_token: str, device_id: str) -> dict:
        """库里没有 session_token 时，用刚换到的 AT 向 chatgpt.com 要一次。

        Bearer AT 打 /api/auth/session，NextAuth 会 Set-Cookie 下发
        __Secure-next-auth.session-token。拿不到不报错——AT/RT 已经换到了，
        session 只是补齐项。
        """
        out: dict = {}
        try:
            did = (device_id or "").strip() or str(uuid.uuid4())
            try:
                sess.cookies.set("oai-did", did, domain=".chatgpt.com")
            except Exception:
                sess.cookies.set("oai-did", did)
            resp = sess.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Referer": "https://chatgpt.com/",
                    "Origin": "https://chatgpt.com",
                    "User-Agent": UA,
                },
                timeout=30,
            )
            if getattr(resp, "status_code", 0) != 200:
                logger.info(f"[refresh_at] 补 session 未成功: HTTP {getattr(resp, 'status_code', 0)}")
                return out
            new_st = ""
            try:
                new_st = (sess.cookies.get("__Secure-next-auth.session-token", "") or "").strip()
            except Exception:
                new_st = ""
            if not new_st:
                return out
            out["session_token"] = new_st
            try:
                parts = []
                for c in sess.cookies:
                    name = getattr(c, "name", "") or ""
                    domain = (getattr(c, "domain", "") or "").lstrip(".")
                    if name and "chatgpt.com" in domain:
                        parts.append(f"{name}={getattr(c, 'value', '') or ''}")
                if parts:
                    out["cookie_header"] = "; ".join(parts)
            except Exception:
                pass
        except Exception as e:
            logger.info(f"[refresh_at] 补 session 跳过: {type(e).__name__}: {e}")
        return out

    def _refresh_via_st(email: str, session_token: str, device_id: str, proxy: Optional[str]) -> dict:
        """路径2（免 RT）：session_token → GET chatgpt.com/api/auth/session → 新 accessToken。

        这就是注册链路发 AT 的同一条会话端点（auth_flow.get_auth_session /
        from_existing_credentials 同款）—— 不需要 refresh_token、不需要绑手机号。
        NextAuth 会话是滚动的：响应可能 Set-Cookie 下发新 session-token，必须写回，
        否则旧 ST 迟早过期把号变死。
        """
        from http_client import close_http_session, create_http_session

        sess = create_http_session(proxy=proxy or None)
        try:
            did = (device_id or "").strip() or str(uuid.uuid4())
            try:
                sess.cookies.set("oai-did", did, domain=".chatgpt.com")
                sess.cookies.set("__Secure-next-auth.session-token", session_token, domain=".chatgpt.com")
            except Exception:
                sess.cookies.set("oai-did", did)
                sess.cookies.set("__Secure-next-auth.session-token", session_token)
            resp = sess.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "Accept": "application/json",
                    "Referer": "https://chatgpt.com/",
                    "Origin": "https://chatgpt.com",
                    "User-Agent": UA,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                body = (getattr(resp, "text", "") or "")[:160].replace("\n", " ")
                return {"ok": False, "fatal": True, "error": f"HTTP {resp.status_code}: {body}"}
            data = resp.json() if resp is not None else {}
            new_at = (data.get("accessToken") or "").strip()
            if not new_at:
                return {"ok": False, "fatal": True,
                        "error": "session_token 已失效（/api/auth/session 未返回 AT），请走「补refresh」重新登录"}
            # 滚动会话：响应 cookie 里可能有新 session-token
            new_st = ""
            try:
                new_st = (sess.cookies.get("__Secure-next-auth.session-token", "") or "").strip()
            except Exception:
                pass
            st_rotated = bool(new_st) and new_st != session_token
            extra: dict = {"access_token": new_at}
            if st_rotated:
                extra["session_token"] = new_st
                # cookie_header 里存的旧 ST 同步替换，保持「查看凭证」一致
                old = db.get_registered(email, source=req.source) or {}
                ch = old.get("cookie_header") or ""
                if ch and session_token in ch:
                    extra["cookie_header"] = ch.replace(session_token, new_st)
            _merge_save(email, **extra)
            return {"ok": True, "at_len": len(new_at), "st_rotated": st_rotated}
        except Exception as e:
            return {"ok": False, "fatal": False, "error": f"{type(e).__name__}: {e}"[:200]}
        finally:
            try:
                close_http_session(sess)
            except Exception:
                pass

    def _refresh_via_login(email: str, proxy: Optional[str]) -> dict:
        """路径3（免 RT/ST）：邮箱+密码(+2FA) 协议登录 → chatgpt.com 会话直提。

        registrar.run_session_login 驱动完整登录链（密码 → TOTP 2FA → 会话），
        但不开 Codex RT 交换、不绑手机号、不接码 —— 跟「补refresh」那条重链路
        的本质区别。慢（单个约十几秒到几十秒），只在前两条路没货时兜底；
        导入的「邮箱----密码----2FA」号首次刷新 AT 走的就是这条路。
        """
        cred0 = db.get_registered(email, source=req.source) or {}
        pw = (cred0.get("password") or "").strip()
        if not pw:
            return {"ok": False, "fatal": True,
                    "error": "库里无密码，无法走登录直提（可在「编辑」补密码，或走「补refresh」重新登录授权）"}
        r = registrar.run_session_login(email, {
            "proxy": proxy,
            "password": pw,
            "cred_source": req.source,
        })
        if not r.get("ok"):
            err = (r.get("error") or "登录直提失败")[:300]
            if r.get("banned") or is_ban_signal(err):
                try:
                    db.update_plus_check(email, {
                        "status": "banned", "label": "已封号",
                        "checked_at": time.time(),
                        "note": "刷新AT时 OpenAI 返回账号已删除/停用",
                    }, source=req.source)
                except Exception as tag_err:
                    logger.warning(f"[refresh_at] [{email}] 封号标签写入失败: {tag_err}")
                return {"ok": False, "banned": True,
                        "error": "🚫 已封号（OpenAI 返回：账号已删除或停用，不可恢复）—— 已打「已封号」标签"}
            return {"ok": False, "error": err}
        extra: dict = {}
        at = (r.get("access_token") or "").strip()
        st = (r.get("session_token") or "").strip()
        if at:
            extra["access_token"] = at
        if st:
            extra["session_token"] = st
        if not extra:
            return {"ok": False, "error": "登录完成但未拿到会话凭证"}
        # 只覆盖拿到的字段：库里已有的 RT/ST/其他凭证不动（与 _merge_save 同语义）
        _merge_save(email, **extra)
        return {"ok": True, "at_len": len(at), "login_refreshed": True}

    def _do_refresh(email: str, proxy: Optional[str]) -> dict:
        """单号刷新（三路径级联）：有 RT 走 RT grant；RT 失效/无 RT 用 session_token
        走登录会话直提；连 RT/ST 都没有的号（如新导入）用 密码+2FA 直接登录
        拿会话。三条路都失败才报错。"""
        cred = db.get_registered(email, source=req.source) or {}
        rt = (cred.get("refresh_token") or "").strip()
        st = (cred.get("session_token") or "").strip()
        did = (cred.get("device_id") or "").strip()
        pw = (cred.get("password") or "").strip()

        attempts: list[str] = []
        if rt:
            r = _refresh_via_rt(email, rt, proxy)
            if r.get("ok"):
                return {**r, "via": "refresh_token"}
            attempts.append(f"RT: {r.get('error', '')}")
        if st:
            r2 = _refresh_via_st(email, st, did, proxy)
            if r2.get("ok"):
                return {**r2, "via": "session_token"}
            attempts.append(f"ST: {r2.get('error', '')}")
        if not rt and not st:
            if not pw:
                return {"ok": False,
                        "error": "库里无 refresh_token / session_token / 密码，刷新无从下手（可在「编辑」补密码，或走「补refresh」重新登录授权）"}
            # 第三路径：无 RT/ST 但有密码(+2FA) → 直接登录会话直提（导入号常规首刷）
            r3 = _refresh_via_login(email, proxy)
            if r3.get("ok"):
                return {**r3, "via": "login"}
            return {"ok": False, "error": r3.get("error", "登录直提失败")[:300]}
        # 有 RT/ST 但都失效了：密码兜底再试一把（导入号/改过密码的号常这样救活）
        if pw:
            r3 = _refresh_via_login(email, proxy)
            if r3.get("ok"):
                return {**r3, "via": "login"}
            attempts.append(f"登录直提: {r3.get('error', '')}")
        return {"ok": False, "error": ("；".join(attempts) or "未知错误")[:300]}

    results: list[dict] = []
    todo: list[tuple[str, Optional[str]]] = []
    for i, email in enumerate(cleaned):
        proxy = proxy_list[i % len(proxy_list)] if proxy_list else None
        cred = db.get_registered(email, source=req.source) or {}
        has_rt = bool((cred.get("refresh_token") or "").strip())
        has_st = bool((cred.get("session_token") or "").strip())
        has_pw = bool((cred.get("password") or "").strip())
        if not has_rt and not has_st and not has_pw:
            results.append({
                "email": email, "ok": False, "proxy": proxy or "直连",
                "error": "库里无 refresh_token / session_token / 密码，刷新无从下手（可在「编辑」补密码，或走「补refresh」重新登录授权）",
            })
        else:
            todo.append((email, proxy))

    if concurrency <= 1:
        for email, proxy in todo:
            results.append({"email": email, **_do_refresh(email, proxy), "proxy": proxy or "直连"})
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futs = {pool.submit(_do_refresh, e, px): (e, px) for e, px in todo}
            for fut, (e, px) in futs.items():
                r = fut.result()
                r["email"] = e
                r["proxy"] = px or "直连"
                results.append(r)

    ok_n = sum(1 for r in results if r.get("ok"))
    # 结果按输入顺序重排（无 RT 跳过的和进线程池的混在一起后要保持一一对应）
    by_email = {r["email"]: r for r in results}
    results = [by_email[e] for e in cleaned if e in by_email]
    logger.info(f"[refresh_at] 完成: 成功 {ok_n}/{len(results)}")
    return {"ok": True, "results": results, "count": len(results), "concurrency": concurrency}


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_fixed_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）
    sms_pool_auto_disable: Optional[str] = None    # 号码池：失败自动禁用 '0'/'1'
    sms_pool_fail_threshold: Optional[str] = None  # 号码池：连续失败几次自动禁用（默认 2）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    # 独立手机号池：余额语义 = 池里可取的号数
    if (cfg.get("sms_provider") or "") in ("custom_pool", "phone_pool"):
        stats = db.pool_stats()
        return {
            "ok": True,
            "provider": "custom_pool",
            "balance": stats.get("idle", 0),
            "message": (
                f"号码池可用 {stats.get('idle', 0)} 个"
                f"（总数 {stats.get('total', 0)} · 禁用 {stats.get('disabled', 0)} ·"
                f" 使用中 {stats.get('in_use', 0)} · 累计成功 {stats.get('total_success', 0)}）"
            ),
        }
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    # 独立手机号池没有「平台国家」概念，直接返回空，让前端跳过国家选择
    if (provider or "").strip().lower() in ("custom_pool", "phone_pool"):
        return {"ok": True, "countries": [], "openai_sms_safe": [], "source": "pool"}

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 独立手机号池 ────────────────────────


@app.get("/api/settings/sms/pool")
def api_sms_pool_list(search: str = "", filter: str = "all"):
    """号码池列表 + 统计 + 池相关配置（供「手机号池（独立）」页使用）。"""
    return {
        "ok": True,
        "items": db.pool_list(search=search, filter_mode=filter),
        "stats": db.pool_stats(),
        "config": {
            "sms_pool_auto_disable": db.get_setting("sms_pool_auto_disable", "1"),
            "sms_pool_fail_threshold": db.get_setting("sms_pool_fail_threshold", "2"),
            "sms_phone_success_max": db.get_setting("sms_phone_success_max", "3"),
        },
    }


class PoolImportReq(BaseModel):
    text: str = Field(..., description="每行：号码----取码URL 或 号码|URL；也支持号码一行、URL 一行")


@app.post("/api/settings/sms/pool/import")
def api_sms_pool_import(req: PoolImportReq):
    if not (req.text or "").strip():
        raise HTTPException(400, "导入内容为空")
    result = db.pool_import(req.text)
    return {"ok": True, **result, "stats": db.pool_stats()}


class PoolUpdateReq(BaseModel):
    id: int
    action: str = Field(..., description="enable / disable / delete / reset_usage / increment_usage / edit_url")
    url: Optional[str] = None
    reason: Optional[str] = None


@app.post("/api/settings/sms/pool/update")
def api_sms_pool_update(req: PoolUpdateReq):
    action = (req.action or "").strip().lower()
    try:
        if action == "enable":
            ok = db.pool_set_enabled(req.id, True)
        elif action == "disable":
            ok = db.pool_set_enabled(req.id, False, reason=req.reason or "手动禁用")
        elif action == "delete":
            ok = db.pool_delete(req.id)
        elif action == "reset_usage":
            ok = db.pool_reset_usage(req.id)
        elif action == "increment_usage":
            db.pool_increment_usage(req.id)
            ok = True
        elif action == "edit_url":
            ok = db.pool_edit_url(req.id, req.url or "")
        else:
            raise HTTPException(400, f"未知操作: {action}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "号码不存在")
    return {"ok": True, "stats": db.pool_stats()}


class PoolBulkReq(BaseModel):
    action: str = Field(..., description="clear_usage / delete_all / enable_all")


@app.post("/api/settings/sms/pool/bulk")
def api_sms_pool_bulk(req: PoolBulkReq):
    action = (req.action or "").strip().lower()
    if action == "clear_usage":
        n = db.pool_clear_usage()
    elif action == "delete_all":
        n = db.pool_delete_all()
    elif action == "enable_all":
        n = db.pool_enable_all()
    else:
        raise HTTPException(400, f"未知操作: {action}")
    return {"ok": True, "affected": n, "stats": db.pool_stats()}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")
    source: str = Field("registered", description="registered / imported")


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered），所以 totp_secret 必须过 base32 校验 —— 脏值存进去
    要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret,
            source=req.source,
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="单代理兜底（无池时全体共用；留空直连）")
    proxy_pool: str = Field("", description="多代理池文本，逐号轮询分配")
    preproxy: str = Field("", description="可选前置 SOCKS（TUN 环境用）")
    source: str = Field("registered", description="registered / imported")


# 封号在 401/403 响应体里的措辞。OpenAI 不止一种写法，全部小写后子串匹配。
# 新措辞加在这里即可；日志会打出未匹配的 401/403 原文方便补充。
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """安全取响应体文本，任何异常都不许打断检测循环。"""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


def classify_plus_offer(acct: dict, ent: dict, promo: dict) -> dict:
    """把 check/v4 的套餐字段收成页面文案。

    不写死活动 ID。实测 free 号的活动在 eligible_promo_campaigns，
    metadata 里带折扣比例和月数：
      percentage=100, num_periods=1 → 可Plus试用1个月
      percentage=50,  num_periods=2 → Plus 2个月半价
    只认 plus 这一条；go 试用不单独占状态，避免「可领Plus」筛进 Go 号。
    没有 plus 活动才是普通 Free。
    """
    promo = promo if isinstance(promo, dict) else {}
    if acct.get("is_deactivated", False):
        return {"status": "banned", "label": "已封号"}
    plan = acct.get("plan_type", "free")
    if plan == "plus" or ent.get("has_active_subscription", False):
        return {"status": "plus_active", "label": "Plus生效中"}

    plus = promo.get("plus") if isinstance(promo.get("plus"), dict) else {}
    meta = plus.get("metadata") if isinstance(plus.get("metadata"), dict) else {}
    if not plus and not meta:
        return {"status": "free", "label": "Free"}

    discount = meta.get("discount") if isinstance(meta.get("discount"), dict) else {}
    duration = meta.get("duration") if isinstance(meta.get("duration"), dict) else {}
    try:
        pct = int(discount.get("percentage"))
    except (TypeError, ValueError):
        pct = None
    try:
        periods = int(duration.get("num_periods") or 1)
    except (TypeError, ValueError):
        periods = 1
    if periods < 1:
        periods = 1
    period = str(duration.get("period") or "month")
    unit = "个月" if period == "month" else ""

    offer_id = str(plus.get("id") or "")
    if pct == 50:
        label = f"Plus {periods}{unit}半价" if unit else "Plus半价"
        status = "plus_half"
    elif pct == 100 or "free" in offer_id:
        label = f"可Plus试用{periods}{unit}" if unit else "可领Plus试用"
        status = "plus_eligible"
    elif pct is not None:
        label = f"Plus {periods}{unit}{pct}%折" if unit else f"Plus {pct}%折"
        status = "plus_half"
    else:
        label = "可领Plus试用"
        status = "plus_eligible"
    return {
        "status": status,
        "label": label,
        "offer_id": offer_id,
        "discount_pct": pct,
        "periods": periods,
    }


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from http_client import create_http_session

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()
    preproxy = (req.preproxy or "").strip() or None
    pool_text = (req.proxy_pool or "").strip()
    proxy_list = [p.strip() for p in pool_text.splitlines() if p.strip()] if pool_text else ([proxy] if proxy else [])
    sessions: dict = {}
    def _sess_for(px):
        key = px or "__direct__"
        if key not in sessions:
            box = {}
            def _build():
                try:
                    box["sess"] = create_http_session(proxy=px or None, preproxy=preproxy, impersonate="chrome110")
                except Exception as e:
                    box["err"] = e
            w = threading.Thread(target=_build, daemon=True)
            w.start()
            w.join(10)
            if "sess" in box:
                sessions[key] = box["sess"]
            elif "err" in box:
                raise HTTPException(500, "创建 HTTP 会话失败: " + str(box["err"]))
            else:
                raise HTTPException(500, "代理建连超时（10秒无响应），代理可能已挂死，请检查代理池或改用直连")
        return sessions[key]

    note = ""

    def _check(access_token: str, account_id: str = "", device_id: str = "", sess=None):
        """打一次检测请求。

        ⚠️ 这里**不再自动降级直连**。原来的行为是：代理第一次报错就永久切直连，
        后面所有号都用主人的真实 IP 去打 chatgpt.com 的账号接口，而提示只是
        结果末尾一句小字。2026-08-10 实测踩到：主人改了代理池密码，这页却还在
        用 localStorage 里的旧代理 → curl:(97) 鉴权被拒 → 静默直连。
        检测失败重试一次就好，不值得拿真实 IP 换。

        请求头按 chatgpt.com 前端真实发的补齐（Origin/Referer/ChatGPT-Account-ID/
        OAI-Device-Id）。以前只发 Authorization，缺 Origin/Referer 属于典型的
        非浏览器特征，容易被风控挑出来；account_id 从 access_token 的 JWT 里解，
        不额外请求。
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            return sess.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # 把 curl 的错误码带出来：(97)=SOCKS5 鉴权被拒，(7)=连不上，
                # 笼统一句「代理连不通」会让人以为是网络抖动，其实是密码/配额问题。
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "代理认证被拒（SOCKS5 (97)）—— 检查代理账号密码/配额是否已变更"
                elif "(7)" in msg:
                    note = "代理连不上（curl (7)）—— 检查代理地址端口是否可达"
                else:
                    note = f"代理请求失败（{type(e).__name__}）—— 已保持代理，未改直连"
                log.warning(f"[check_plus] {note}: {msg[:140]}")
            raise

    results = {}
    log.info(f"[check_plus] 开始检测 {len(req.emails)} 个号（代理: {proxy or '直连'}）")
    for i, email in enumerate(req.emails):
        log.info(f"[check_plus] 正在检测: {email}")
        cred = db.get_registered(email, source=req.source)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        # account_id 直接从 AT 的 JWT payload 解（实测 12/12 都带），不发额外请求。
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # device_id 库里普遍是空的（注册时没落盘），按邮箱派生一个稳定 UUID：
        # 同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        try:
            px = proxy_list[i % len(proxy_list)] if proxy_list else None
            resp = _check(at, account_id, device_id, sess=_sess_for(px))
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code in (401, 403):
            # 401/403 的**响应体必须看**。以前这里只看状态码就贴「凭证失效」，
            # 结果是封号号 100% 显示成凭证失效：账号被封时 access_token 会被一起
            # 吊销 → 请求在这里就 401 了 → 永远走不到下面 200 分支的 is_deactivated
            # 判据。2026-08-10 实测某个被封号：JWT exp 还有 239 小时、
            # 13:53 检测还是 plus_eligible，之后被封 → 同一个 token 直接 401。
            #
            # 未过期却失效 = 被吊销，而 OpenAI 会在响应体里写明原因，
            # 所以按响应体内容区分「封号」和「单纯的凭证过期/轮换」。
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = {"status": "banned", "label": "封号"}
                log.info(f"[check_plus] {email} 判定封号 (HTTP {resp.status_code}): {body[:200]}")
                continue
            if resp.status_code == 401:
                results[email] = {"status": "token_invalid", "label": "凭证失效"}
                # 日志留原文：万一是没覆盖到的封号措辞，主人看一眼就能告诉我补进去。
                log.info(f"[check_plus] {email} 401 响应体: {body[:200]}")
                continue
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            log.info(f"[check_plus] {email} 403 响应体: {body[:200]}")
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        accts = data.get("accounts", {})
        if not accts:
            results[email] = {"status": "error", "label": "无账户数据"}
            continue
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})
        results[email] = classify_plus_offer(acct, ent, promo)

    for s in sessions.values():
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

    checked_at = time.time()
    for email, info in results.items():
        # not_found / no_at / error 不写库：它们不是「检测结论」而是**没检测成**
        # （号不在库里、没凭证、代理挂了），写进去号就从 unchecked 过滤器里消失，
        # 看着像已经检测过。修好后重点一次即可。
        #
        # token_invalid **要写**（2026-08-10 改）。原先不写的理由是「凭证问题不是
        # 账号问题，换新凭证后该重查」，但实测下来：AT 没过期却 401 = 被吊销，
        # 大概率就是封号（2026-08-10 实测那个号即是）。不写库的实际后果是这号
        # 一直挂着上次的 plus_eligible，列表上显示「可领Plus试用」——比标成凭证
        # 失效误导得多。写库后 unchecked 过滤器会跳过它，正是想要的：它已经有结论了。
        if info["status"] not in ("not_found", "no_at", "error"):
            # 同步回前端：刚检测完无需再手动刷新，列表中的「查询时间」立即更新。
            info["checked_at"] = checked_at
            db.update_plus_check(email, {**info, "checked_at": checked_at},
                                 source=req.source)

    log.info(f"[check_plus] 检测完成: {len(results)} 个号")
    return {"ok": True, "results": results, "note": note}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    preproxy: str = ""           # 可选前置 SOCKS，批量各 worker 共用
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）
    # 批量页已放开关且**默认开**（主人要求每个号都绑）。
    # 这里的 default 仍保持 False —— 它只在「前端没传这个字段」时生效，
    # 是给旧前端缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不要
    # 替调用方做一个不可逆的决定。真实默认值由 AutoLoop.vue 的 autoWant2fa 决定。
    want_2fa: bool = False
    # 批量并发时启用 SMS 并行接码（每 worker 独立租号独立等码，
    # 与补 refresh 链路的 sms_parallel 语义一致）。
    # default=False 同上：只在字段漏传时兜底，真实值由前端 concurrency>1 时传 true。
    sms_parallel: bool = False


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 阻塞拿消息，但每 30s 心跳
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    # index.html 会引用带 hash 的前端资源。发布新版后若浏览器继续拿旧入口，
    # 动态路由会去请求已删除的旧 chunk，页面看似“自己坏掉”。入口绝不缓存；
    # 具体 assets 仍按文件 hash 正常缓存。
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8768, reload=False)
