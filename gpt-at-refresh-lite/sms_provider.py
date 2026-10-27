"""SMS 接码 provider 抽象 + SmsBower 实现。

设计参考：asz798838958/GeniusFKoai 的 core/base_sms.py，但裁剪掉浏览器回调相关代码、
仅保留纯协议注册需要的两段流程：
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) 成功/失败       → provider.report_success / cancel / mark_code_failed

⚠️ 关键事实：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，**纯 SMS 路径目前只有
泰国（country_id=52）确认可用**。其它国家可能抽到 WhatsApp 号导致拿不到 SMS。
SmsBower 的 `auto_select_country=True` 会按价格 + 库存自动选号。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""
    activation_id: str
    phone_number: str          # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到 code 即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台可能据此结算/允许复用。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到 code 但 validate 失败 → 请求 resend。"""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 停止复用。"""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """注册 resend 钩子（SmsBower 长等待时回调业务侧重新触发 OTP）。"""
        return None


# ---------------------------------------------------------------------------
# 国家 ID → 中文名映射（sms-activate.org 协议系，SmsBower 共用）
# ---------------------------------------------------------------------------

SMS_COUNTRY_NAMES_CN: dict[str, str] = {
    "0": "俄罗斯", "1": "乌克兰", "2": "哈萨克斯坦", "3": "中国", "4": "菲律宾",
    "5": "缅甸", "6": "印度尼西亚", "7": "马来西亚", "8": "肯尼亚", "9": "坦桑尼亚",
    "10": "越南", "11": "吉尔吉斯斯坦", "12": "美国(虚拟)", "13": "以色列", "14": "香港",
    "15": "波兰", "16": "英国", "17": "马达加斯加", "18": "刚果(布)", "19": "尼日利亚",
    "20": "澳门", "21": "埃及", "22": "印度", "23": "爱尔兰", "24": "柬埔寨",
    "25": "老挝", "26": "海地", "27": "科特迪瓦", "28": "冈比亚", "29": "塞尔维亚",
    "30": "也门", "31": "南非", "32": "罗马尼亚", "33": "哥伦比亚", "34": "爱沙尼亚",
    "35": "阿塞拜疆", "36": "加拿大", "37": "摩洛哥", "38": "加纳", "39": "阿根廷",
    "40": "乌兹别克斯坦", "41": "喀麦隆", "42": "乍得", "43": "德国", "44": "立陶宛",
    "45": "克罗地亚", "46": "瑞典", "47": "伊拉克", "48": "荷兰", "49": "拉脱维亚",
    "50": "奥地利", "51": "白俄罗斯", "52": "泰国", "53": "沙特阿拉伯", "54": "墨西哥",
    "55": "台湾", "56": "西班牙", "57": "伊朗", "58": "阿尔及利亚", "59": "斯洛文尼亚",
    "60": "孟加拉国", "61": "塞内加尔", "62": "土耳其", "63": "捷克", "64": "斯里兰卡",
    "65": "秘鲁", "66": "巴基斯坦", "67": "新西兰", "68": "几内亚", "69": "马里",
    "70": "委内瑞拉", "71": "埃塞俄比亚", "72": "蒙古", "73": "巴西", "74": "阿富汗",
    "75": "乌干达", "76": "安哥拉", "77": "塞浦路斯", "78": "法国", "79": "巴布亚新几内亚",
    "80": "莫桑比克", "81": "尼泊尔", "82": "比利时", "83": "保加利亚", "84": "匈牙利",
    "85": "摩尔多瓦", "86": "意大利", "87": "巴拉圭", "88": "洪都拉斯", "89": "突尼斯",
    "90": "尼加拉瓜", "91": "东帝汶", "92": "玻利维亚", "93": "哥斯达黎加", "94": "危地马拉",
    "95": "阿联酋", "96": "津巴布韦", "97": "波多黎各", "98": "苏丹", "99": "多哥",
    "100": "科威特", "101": "萨尔瓦多", "102": "利比亚", "103": "牙买加", "104": "特立尼达和多巴哥",
    "105": "厄瓜多尔", "106": "斯威士兰", "107": "阿曼", "108": "波黑", "109": "多米尼加",
    "110": "叙利亚", "111": "卡塔尔", "112": "巴拿马", "113": "古巴", "114": "毛里塔尼亚",
    "115": "塞拉利昂", "116": "约旦", "117": "葡萄牙", "118": "巴巴多斯", "119": "布隆迪",
    "120": "贝宁", "121": "文莱", "122": "巴哈马", "123": "博茨瓦纳", "124": "伯利兹",
    "125": "中非", "126": "多米尼克", "127": "格林纳达", "128": "格鲁吉亚", "129": "希腊",
    "130": "几内亚比绍", "131": "圭亚那", "132": "冰岛", "133": "科摩罗", "134": "利比里亚",
    "135": "莱索托", "136": "马拉维", "137": "纳米比亚", "138": "尼日尔", "139": "卢旺达",
    "140": "斯洛伐克", "141": "苏里南", "142": "塔吉克斯坦", "143": "摩纳哥", "144": "巴林",
    "145": "留尼汪岛", "146": "赞比亚", "147": "亚美尼亚", "148": "索马里", "149": "刚果(金)",
    "150": "智利", "151": "布基纳法索", "152": "黎巴嫩", "153": "加蓬", "154": "阿尔巴尼亚",
    "155": "乌拉圭", "156": "毛里求斯", "157": "不丹", "158": "马尔代夫", "159": "瓜德罗普岛",
    "160": "土库曼斯坦", "161": "法属圭亚那", "162": "芬兰", "163": "圣卢西亚", "164": "卢森堡",
    "165": "圣文森特", "166": "赤道几内亚", "167": "吉布提", "168": "安提瓜和巴布达", "169": "开曼群岛",
    "170": "黑山", "171": "丹麦", "172": "瑞士", "173": "挪威", "174": "澳大利亚",
    "175": "厄立特里亚", "176": "南苏丹", "177": "圣多美", "178": "阿鲁巴岛", "179": "蒙特塞拉特",
    "180": "安圭拉岛", "181": "北马其顿", "182": "塞舌尔", "183": "新喀里多尼亚", "184": "佛得角",
    "185": "美国(实体)", "186": "巴勒斯坦", "187": "美国", "188": "中国", "189": "韩国",
    "190": "科特迪瓦", "191": "日本",
}


def country_label(country_id) -> str:
    """返回 '52 泰国' 这样的展示标签。"""
    cid = str(country_id or "").strip()
    name = SMS_COUNTRY_NAMES_CN.get(cid, "")
    return f"{cid} {name}".strip()


# ---------------------------------------------------------------------------
# SmsBower / SMSBower —— 共享 API 协议
# ---------------------------------------------------------------------------

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # Thailand —— OpenAI 走 SMS 的稳定国家
SMS_PHONE_LIFETIME = 20 * 60  # 号码租用窗口（秒）
_SMS_CACHE_LOCK = threading.Lock()
_SMS_VERIFY_LOCK = threading.RLock()
_SMS_CACHE: Optional[dict] = None  # 跨线程共享的号码复用缓存

# OpenAI 走纯 SMS 的国家白名单（截至 2025-2026 实测；其它国家会抽到 WhatsApp 号）
OPENAI_SMS_COUNTRIES = {"52"}  # Thailand only


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _project_cache_dir() -> Path:
    root = Path(__file__).resolve().parent
    cache = root / "data"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _smsbower_cache_file() -> Path:
    return _project_cache_dir() / ".smsbower_phone_cache.json"


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(
            f"{activation_id}:{code}".encode("utf-8")
        ).hexdigest(),
    }


class SmsBowerProvider(BaseSmsProvider):
    """sms-activate 协议系 provider（SmsBower / HeroSMS 共用）。"""

    DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    auto_report_success_on_code = False  # 等业务侧确认才报成功（便于号码复用）

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        fixed_price: float = -1,
        proxy: Optional[str] = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
        parallel: bool = False,
    ):
        self.api_key = str(api_key or "").strip()
        # 并行模式（补 refresh 多并发）：每个任务独立租号独立等码，
        # 完全绕开全局号码复用缓存 —— getStatus 按 activation_id 查询，
        # 平台侧天然隔离，不会串号。代价是省掉"一号绑 3 个号"的复用省钱。
        self.parallel = bool(parallel)
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.fixed_price = float(fixed_price or -1)
        self._proxy = (proxy or "").strip() or None
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self._resend_callback: Optional[Callable[[], None]] = None
        self.last_code_result: Optional[dict] = None
        self.current_activation: Optional[SmsActivation] = None

    # ---- HTTP ----

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
        resp.raise_for_status()
        return resp

    # ---- 余额 / 价格 / 国家 ----

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"SmsBower getBalance 失败: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("SmsBower getPrices 返回结构异常")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """按价格 + 库存排序返回国家列表。"""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        # 策略1：使用专用排名 API
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue
        # 策略2：从 getPrices 解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc = services.get(service_code)
                if not isinstance(svc, dict):
                    continue
                price = svc.get("cost") or svc.get("price")
                count = svc.get("count") or svc.get("qty") or svc.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        rows = []
        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost")
                count = item.get("count") or item.get("qty") or item.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "price": price, "count": count})
        return rows

    def get_best_country(self, service: Optional[str] = None, *,
                         min_stock: int = 20, max_price: float = 0,
                         strict_whitelist: bool = False,
                         allowed_countries: Optional[list[str]] = None) -> Optional[str]:
        """自动选最优国家。

        allowed_countries 优先级最高（用户自定义 = 从这些国家里挑最便宜+库存足的）
        strict_whitelist  = True → 只从 OPENAI_SMS_COUNTRIES 选（即 52 泰国）
        都没设 → 全部国家自由选（默认；用户自行承担"OpenAI 让用 WhatsApp"的风险）
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("SmsBower get_best_country 查询失败: %s", exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                # 优先用 user-supplied 白名单
                if allowed_set is not None:
                    if cid not in allowed_set:
                        continue
                elif strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    continue
                price = row.get("price") or 0
                count = row.get("count") or 0
                if count < stock_threshold:
                    continue
                if max_price > 0 and price > max_price:
                    continue
                # 非白名单国家 → warn 一下（不阻止）
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    logger.warning(
                        "SmsBower 自动选了非 OpenAI-SMS 白名单国家 country=%s price=%s "
                        "（OpenAI 可能让此号用 WhatsApp 验证 → 收不到 SMS）",
                        cid, price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # ---- 号码复用缓存 ----

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> Optional[dict]:
        global _SMS_CACHE
        cache = _SMS_CACHE
        if cache is None:
            path = _smsbower_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(k) or "") != str(v) for k, v in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        _SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: Optional[dict]) -> None:
        global _SMS_CACHE
        _SMS_CACHE = cache
        path = _smsbower_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    # ---- 租号 ----

    def _request_number_single_action(self, action: str, service: str, country: str) -> dict:
        """单次调用 getNumberV2 或 getNumber（不自己 fallback，由调用方双重 for 控制）。

        借鉴 GuJumpgate：每个国家分别试 V2 / V1，而不是内部自动 fallback。
        """
        common = {"action": action, "service": service, "country": country}
        # 固定价格模式：SmsBower 用 minPrice=maxPrice（必须相等），HeroSMS 用 fixedPrice=true
        if self.fixed_price > 0:
            if "hero-sms.com" in self.base_url:
                common["maxPrice"] = self.fixed_price
                common["fixedPrice"] = "true"
            else:
                # SmsBower：minPrice 和 maxPrice 必须相等才能固定价格
                common["minPrice"] = self.fixed_price
                common["maxPrice"] = self.fixed_price
        elif self.max_price > 0:
            common["maxPrice"] = self.max_price
        logger.info("SmsBower %s: service=%s country=%s maxPrice=%s",
                    action, service, country, common.get("maxPrice", "未设置"))

        try:
            resp = self._request(common)
            resp_text = resp.text.strip()
            logger.info("SmsBower %s resp: status=%s text=%s", action, resp.status_code, resp_text[:500])

            # V2 返回 JSON
            if action == "getNumberV2":
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("activationId"):
                        return data
                except ValueError:
                    pass
                raise RuntimeError(resp_text[:200] or "empty response")

            # V1 返回纯文本 ACCESS_NUMBER:id:phone
            if resp_text.startswith("ACCESS_NUMBER:"):
                parts = resp_text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                    }
            raise RuntimeError(resp_text[:200] or "empty response")
        except Exception as e:
            # 不在这里 fallback，让调用方的 for action 循环去试下个 action
            raise

    @staticmethod
    def _format_phone(info: dict) -> str:
        raw = str(info.get("phoneNumber") or "").strip()
        cc = str(info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if cc and raw.startswith(cc):
            return f"+{raw}"
        if cc:
            return f"+{cc}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        """租号。支持多国家候选依次尝试（按入参顺序）。

        country_candidates: 候选国家 ID 列表，按这个顺序依次尝试；空时只用 country 单个。

        借鉴 GuJumpgate: 双重 for 循环 —— 外层遍历国家，内层每个国家先试 getNumberV2，
        失败才 fallback getNumber（V1）。
        """
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        # 单一 country 兜底
        if not country_candidates:
            country_candidates = [str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()]

        if self.parallel:
            # 并行模式：不碰 _SMS_VERIFY_LOCK（串行任务会整生命周期持有它，
            # 并行任务绝不能在里面排队），也不碰全局复用缓存。
            # 各任务独立 activation，getStatus 按 activation_id 查询，平台侧天然隔离。
            return self._rent_number_unlocked(service_code, country_candidates)

        with _SMS_VERIFY_LOCK:
            # 串行模式（原逻辑）：独占 _SMS_VERIFY_LOCK 直到本号生命周期结束
            # （PhoneCallbackController 在 report_success/cleanup 时才释放），
            # 防止两个任务同时抢同一份复用缓存导致串号。
            with _SMS_CACHE_LOCK:
                # 复用 cache（仅当用户许可且 cache 国家在候选列表里）
                cache = self._load_cache(service_code, country_candidates[0]) if self.reuse_phone_to_max else None
                if cache and str(cache.get("country") or "") in country_candidates:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=str(cache.get("country") or country_candidates[0]),
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                activation = self._rent_number_unlocked(service_code, country_candidates)
                # 成功 → 立刻保存到全局复用缓存（串行模式专属）
                cache = {
                    **self._cache_identity(service_code, activation.country),
                    "country": activation.country,
                    "activation_id": activation.activation_id,
                    "phone_number": activation.phone_number,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                }
                self._save_cache(cache)
                return activation

    def _rent_number_unlocked(self, service_code: str, country_candidates: list[str]) -> SmsActivation:
        """真正发请求租号（无缓存逻辑，无长锁）。串行/并行两模式共用。"""
        # 双重 for：外层国家 × 内层 action（V2 / V1）
        failures: list[str] = []
        last_exc: Optional[Exception] = None
        for cid in country_candidates:
            cid = str(cid).strip()
            if not cid:
                continue
            for action in ("getNumberV2", "getNumber"):
                try:
                    info = self._request_number_single_action(action, service_code, cid)
                    aid = str(info.get("activationId") or "")
                    phone = self._format_phone(info)
                    if not aid or not phone.strip("+"):
                        failures.append(f"{cid}: {action} 返回信息不完整")
                        continue  # 同国家试下个 action
                    activation = SmsActivation(
                        activation_id=aid,
                        phone_number=phone,
                        country=cid,
                        metadata={"reused": False},
                    )
                    self.current_activation = activation
                    if len(country_candidates) > 1:
                        logger.info("SmsBower 在国家 %s 租到号 %s (action=%s)", cid, phone, action)
                    return activation
                except Exception as e:
                    msg = str(e)[:120]
                    failures.append(f"{cid}: {action}={msg}")
                    last_exc = e
                    continue  # 同国家试下个 action

        detail = " | ".join(failures) if failures else "未知"
        raise RuntimeError(f"SmsBower 依次尝试 {len(country_candidates)} 个候选国家全失败: {detail}") from last_exc

    # ---- 等 code / 状态查询 ----

    def get_status(self, activation_id: str) -> dict:
        text = self._request({"action": "getStatus", "id": activation_id}).text
        return _parse_sms_status_text(text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_sms_status_text(text)
        if isinstance(data, str):
            return _parse_sms_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown"}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_sms_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(activation_id, f"getStatusV2.{channel}", item.get("code"))
                if candidate:
                    return candidate
        return {"status": "wait_code"}

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 3})
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 80, poll: int = 3,
                       openai_resend_interval: int = 20,
                       openai_resend_max: int = 3) -> Optional[dict]:
        """等 SMS 验证码：每 `openai_resend_interval` 秒触发一次 OpenAI 端 resend，
        最多 `openai_resend_max` 次。超过 timeout 仍没收到 → 返回 None（由上层 cancel 换号）。
        """
        deadline = time.time() + timeout
        start = time.time()
        openai_resend_count = 0
        last_smsbower_resend = start
        if self.parallel:
            # 并行模式：used_codes 是全局复用缓存里"本号已用过的码"去重表，
            # 跟当前任务的独立 activation 无关，绝不能拿来过滤自己的码。
            used_codes = set()
        else:
            with _SMS_CACHE_LOCK:
                cache = _SMS_CACHE or {}
                used_codes = set(cache.get("used_codes") or [])

        while time.time() < deadline:
            for src in ("v2", "v1"):
                try:
                    if src == "v2":
                        result = self.get_status_v2(activation_id)
                    else:
                        result = self.get_status(activation_id)
                    if result.get("status") == "cancel":
                        return None
                    if result.get("status") == "ok":
                        code = str(result.get("code") or "")
                        if code and code not in used_codes:
                            return {"status": "ok", "code": code,
                                    "sms_key": result.get("sms_key") or ""}
                except Exception as e:
                    logger.debug("SmsBower status %s 失败: %s", src, e)

            elapsed = time.time() - start
            # OpenAI 端 resend：固定间隔触发，最多 N 次
            expected_resend_count = min(openai_resend_max, int(elapsed // openai_resend_interval))
            if expected_resend_count > openai_resend_count and self._resend_callback:
                try:
                    self._resend_callback()
                    openai_resend_count = expected_resend_count
                    logger.info(
                        "SmsBower: 已请求 OpenAI 端 resend (第 %d/%d 次, elapsed=%ds)",
                        openai_resend_count, openai_resend_max, int(elapsed),
                    )
                except Exception as e:
                    logger.warning("OpenAI resend callback 失败: %s", e)
                # 同步请求 SmsBower 端 resend
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()
            elif time.time() - last_smsbower_resend >= openai_resend_interval:
                # 平时也间歇请求 SmsBower 端 resend，跟 OpenAI 同节奏
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()

            time.sleep(poll)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        # ⚠️ 不再用 cache.remaining 延长 timeout：
        # 用户给的 timeout 就是真 timeout，超时就让上层换号或换 attempt。
        # （旧逻辑会被拉到 20 分钟号码生命周期，OpenAI 端 phone-otp challenge 等不了那么久）
        candidate = self.wait_for_code(activation_id, timeout=timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # ---- 状态报告 ----

    def cancel(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            ok = resp.status_code == 204 or "ACCESS_CANCEL" in resp.text
        except Exception:
            ok = False
        if not ok:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
                ok = "ACCESS_CANCEL" in resp.text
            except Exception:
                ok = False
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._clear_cache()
        return ok

    def report_success(self, activation_id: str) -> bool:
        if self.parallel:
            # 并行模式：不走全局复用缓存，直接 finish（一次性号码）。
            try:
                resp = self._request({"action": "finishActivation", "id": activation_id})
                return resp.status_code in (200, 204) or "ACCESS" in resp.text
            except Exception:
                try:
                    resp = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                    return "ACCESS" in resp.text
                except Exception:
                    return False
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            should_finish = False
            should_clear = False
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                remaining = SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    should_finish = True
                    cache["reuse_stopped"] = True
                elif remaining <= 30:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                self._save_cache(cache)
                if should_clear:
                    self._clear_cache()
        try:
            if should_finish or not (cache and str(cache.get("activation_id")) == str(activation_id)):
                resp = self._request({"action": "finishActivation", "id": activation_id})
                return resp.status_code in (200, 204) or "ACCESS" in resp.text
        except Exception:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                return "ACCESS" in resp.text
            except Exception:
                return False
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                self._save_cache(cache)
        if self._resend_callback:
            try:
                self._resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # 业务侧拒了这个号 → cancel 退款（号根本没用上，不能让主人白花钱）
        cancel_ok = False
        try:
            resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
            cancel_ok = "ACCESS_CANCEL" in resp.text or resp.status_code in (200, 204)
        except Exception:
            pass
        # 简化原因显示：只保留前 80 字符
        short_reason = (reason or "未知原因")[:80]
        logger.info("SmsBower 号 activation_id=%s cancel 退款 %s (原因: %s)",
                    activation_id, "✅" if cancel_ok else "❌", short_reason)
        # 同时清掉复用缓存（避免下次注册又拿到这个被拒的号）
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "phone rejected"
                self._save_cache(cache)
                self._clear_cache()

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._resend_callback = callback



# ---------------------------------------------------------------------------
# 独立手机号池 provider（号码 + 取码URL 由主人本地维护）
# ---------------------------------------------------------------------------


class CustomPhonePoolProvider(BaseSmsProvider):
    """独立手机号池 provider。

    号码和取码（接收）URL 在 WebUI「手机号池（独立）」页维护，存 webui.db 的
    sms_phone_pool 表。get_number 直接从池里 claim；get_code 轮询该号绑定的
    接收 URL，按 GuJumpgate hosted-pool 的规则提取 6 位验证码：

        1) 优先从 sms/code/otp/message/text... 类字段取 6 位码；
        2) 兜底取文本中最后出现的 6 位码；
        3) 排除 000000 / 123456 这类示例码。
    """

    # 收到 code 不立刻报成功：等业务侧 validate 通过再计数，
    # 避免并行 worker 在 validate 窗口内抢到同一个号。
    auto_report_success_on_code = False

    _CODE_KEY_RE = re.compile(
        r"^(sms|smscode|sms_code|message|msg|text|content|body|code|otp|verificationcode|verification_code)$",
        re.I,
    )
    _CODE_META_RE = re.compile(r"(^|[_-])(phone|mobile|tel|id|order|time|date|expired|expire|status)([_-]|$)", re.I)
    _CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
    _BAD_CODES = {str(d) * 6 for d in "0123456789"} | {"012345", "123456", "654321"}

    def __init__(self, config: dict, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log = log_fn or (lambda msg: logger.info("[sms-pool] %s", msg))
        self.max_use = max(1, _safe_int(self.config.get("sms_phone_success_max"), 3))
        self.auto_disable = _safe_bool(self.config.get("sms_pool_auto_disable"), True)
        self.fail_threshold = max(1, _safe_int(self.config.get("sms_pool_fail_threshold"), 2))
        self._current: Optional[dict] = None

    @staticmethod
    def _db():
        # 延迟导入，避免 sms_provider ↔ webui 循环依赖
        from webui import db as _webui_db
        return _webui_db

    # -- 两阶段主流程 ------------------------------------------------------

    def get_number(self, *, service: str = "", country: str = "",
                   country_candidates: Optional[list[str]] = None) -> SmsActivation:
        row = self._db().pool_claim(max_use=self.max_use)
        if not row:
            raise RuntimeError(
                "独立手机号池没有可用号码（全部用完 / 已禁用 / 被占用），"
                "请先到「手机号池（独立）」导入或清理"
            )
        self._current = row
        digits = "".join(ch for ch in str(row.get("phone") or "") if ch.isdigit())
        prefix = digits[1:7] if len(digits) == 11 and digits.startswith("1") else digits[:6]
        self.log(f"✅ 号码池随机取号: {row['phone']}（号段 {prefix}，已用 {row['use_count']}/{self.max_use} 次）")
        return SmsActivation(
            activation_id=str(row["id"]),
            phone_number=row["phone"],
            country="",
            metadata={"recv_url": row["recv_url"], "pool_id": row["id"]},
        )

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        if not self._current:
            raise RuntimeError("sms-pool: 未先 get_number")
        recv_url = str(self._current.get("recv_url") or "").strip()
        if not recv_url:
            self.log(f"⚠️ 号 {self._current.get('phone')} 没有配置取码 URL，无法接码")
            return ""
        deadline = time.time() + max(10, int(timeout or 180))
        self.log(f"⏳ 轮询取码URL: {recv_url[:90]}（窗口 {int(timeout or 180)}s / 每 5s 一次）")
        while time.time() < deadline:
            try:
                code = self._fetch_once(recv_url)
            except Exception as e:  # noqa: BLE001
                self.log(f"⚠️ 取码URL请求失败: {str(e)[:120]}")
                code = ""
            if code:
                self.log(f"✅ 从取码URL解析到验证码: {code}")
                return code
            time.sleep(5)
        return ""

    # -- 取码 URL 解析 -----------------------------------------------------

    def _fetch_once(self, recv_url: str) -> str:
        import requests as _requests
        resp = _requests.get(
            recv_url,
            timeout=10,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Cache-Control": "no-cache",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) sms-pool/1.0",
            },
        )
        return self._extract_code(resp.text or "")

    @classmethod
    def _walk(cls, value, path: str = ""):
        """遍历 JSON 结构，产出 (叶子key, 文本) 候选对。"""
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                yield (path.rsplit(".", 1)[-1] if path else ""), text
            return
        if isinstance(value, dict):
            for k, v in value.items():
                yield from cls._walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, (list, tuple)):
            for idx, v in enumerate(value):
                yield from cls._walk(v, f"{path}[{idx}]")

    @classmethod
    def _extract_code(cls, text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = raw
        candidates = list(cls._walk(payload)) if not isinstance(payload, str) else [("", payload)]
        # 第一遍：信任字段里的 6 位码
        for key, val in candidates:
            if not key or cls._CODE_META_RE.search(key):
                continue
            if not cls._CODE_KEY_RE.match(key):
                continue
            m = cls._CODE_RE.search(val)
            if m and m.group(1) not in cls._BAD_CODES:
                return m.group(1)
        # 第二遍：任意文本兜底（取最后出现的 6 位码）
        for _key, val in reversed(candidates):
            matches = [m.group(1) for m in cls._CODE_RE.finditer(val)]
            for code in reversed(matches):
                if code not in cls._BAD_CODES and len(set(code)) > 1:
                    return code
        return ""

    # -- 状态回写 ----------------------------------------------------------

    def _fail(self, activation_id: str, reason: str, *, release: bool = True,
              auto_disable: bool = True) -> None:
        if not self._current:
            return
        try:
            self._db().pool_mark_fail(
                int(activation_id), reason,
                release=release, auto_disable=auto_disable,
                fail_threshold=self.fail_threshold,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sms-pool mark_fail 失败: %s", e)

    def cancel(self, activation_id: str) -> bool:
        # 单号窗口用尽（没等到码）→ 记失败 + 释放；连续超时按阈值自动禁用
        self._fail(activation_id, "等待验证码超时", release=True)
        return True

    def report_success(self, activation_id: str) -> bool:
        if self._current:
            try:
                self._db().pool_mark_success(int(activation_id))
                self.log(f"🎉 号码 {self._current.get('phone')} 用码成功，已计数并释放")
            except Exception as e:  # noqa: BLE001
                logger.warning("sms-pool mark_success 失败: %s", e)
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        # validate 失败：号没坏（码收到了但校验不过），保留占用等重发，不自动禁用
        self._fail(activation_id, f"验证码校验失败: {reason}", release=False, auto_disable=False)

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # OpenAI 拒了这个号（已用 / 黑名单 / 号段可疑）→ 记失败 + 释放 + 按阈值自动禁用
        self._fail(activation_id, f"号码被目标站拒绝: {reason}", release=True)

    def get_balance(self) -> float:
        """余额语义 = 池里当前可取的号数。"""
        try:
            return float(self._db().pool_stats().get("idle", 0))
        except Exception:  # noqa: BLE001
            return 0.0


# ---------------------------------------------------------------------------
# 工厂 + 回调控制器（注入到 auth_flow）
# ---------------------------------------------------------------------------


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    provider_key: smsbower / herosms / custom_pool（独立手机号池）
    config 字段：sms_api_key / sms_country / sms_service / sms_max_price /
                sms_reuse_phone / sms_phone_success_max
    """
    pk = (provider_key or "").lower().strip()

    # 独立手机号池：号码来自本地池表，不需要 API Key
    if pk in ("custom_pool", "phone_pool"):
        return CustomPhonePoolProvider(config)

    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{pk} 未配置 API Key")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # 接码 API 请求走的代理：复用全局 proxy（registrar 注入注册流程的代理），
    # 也允许调用方显式传 sms_proxy 覆盖（保留扩展点，目前 WebUI 不暴露）。
    proxy = (str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)
    fixed_price = _safe_float(config.get("sms_fixed_price"), -1)
    reuse = _safe_bool(config.get("sms_reuse_phone"), False)
    succ_max = max(0, _safe_int(config.get("sms_phone_success_max"), 3))

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(api_key=api_key,
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    if pk in ("herosms", "hero_sms"):
        return SmsBowerProvider(api_key=api_key,
                                base_url="https://hero-sms.com/stubs/handler_api.php",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    raise RuntimeError(f"未知接码服务: {provider_key}")


class PhoneCallbackController:
    """把 SMS provider 包装成两阶段回调，注入到 auth_flow.add_phone 流程。

    用法（在 auth_flow._handle_add_phone_verification 里）：
        controller = PhoneCallbackController(...)
        phone = controller.get_phone()         # 阶段1：租号
        flow._add_phone_send(phone)
        ...
        code = controller.get_code()           # 阶段2：等 SMS 验证码
        flow._phone_otp_validate(code)
        controller.report_success()            # 成功
        # 失败时 controller.cancel() / mark_code_failed()
    """

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
        parallel: bool = False,
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        # 并行模式（补 refresh 多并发）：不持全局 verify 锁、不复用号码 ——
        # 每任务独立租号独立等码，多个号可以同时绑手机。
        self.parallel = bool(parallel)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
            if self.parallel and isinstance(self.provider, SmsBowerProvider):
                self.provider.parallel = True
                logger.info("[sms] 并行模式: 跳过号码复用缓存, 每任务独立租号独立等码")
        return self.provider

    def get_phone(self) -> str:
        """阶段 1：租手机号（已带 +）。"""
        provider = self._provider()
        # 同号复用锁（SmsBower 系列才用，防止两个注册任务并发抢同一个 cache）。
        # 并行模式不拿这把锁 —— 锁的整周期持有正是"绑号串行"的根源；
        # 并行模式禁用复用，各任务独立 activation，没有共享状态要保护。
        if not self.parallel and isinstance(provider, SmsBowerProvider) and not self._verify_lock_acquired:
            _SMS_VERIFY_LOCK.acquire()
            self._verify_lock_acquired = True

        # 收集候选国家列表：用户多选 > 自动选号选出的 best > 单一 country
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed_list = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]

        effective_country = self.country
        country_candidates: list[str] = []

        if self.auto_select_country and isinstance(provider, SmsBowerProvider):
            if allowed_list:
                self.log(f"🔍 自动选号: 从主人勾选的 {len(allowed_list)} 个国家依次尝试（按价格升序）")
                try:
                    rows = provider.get_top_countries(service=self.service)
                    # 按价格升序排，只保留在 allowed_list 中的
                    in_allow = [r for r in rows if str(r.get("country") or "") in allowed_list]
                    ordered_allowed = [str(r["country"]) for r in in_allow]
                    # 把 allowed 里没在排名中出现的也加在最后
                    appended = [c for c in allowed_list if c not in ordered_allowed]
                    country_candidates = ordered_allowed + appended
                    self.log(f"  候选顺序: {','.join(country_candidates)}")
                except Exception as e:
                    self.log(f"  排名查询失败({e})，按主人勾选的原始顺序尝试")
                    country_candidates = list(allowed_list)
            else:
                # 未多选时，单纯按价格选最便宜（默认非严格白名单）
                self.log("🔍 自动选号（未指定允许国家，按全平台价格+库存挑最优）...")
                try:
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=_safe_int(self.config.get("sms_auto_min_stock"), 20),
                        max_price=_safe_float(self.config.get("sms_auto_max_price"), 0),
                        strict_whitelist=_safe_bool(self.config.get("sms_strict_whitelist"), False),
                    )
                    if best:
                        name_cn = SMS_COUNTRY_NAMES_CN.get(best, "未知")
                        in_wl = best in OPENAI_SMS_COUNTRIES
                        wl_label = "✅ OpenAI SMS 白名单" if in_wl else "⚠️ 非白名单"
                        self.log(f"✅ 自动选择国家: {best} {name_cn}  [{wl_label}]")
                        country_candidates = [best]
                    else:
                        self.log("⚠️ 未找到满足条件的国家，使用默认 country")
                        country_candidates = [self.country] if self.country else []
                except Exception as e:
                    self.log(f"⚠️ 国家智能选择失败({e})，使用默认 country")
                    country_candidates = [self.country] if self.country else []
        else:
            # 没启用自动选号 → 强制用默认国家
            country_candidates = [self.country] if self.country else []

        if not country_candidates:
            country_candidates = [SMS_DEFAULT_COUNTRY]

        country_label_log = ",".join(
            f"{c}({SMS_COUNTRY_NAMES_CN.get(c, '?')})" for c in country_candidates[:5]
        )
        self.log(f"📱 准备租号: provider={self.provider_key} service={self.service} 候选={country_label_log}{' ...' if len(country_candidates) > 5 else ''}")
        try:
            self.activation = provider.get_number(
                service=self.service,
                country=country_candidates[0],
                country_candidates=country_candidates,
            )
        except Exception as exc:
            self._release_lock()
            raise

        reused = bool((self.activation.metadata or {}).get("reused"))
        used_country = self.activation.country or country_candidates[0]
        used_country_label = f"{used_country} {SMS_COUNTRY_NAMES_CN.get(used_country, '')}"
        self.log(f"✅ 已租到号码{'(复用)' if reused else ''}{'(并行独立)' if self.parallel else ''}: "
                 f"{self.activation.phone_number} "
                 f"国家={used_country_label} (activation_id={self.activation.activation_id})")
        return self.activation.phone_number

    def get_code(self, timeout: int = 180) -> str:
        """阶段 2：等待 SMS 验证码。"""
        if not self.activation:
            raise RuntimeError("PhoneCallbackController: 未先 get_phone")
        provider = self._provider()
        self.log(f"⏳ 等待 SMS 验证码... (activation_id={self.activation.activation_id} timeout={timeout}s)")
        code = provider.get_code(self.activation.activation_id, timeout=timeout)
        if code:
            self.log(f"✅ 收到 SMS 验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"⚠️ 未收到 SMS 验证码: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as e:
                logger.warning("report_success 失败: %s", e)
            self.completed = True
            self.log(f"🎉 已标记号码成功完成: activation_id={self.activation.activation_id}")
        self._release_lock()

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        try:
            self._provider().set_resend_callback(callback)
        except Exception:
            pass

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号、解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"🗑️ 已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        self._release_lock()

    def _release_lock(self) -> None:
        if self._verify_lock_acquired:
            try:
                _SMS_VERIFY_LOCK.release()
            except RuntimeError:
                pass
            self._verify_lock_acquired = False


# ---------------------------------------------------------------------------
# 简单 CLI 测试
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python sms_provider.py <provider_key> <api_key> [country]")
        sys.exit(1)
    pk = sys.argv[1]
    key = sys.argv[2]
    cc = sys.argv[3] if len(sys.argv) > 3 else ""
    p = create_sms_provider(pk, {"sms_api_key": key, "sms_country": cc})
    print(f"余额: {p.get_balance()}")
