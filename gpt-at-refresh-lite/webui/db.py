"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import json
import random
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(__file__).resolve().parent / "webui.db"

_lock = threading.Lock()  # SQLite 写入串行化


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- 中转取码 URL（icloud 类用，其余留空）
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- 邮箱类型，对应 mail_providers 注册表的 kind
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- idx_outlook_kind 不在这里建：老库此刻还没有 kind 列，
        -- 建索引会当场报错。放到下面补完列之后再建。

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        -- 「导入账号」独立表：与 registered 完全同构，物理隔离。
        -- 别处注册的号（邮箱----密码----2FA）导进来，复用注册结果页的全部
        -- 功能（检测/补refresh/刷新AT/导出），但与本站注册的号互不可见、
        -- 互不影响（删除/清空/统计都只作用于各自表）。
        CREATE TABLE IF NOT EXISTS imported (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- banned / network / account / unknown
        );

        CREATE TABLE IF NOT EXISTS sms_phone_pool (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT NOT NULL UNIQUE,   -- E.164，带 + 前缀
            recv_url        TEXT NOT NULL,          -- 该号绑定的取码（接收）URL
            enabled         INTEGER NOT NULL DEFAULT 1,
            use_count       INTEGER NOT NULL DEFAULT 0,
            success_count   INTEGER NOT NULL DEFAULT 0,
            fail_count      INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT DEFAULT '',
            disabled_reason TEXT DEFAULT '',
            disabled_at     REAL DEFAULT 0,
            in_use          INTEGER NOT NULL DEFAULT 0,
            last_used_at    REAL DEFAULT 0,         -- 最近一次被取号时间（号段避让用）
            created_at      REAL
        );

        CREATE INDEX IF NOT EXISTS idx_sms_pool_enabled
            ON sms_phone_pool(enabled, in_use, use_count);
    """)
    con.commit()

    # 老 DB migrate：号码池 last_used_at（随机选号的号段避让用）
    cur = con.execute("PRAGMA table_info(sms_phone_pool)")
    pool_cols = {r[1] for r in cur.fetchall()}
    if "last_used_at" not in pool_cols:
        con.execute("ALTER TABLE sms_phone_pool ADD COLUMN last_used_at REAL DEFAULT 0")
        con.commit()

    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # 老 DB migrate：号池多邮箱混放（kind / relay_url 在后期才加）
    # 存量行全部是 outlook 时代导进去的，DEFAULT 'outlook' 正好把它们
    # 归位，不需要额外 UPDATE。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    # 索引建在补列之后，否则老库上 CREATE INDEX 会因为没有 kind 列而失败
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.commit()

    # 老 DB migrate：registered 的 2FA 两列（totp_secret / totp_factor_id）后期才加。
    # secret 一次性下发、服务端取不回，务必单独补列持久化。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()
    con.close()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数唯一时才行，Outlook/Gmail 都是 4 段会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(text: str, kind: str = "") -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?)",
                    (r["email"], password, client_id, refresh, relay, row_kind, now),
                )
                inserted += 1
            elif (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            ):
                # 凭证或类型变了 → 覆盖并重置为可用
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, status='available', imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (refresh, password, client_id, relay, row_kind, now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def _email_search_condition(search: str) -> tuple[str, list]:
    """返回邮箱搜索的 SQL 条件，保留邮箱中 _ / % 的字面含义。"""
    keyword = (search or "").strip().lower()
    if not keyword:
        return "", []
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "lower(email) LIKE ? ESCAPE '\\'", [f"%{escaped}%"]


def _accounts_where(status: str = "", kind: str = "", search: str = "") -> tuple[str, list]:
    """构造号池列表/计数共用的筛选条件。

    搜索限定为邮箱的大小写不敏感部分匹配，既不会把密码、取件链接等敏感字段
    暴露为检索入口，也能让分页总数与当前表格保持一致。
    """
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    search_condition, search_args = _email_search_condition(search)
    if search_condition:
        where.append(search_condition)
        args.extend(search_args)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def count_accounts(status: str = "", kind: str = "", search: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = _accounts_where(status=status, kind=kind, search=search)
    sql += where
    total = con.execute(sql, args).fetchone()[0]
    con.close()
    return total


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = "", search: str = ""
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = _accounts_where(status=status, kind=kind, search=search)
    sql += where
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            if k:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k,),
                )
            else:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "ORDER BY imported_at ASC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def _cred_table(source: str = "registered") -> str:
    """凭证表路由：registered（本站注册）/ imported（外部导入）。

    两个表结构完全同构，全部凭证类函数通过本函数切表；
    source 传错/不传一律落回 registered，老调用零影响。
    """
    return "imported" if (source or "").strip().lower() == "imported" else "registered"


def save_registered(d: dict, source: str = "registered") -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    source='imported' 时写入 imported 表（补 refresh 等功能的写回按数据源路由）。
    """
    table = _cred_table(source)
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id",
    }}
    with _lock:
        con = _conn()
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    token 三件套正相反：每轮跑都是全新的，旧的可能已失效，照常整列覆盖。
        # totp_secret 和密码同理，甚至更严：secret【一次性下发、服务端取不回】，
        #    丢了 = 该号 2FA 永久锁死。重跑同邮箱（已绑过 2FA）时这一轮不会再绑，
        #    d 里没有 secret —— 绝不能拿空值把库里已存的 secret 冲没。
        #    与密码合成一次 SELECT，顺带把两列旧值一起兜住。
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if not password or not totp_secret:
            row = con.execute(
                f"SELECT password, totp_secret, totp_factor_id FROM {table} WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
        con.execute(
            f"INSERT OR REPLACE INTO {table} "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()
        con.close()


def save_password_early(email: str, password: str) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(email: str, secret: str, factor_id: str = "") -> None:
    """2FA secret 一从 enroll 响应拿到就落盘，不等整个注册流程跑完。

    由 registrar 的 _bind_2fa_hook 触发（钩子在「拿到 session」和「Codex 授权 /
    绑手机号接码」之间调 bind_totp_2fa_inline，成功即拿到 secret）。

    ⚠️ 早落盘的理由和 save_password_early 一模一样、甚至更急：
       secret 绑成之后，流程还要走 Codex 授权 + add-phone 接码（可能好几分钟），
       这段时间 secret 只活在 registrar 内存的 _tfa_box 里。接码太久用户一关进程，
       secret 就永久蒸发 —— 而它【一次性下发、服务端取不回】，丢了该号 2FA 锁死。
       所以一拿到手就先写库，后面接码怎么中断都不怕。

    只写 totp 两列；token / 密码留给后续 save_registered 用同一 email 主键补齐。
    ⚠️ 行已存在时**只 UPDATE totp 两列**，绝不动已有的密码 / token
       —— 重跑老号时不能把人家已存的凭证清空。
    """
    email = (email or "").strip().lower()
    secret = (secret or "").strip()
    if not email or not secret:
        return
    factor_id = (factor_id or "").strip()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, '', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=excluded.totp_factor_id",
            (
                email,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None,
                             source: str = "registered") -> bool:
    """手动修正某个账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 外部账号走导入功能）。
    """
    table = _cred_table(source)
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute(
            f"SELECT email FROM {table} WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


def update_plus_check(email: str, plus_info: dict, source: str = "registered") -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    table = _cred_table(source)
    email = email.lower()
    con = _conn()
    cur = con.execute(f"SELECT extra_json FROM {table} WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        con.close()
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            f"UPDATE {table} SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()
    con.close()


def _registered_where(filt: str, search: str = "") -> tuple[str, list]:
    conditions = []
    if filt == "has_rt":
        conditions.append("length(refresh_token) > 0")
    elif filt == "no_rt":
        conditions.append("coalesce(length(refresh_token),0) = 0")
    elif filt == "no_at":
        conditions.append("coalesce(length(access_token),0) = 0")
    elif filt == "unchecked":
        conditions.append("(extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')")
    elif filt == "free":
        conditions.append("extra_json LIKE '%\"free\"%'")
    elif filt == "plus":
        # 只收试用（plus_eligible）和已开通（plus_active）。
        # 半价是 plus_half，不能用 '%"plus_%' 一把捞——会把半价混进「可领Plus」。
        conditions.append("(extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')")
    elif filt == "plus_half":
        conditions.append("extra_json LIKE '%\"plus_half\"%'")
    elif filt == "banned":
        conditions.append("extra_json LIKE '%\"banned\"%'")
    elif filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        conditions.append("extra_json LIKE '%\"token_invalid\"%'")
    search_condition, args = _email_search_condition(search)
    if search_condition:
        conditions.append(search_condition)
    return ("WHERE " + " AND ".join(conditions)) if conditions else "", args


def count_registered(filter_rt: str = "all", search: str = "", source: str = "registered") -> int:
    table = _cred_table(source)
    con = _conn()
    where, args = _registered_where(filter_rt, search)
    cur = con.execute(f"SELECT COUNT(*) FROM {table} {where}", args)
    total = cur.fetchone()[0]
    con.close()
    return total


def list_registered(
    limit: int = 20, offset: int = 0, filter_rt: str = "all", search: str = "",
    source: str = "registered",
) -> list[dict]:
    table = _cred_table(source)
    con = _conn()
    where, args = _registered_where(filter_rt, search)
    cur = con.execute(
        f"SELECT email, password, totp_secret, "
        f"length(access_token) AS at_len, length(session_token) AS st_len, "
        f"length(refresh_token) AS rt_len, extra_json, created_at FROM {table} "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        [*args, limit, offset],
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        plus = None
        extra = {}
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"]) or {}
            except Exception:
                extra = {}
            plus = extra.get("plus_check")
        d["plus_check"] = plus
        # 注册出口/画像：注册落库时由 registrar 写进 extra_json。
        # exit_ip 是 Cloudflare trace 返回的真实出口，proxy 只是代理池连接串，
        # 即使旧记录还存着也不能用于页面展示。
        d["exit_ip"] = (extra.get("exit_ip") or "").strip()
        d["country"] = (extra.get("country") or "").strip()
        d["fingerprint"] = (extra.get("fingerprint") or "").strip()
        d.pop("extra_json", None)
        rows.append(d)
    con.close()
    return rows


def list_registered_full(limit: int = 5000, source: str = "registered") -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式，外加 relay_url。

    ⚠️ relay_url（中转取件链接）**不在 registered/imported 表里**，它跟着号池
       那一行走（outlook_accounts.relay_url，icloud_relay 这类号一号一条 token）。
       导出格式「邮箱----密码----2FA----取件url」要用它，所以这里 LEFT JOIN 带出来。
       导入的号不在号池里，relay_url 自然是空串（导出时分隔符保留、字段留空）。
    """
    table = _cred_table(source)
    con = _conn()
    cur = con.execute(
        f"SELECT r.*, a.relay_url AS relay_url "
        f"FROM {table} r LEFT JOIN outlook_accounts a ON a.email = r.email "
        f"ORDER BY r.created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        d.pop("extra_json", None)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str], source: str = "registered") -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    - relay_url 从号池表 LEFT JOIN 带出（原因见 list_registered_full）。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    table = _cred_table(source)
    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, a.relay_url AS relay_url "
            f"FROM {table} r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            d.pop("extra_json", None)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str, source: str = "registered") -> Optional[dict]:
    con = _conn()
    cur = con.execute(
        f"SELECT * FROM {_cred_table(source)} WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


def delete_registered(email: str, source: str = "registered") -> bool:
    with _lock:
        con = _conn()
        rc = con.execute(
            f"DELETE FROM {_cred_table(source)} WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str], source: str = "registered") -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM {_cred_table(source)} WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered(source: str = "registered") -> int:
    with _lock:
        con = _conn()
        rc = con.execute(f"DELETE FROM {_cred_table(source)}")
        con.commit()
        return rc.rowcount


def upsert_imported(email: str, password: str, totp_secret: str) -> str:
    """导入账号专用 upsert（imported 表）。

    与 save_registered 的「token 整列覆盖」语义不同：导入只带 邮箱/密码/2FA
    三件套，重复导入同一邮箱必须**保留已补出的 token 和原导入时间**，绝不能
    把补 refresh 拿到的凭证冲没。
    返回 'inserted'（新增）或 'updated'（更新已有行的密码/2FA）。
    抛 ValueError = TOTP 非法（与手填凭证同一套校验）。
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("邮箱为空")
    totp_secret = (totp_secret or "").strip()
    if totp_secret:
        totp_secret = normalize_totp_secret(totp_secret)
    with _lock:
        con = _conn()
        now = time.time()
        row = con.execute(
            "SELECT created_at FROM imported WHERE email=?", (email,)
        ).fetchone()
        if row:
            con.execute(
                "UPDATE imported SET password=?, totp_secret=? WHERE email=?",
                (password or "", totp_secret, email),
            )
            con.commit()
            con.close()
            return "updated"
        con.execute(
            "INSERT INTO imported (email, password, access_token, session_token, "
            "refresh_token, id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, '', NULL, ?)",
            (email, password or "", totp_secret, now),
        )
        con.commit()
        con.close()
        return "inserted"


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50, category: str = "") -> list[dict]:
    con = _conn()
    category = (category or "").strip()
    if category and category != "all":
        cur = con.execute(
            "SELECT * FROM runs WHERE error_category=? ORDER BY started_at DESC LIMIT ?",
            (category, limit),
        )
    else:
        cur = con.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def get_runs_status(run_ids: list[str]) -> list[dict]:
    """按 run_id 批量查状态（补 refresh 横幅轮询用）。

    只取横幅需要的列；run_ids 里已经消失/被清库的 id 自然不返回，
    前端按「返回数 < 请求数 = 全部终态」无法成立的原则，以返回条目为准。
    """
    ids = [str(i or "").strip() for i in (run_ids or []) if str(i or "").strip()]
    if not ids:
        return []
    con = _conn()
    marks = ",".join("?" for _ in ids)
    cur = con.execute(
        f"SELECT run_id, email, status, error, error_category "
        f"FROM runs WHERE run_id IN ({marks})",
        tuple(ids),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
        # 独立手机号池
        "sms_pool_auto_disable":   get_setting("sms_pool_auto_disable", "1"),
        "sms_pool_fail_threshold": get_setting("sms_pool_fail_threshold", "2"),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms", "custom_pool"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price", "sms_fixed_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
        "sms_pool_fail_threshold",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist",
                "sms_pool_auto_disable"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
        # 独立手机号池
        "sms_pool_auto_disable":   get_setting("sms_pool_auto_disable", "1") in ("1", "true"),
        "sms_pool_fail_threshold": get_setting("sms_pool_fail_threshold", "2"),
    }


# ──────────────────────── 独立手机号池 ────────────────────────

_PHONE_NON_DIGIT_RE = re.compile(r"\D+")


# 域名开头、无协议头的取码 URL：supabase.co/functions/...、smscloud.sbs/api/... 等
_URL_NO_SCHEME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}([/?#].*)?$")


def _pool_normalize_phone(raw) -> str:
    """号码规范化成 E.164（+ 前缀、纯数字）；解析不出返回空串。"""
    digits = _PHONE_NON_DIGIT_RE.sub("", str(raw or ""))
    if len(digits) < 7:
        return ""
    return f"+{digits}"


def _pool_normalize_url(raw) -> str:
    """取码 URL 规范化。允许不带协议头，自动补 https://。

    运营商给的取码地址经常是「域名/路径?key=...」形式（浏览器打开会自动补协议），
    这里对齐这个习惯：长得像域名的直接当 https 处理。
    """
    u = str(raw or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if _URL_NO_SCHEME_RE.match(u):
        return "https://" + u
    return ""


def _pool_prefix(phone) -> str:
    """号段前缀：连坐风控的粒度。

    美国号 +1 NPA NXX：19048596977 → '904859'（州区号 904 + 局号 859），
    同前缀基本来自同一虚拟号池，OpenAI 风控一个往往连坐一片。
    其它国家/长度取前 6 位数字兜底。
    """
    d = _PHONE_NON_DIGIT_RE.sub("", str(phone or ""))
    if len(d) == 11 and d.startswith("1"):
        return d[1:7]
    return d[:6]


def pool_import(text: str) -> dict:
    """导入手机号池文本，重复号码只更新其取码 URL（不算新号）。

    支持三种行格式（对齐 GuJumpgate 接码池）：
        +19429996028----https://sms.example/api/recv/xxx
        19429996028|https://sms.example/api/recv/xxx
        两行式：号码一行、URL 一行（URL 必须以 http(s):// 开头才认）
    """
    lines = [ln.strip() for ln in str(text or "").replace("\r", "").split("\n")]
    errors: list[dict] = []
    entries: list[tuple[str, str]] = []

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        if not line or line.startswith("#"):
            continue
        sep_idx = line.find("----")
        pipe_idx = line.find("|")
        phone_part, url_part = "", ""
        if sep_idx > 0:
            phone_part, url_part = line[:sep_idx], line[sep_idx + 4:]
        elif pipe_idx > 0:
            phone_part, url_part = line[:pipe_idx], line[pipe_idx + 1:]
        else:
            phone_part = line
            j = i
            while j < n and not lines[j]:
                j += 1
            if j < n and (
                lines[j].startswith("http://")
                or lines[j].startswith("https://")
                or _URL_NO_SCHEME_RE.match(lines[j])
            ):
                url_part = lines[j]
                i = j + 1
        phone = _pool_normalize_phone(phone_part)
        url = _pool_normalize_url(url_part)
        if not phone or not url:
            errors.append({"line": i, "text": line[:80], "error": "需要「号码 + 取码URL（域名或 http/https 链接）」"})
            continue
        entries.append((phone, url))

    inserted = updated = 0
    seen: set[str] = set()
    with _lock:
        con = _conn()
        try:
            for phone, url in entries:
                if phone in seen:
                    # 同一批内重复：以最后一条为准
                    con.execute("UPDATE sms_phone_pool SET recv_url=? WHERE phone=?", (url, phone))
                    updated += 1
                    continue
                seen.add(phone)
                if con.execute("SELECT 1 FROM sms_phone_pool WHERE phone=?", (phone,)).fetchone():
                    con.execute("UPDATE sms_phone_pool SET recv_url=? WHERE phone=?", (url, phone))
                    updated += 1
                else:
                    con.execute(
                        "INSERT INTO sms_phone_pool (phone, recv_url, created_at) VALUES (?, ?, ?)",
                        (phone, url, time.time()),
                    )
                    inserted += 1
            con.commit()
        finally:
            con.close()
    return {
        "parsed": len(entries) + len(errors),
        "inserted": inserted,
        "updated": updated,
        "skipped": len(errors),
        "errors": errors,
    }


def pool_list(search: str = "", filter_mode: str = "all", limit: int = 500) -> list[dict]:
    where: list[str] = []
    args: list = []
    f = (filter_mode or "all").strip().lower()
    if f == "enabled":
        where.append("enabled=1")
    elif f == "disabled":
        where.append("enabled=0")
    elif f == "unused":
        where.append("use_count=0")
    elif f == "used":
        where.append("use_count>0")
    elif f == "in_use":
        where.append("in_use=1")
    elif f == "error":
        where.append("last_error!=''")
    s = str(search or "").strip().lower()
    if s:
        where.append(
            "(LOWER(phone) LIKE ? OR LOWER(recv_url) LIKE ? "
            "OR LOWER(last_error) LIKE ? OR LOWER(disabled_reason) LIKE ?)"
        )
        like = f"%{s}%"
        args.extend([like, like, like, like])
    sql = "SELECT * FROM sms_phone_pool"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(max(1, min(int(limit or 500), 5000)))
    with _lock:
        con = _conn()
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()


def pool_stats() -> dict:
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END)  AS enabled,
                          SUM(CASE WHEN enabled=0 THEN 1 ELSE 0 END)  AS disabled,
                          SUM(CASE WHEN in_use=1 THEN 1 ELSE 0 END)   AS in_use,
                          SUM(CASE WHEN enabled=1 AND in_use=0 THEN 1 ELSE 0 END) AS idle,
                          COALESCE(SUM(use_count), 0)     AS total_use,
                          COALESCE(SUM(success_count), 0) AS total_success,
                          COALESCE(SUM(fail_count), 0)    AS total_fail
                   FROM sms_phone_pool"""
            ).fetchone()
            d = dict(row) if row else {}
            return {k: int(v or 0) for k, v in d.items()}
        finally:
            con.close()


def pool_claim(max_use: int = 3, prefix_cooldown: int = 1800) -> Optional[dict]:
    """随机取一个可用号（enabled / 未占用 / 未达使用上限）并标记 in_use。

    选号策略（降低同号段连坐风控）：
      1. 优先在「冷却期 prefix_cooldown 秒内没用过其号段」的号里随机挑；
         号段前缀：美国号取 +1 NPA+NXX 六位（19048596977 → 904859），其它取前 6 位；
      2. 正在被其它 worker 占用的号段一并避让（并发不撞号段）；
      3. 可用号段全部在冷却期/占用中时，选「最久没用过的号段」轮转，
         保证尽量不连续两次取同一号段；
      4. 同号段内部再随机挑具体号码。
    """
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                """SELECT * FROM sms_phone_pool
                   WHERE enabled=1 AND in_use=0 AND use_count < ?
                   ORDER BY RANDOM()
                   LIMIT 2000""",
                (max(1, int(max_use or 3)),),
            ).fetchall()
            if not rows:
                return None
            now = time.time()
            prefix_ts = {}
            recent = con.execute(
                "SELECT phone, last_used_at FROM sms_phone_pool WHERE last_used_at >= ?",
                (now - max(0, int(prefix_cooldown or 0)),),
            ).fetchall()
            for r in recent:
                p = _pool_prefix(r["phone"])
                ts = r["last_used_at"] or 0
                if ts > prefix_ts.get(p, 0):
                    prefix_ts[p] = ts
            # 正在被占用的号段视为「刚刚用过」，并发 worker 之间也错开号段
            in_use_rows = con.execute(
                "SELECT phone FROM sms_phone_pool WHERE in_use=1"
            ).fetchall()
            for r in in_use_rows:
                p = _pool_prefix(r["phone"])
                if now > prefix_ts.get(p, 0):
                    prefix_ts[p] = now
            fresh = [r for r in rows if _pool_prefix(r["phone"]) not in prefix_ts]
            if fresh:
                pick = random.choice(fresh)
            else:
                # 可用号段全在冷却期/占用中：挑最久没用过的号段轮转
                oldest = min(prefix_ts.get(_pool_prefix(r["phone"]), 0) for r in rows)
                pick = random.choice(
                    [r for r in rows if prefix_ts.get(_pool_prefix(r["phone"]), 0) == oldest]
                )
            con.execute(
                "UPDATE sms_phone_pool SET in_use=1, last_used_at=? WHERE id=?",
                (now, pick["id"]),
            )
            con.commit()
            return dict(pick)
        finally:
            con.close()


def pool_release(phone_id: int) -> None:
    with _lock:
        con = _conn()
        try:
            con.execute("UPDATE sms_phone_pool SET in_use=0 WHERE id=?", (int(phone_id),))
            con.commit()
        finally:
            con.close()


def pool_mark_success(phone_id: int) -> None:
    """用码成功：使用/成功次数 +1，清空连续失败，释放占用，刷新取号时间。"""
    with _lock:
        con = _conn()
        try:
            con.execute(
                """UPDATE sms_phone_pool SET
                       use_count=use_count+1, success_count=success_count+1,
                       fail_count=0, last_error='', in_use=0, last_used_at=?
                   WHERE id=?""",
                (time.time(), int(phone_id)),
            )
            con.commit()
        finally:
            con.close()


def pool_mark_fail(phone_id: int, reason: str = "", *, release: bool = True,
                   auto_disable: bool = True, fail_threshold: int = 2) -> None:
    """记一次失败；连续失败达到阈值时自动禁用（reason 写进 disabled_reason）。"""
    with _lock:
        con = _conn()
        try:
            con.execute(
                """UPDATE sms_phone_pool SET
                       fail_count=fail_count+1, last_error=?,
                       in_use=CASE WHEN ? THEN 0 ELSE in_use END
                   WHERE id=?""",
                (str(reason or "")[:200], 1 if release else 0, int(phone_id)),
            )
            if auto_disable:
                con.execute(
                    """UPDATE sms_phone_pool SET
                           enabled=0, disabled_reason=?, disabled_at=?
                       WHERE id=? AND enabled=1 AND fail_count >= ?""",
                    (f"连续失败 {max(1, int(fail_threshold))} 次自动禁用: {str(reason or '')[:90]}",
                     time.time(), int(phone_id), max(1, int(fail_threshold or 2))),
                )
            con.commit()
        finally:
            con.close()


def pool_increment_usage(phone_id: int) -> None:
    with _lock:
        con = _conn()
        try:
            con.execute(
                "UPDATE sms_phone_pool SET use_count=use_count+1 WHERE id=?", (int(phone_id),)
            )
            con.commit()
        finally:
            con.close()


def pool_reset_usage(phone_id: int) -> bool:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                "UPDATE sms_phone_pool SET use_count=0, success_count=0, fail_count=0, last_error='' WHERE id=?",
                (int(phone_id),),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def pool_clear_usage() -> int:
    """清空全部使用/失败记录（号码与禁用状态保留）。"""
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                "UPDATE sms_phone_pool SET use_count=0, success_count=0, fail_count=0, last_error=''"
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def pool_set_enabled(phone_id: int, enabled: bool, reason: str = "") -> bool:
    with _lock:
        con = _conn()
        try:
            if enabled:
                cur = con.execute(
                    "UPDATE sms_phone_pool SET enabled=1, disabled_reason='', disabled_at=0, "
                    "fail_count=0, last_error='' WHERE id=?",
                    (int(phone_id),),
                )
            else:
                cur = con.execute(
                    "UPDATE sms_phone_pool SET enabled=0, disabled_reason=?, disabled_at=? WHERE id=?",
                    (str(reason or "手动禁用")[:100], time.time(), int(phone_id)),
                )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def pool_enable_all() -> int:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                "UPDATE sms_phone_pool SET enabled=1, disabled_reason='', disabled_at=0, fail_count=0 "
                "WHERE enabled=0"
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def pool_edit_url(phone_id: int, recv_url: str) -> bool:
    u = _pool_normalize_url(recv_url)
    if not u:
        raise ValueError("取码 URL 必须以 http:// 或 https:// 开头")
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                "UPDATE sms_phone_pool SET recv_url=? WHERE id=?", (u, int(phone_id))
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def pool_delete(phone_id: int) -> bool:
    with _lock:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM sms_phone_pool WHERE id=?", (int(phone_id),))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def pool_delete_all() -> int:
    with _lock:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM sms_phone_pool")
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def pool_reset_in_use() -> int:
    """进程启动时调用：清掉上次强退留下的 in_use 占用。"""
    with _lock:
        con = _conn()
        try:
            cur = con.execute("UPDATE sms_phone_pool SET in_use=0 WHERE in_use=1")
            con.commit()
            return cur.rowcount
        finally:
            con.close()


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# 模块加载时自动建表
init_db()
