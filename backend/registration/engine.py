#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""账号注册编排引擎。

组合邮箱渠道、浏览器流程、结果持久化与任务取消机制，提供 Web 后台
调用的批量注册入口。
"""

import threading
import datetime
import time
import os
import gc
import secrets
import random
import re
import json
import traceback
from urllib.parse import urlsplit

from playwright._impl._errors import TargetClosedError as PageDisconnectedError
from curl_cffi import requests

from backend.integrations import exit_ip as _exit_ip
from backend.mailbox import cloudflare_worker as cloudflare_provider
from backend.mailbox import cloud_mail as cloudmail_provider
from backend.mailbox import duck_mail as duckmail_provider
from backend.mailbox import mail_nest as mailnest_provider
from backend.mailbox import outlook_pool as outlookemail_provider
from backend.mailbox import yyds_mail as yyds_provider
from backend.mailbox.utilities import extract_verification_code as _extract_code
from backend.mailbox.utilities import generate_username as _generate_username
from backend.mailbox.utilities import pick_list_payload as _pick_list

from backend.automation import session as _bs
from backend.registration import signup_flow as _rf
from backend.integrations import network_checks as _conn
from backend.registration.store import RegistrationRepository
from backend.integrations.proxy import redact_proxy_text, redact_proxy_url, resolve_proxy_url
from backend.shared.paths import DATA_ROOT, PROJECT_ROOT
from backend.automation.session import (
    browser,
    page,
    active_browser as _active_browser,
    active_page as _active_page,
    set_browser_session as _set_browser_session,
    start_browser,
    stop_browser,
    restart_browser,
    cleanup_runtime_memory,
    refresh_active_page,
    extract_cf_clearance_and_ua,
    create_browser_options,
    get_start_fail_streak,
    cleanup_stale_profiles as _cleanup_stale_profiles,
)
from backend.registration.signup_flow import (
    SIGNUP_URL,
    click_email_signup_button,
    open_signup_page,
    has_profile_form,
    detect_email_domain_rejection,
    raise_if_email_domain_rejected,
    fill_email_and_submit,
    fill_code_and_submit,
    getTurnstileToken,
    build_profile,
    fill_profile_and_submit,
    wait_for_registration_completion,
)



APP_DIR = str(PROJECT_ROOT)
DATA_DIR = str(DATA_ROOT)
CONFIG_FILE = os.path.abspath(
    os.path.expanduser(os.environ.get("GROK_CONFIG_FILE", os.path.join(APP_DIR, "config.json")))
)
# 所有注册运行数据统一放入 data/，避免与前后端代码混放。
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
RESULTS_DB_FILE = os.path.join(ACCOUNTS_DIR, "registration_results.sqlite3")
MEMORY_CLEANUP_INTERVAL = 5
TRACEBACK_MAX_CHARS = 60_000
TRACEBACK_LOG_MAX_CHARS = 16_000

_repository = None
_repository_lock = threading.Lock()
_network_route_log_lock = threading.Lock()
_network_route_log_keys = set()


def current_exception_traceback(max_chars=TRACEBACK_MAX_CHARS):
    """返回当前异常的标准堆栈；没有活动异常时返回空字符串。"""
    text = traceback.format_exc().strip()
    if not text or text == "NoneType: None":
        return ""

    limit = max(1_000, int(max_chars or TRACEBACK_MAX_CHARS))
    if len(text) > limit:
        tail_size = min(4_000, limit // 4)
        text = (
            text[: limit - tail_size]
            + "\n... 异常堆栈过长，已截断 ...\n"
            + text[-tail_size:]
        )
    return text


def ensure_accounts_dir():
    """确保 data/accounts/ 存在，返回目录绝对路径。"""
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    return ACCOUNTS_DIR


def account_file_for_email(email):
    """单个账号的独立输出路径：data/accounts/{email}.txt"""
    ensure_accounts_dir()
    safe_email = str(email or "").strip().replace("/", "_").replace("\\", "_")
    return os.path.join(ACCOUNTS_DIR, f"{safe_email}.txt")


def get_registration_repository():
    """懒加载 SQLite；首次启动时把旧账号 TXT 补录为成功结果。"""
    global _repository
    if _repository is not None:
        return _repository
    with _repository_lock:
        if _repository is None:
            store = RegistrationRepository(RESULTS_DB_FILE)
            store.import_existing_accounts(ACCOUNTS_DIR)
            _repository = store
    return _repository


def email_registered_successfully(email):
    """数据库或旧账号文件中已有成功/已消耗记录时返回 True。

    除正式 success 外，已判定 already_registered 或已尝试停用的 Outlook
    邮箱也应跳过，避免邮箱池重复取用造成死循环。
    """
    normalized = str(email or "").strip()
    if not normalized:
        return False
    try:
        repo = get_registration_repository()
        if repo.has_success(normalized):
            return True
        if hasattr(repo, "has_registered_or_consumed") and repo.has_registered_or_consumed(normalized):
            return True
    except Exception:
        pass
    return os.path.isfile(account_file_for_email(normalized))


DEFAULT_CONFIG = {
    "email_provider": "cloudflare",
    "duckmail_api_key": "",
    "duckmail_api_base": "https://api.duckmail.sbs",
    "defaultDomains": "",
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_custom_auth": "",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/admin/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "outlookemail_api_base": "",
    "outlookemail_api_key": "",
    "outlookemail_source": "accounts",
    "outlookemail_group_id": "",
    "outlookemail_code_timeout_group_id": "",
    "outlookemail_web_password": "",
    "outlookemail_session_cookie": "",
    "outlookemail_temp_tag_ids": "",
    "outlookemail_folder": "all",
    "outlookemail_top": 10,
    "outlookemail_pick_mode": "random",
    "outlookemail_disable_after_registration_success": False,
    "proxy": "http://127.0.0.1:7890",
    "debug_mode": False,
    "browser_engine": _bs.normalize_browser_engine(
        os.environ.get("GROK_BROWSER_ENGINE", "camoufox")
    ),
    "browser_headless": False,
    "browser_locale": "en-US",
    "browser_low_traffic_mode": True,
    "browser_traffic_savings_level": "standard",
    "close_browser_on_stop": False,
    "log_level": "info",
    "register_count": 1,
    "register_workers": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "mailnest_api_key": "",
    "mailnest_project_code": "x-ai001",
    # YYDS：留空自动选已验证域名；填写则固定该域名
    "yyds_default_domain": "",
    # 账号间注册间隔（秒），0=不等待。填一个整数=N秒固定等待，填区间"60-120"=随机等待
    "account_interval": "60-120",
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


class EmailDomainRejected(Exception):
    """xAI 拒绝当前邮箱域名（如公共临时域被拉黑）。"""

    def __init__(self, email="", message=""):
        self.email = email or ""
        self.message = message or "邮箱域名已被拒绝"
        domain = ""
        if "@" in self.email:
            domain = self.email.split("@", 1)[1]
        detail = self.message
        if domain and domain not in detail:
            detail = f"{detail}（域名: {domain}）"
        if self.email and self.email not in detail:
            detail = f"{detail} | 邮箱: {self.email}"
        super().__init__(detail)


FAIL_CODE = "verification_code"
FAIL_DOMAIN = "email_domain"
FAIL_ALREADY_REGISTERED = "already_registered"
FAIL_BROWSER = "browser"
FAIL_STUCK = "flow_stuck"
FAIL_OTHER = "other"
FAIL_LABELS = {
    FAIL_CODE: "验证码失败",
    FAIL_DOMAIN: "邮箱域名拒绝",
    FAIL_ALREADY_REGISTERED: "账号已存在",
    FAIL_BROWSER: "浏览器异常",
    FAIL_STUCK: "注册流程卡住",
    FAIL_OTHER: "其他失败",
}


def prepare_registration_exit_ip(log_callback=None) -> str:
    """打开注册页前，在浏览器里识别出口 IP，并尽量避开已风控出口。"""
    return _exit_ip.ensure_unflagged_exit_ip(
        store=get_registration_repository(),
        proxy_enabled=bool(get_proxies()),
        log_callback=log_callback,
        restart=lambda: restart_browser(log_callback=log_callback),
    )


def classify_failure(exc) -> str:
    if isinstance(exc, EmailDomainRejected):
        return FAIL_DOMAIN
    if isinstance(exc, _rf.AccountAlreadyRegistered):
        return FAIL_ALREADY_REGISTERED
    msg = str(exc or "")
    low = msg.lower()
    if isinstance(exc, AccountRetryNeeded) or "达到最大重试" in msg or "流程卡住" in msg:
        return FAIL_STUCK
    if "registration_timeout" in low or "等待注册完成" in msg:
        return FAIL_STUCK
    if "未收到验证码" in msg or "验证码阶段失败" in msg or "验证码" in msg and "失败" in msg:
        return FAIL_CODE
    if (
        "浏览器" in msg
        or "page disconnected" in low
        or "与页面的连接已断开" in msg
        or "PageDisconnected" in msg
        or "disconnected" in low
    ):
        return FAIL_BROWSER
    return FAIL_OTHER


def empty_fail_stats():
    return {k: 0 for k in FAIL_LABELS}


def format_fail_stats(stats: dict, labels=None) -> str:
    active_labels = dict(FAIL_LABELS)
    active_labels.update(dict(labels or {}))
    parts = [
        f"{active_labels.get(k, k)}={stats.get(k, 0)}"
        for k in active_labels
        if stats.get(k, 0)
    ]
    if not parts:
        return "无分类失败"
    return " | ".join(parts)


def new_registration_batch_id(source="web"):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{source}-{stamp}-{secrets.token_hex(3)}"


def current_attempt_email(email="", exc=None):
    return str(
        getattr(exc, "email", "")
        or email
        or _rf.last_acquired_email()
        or ""
    ).strip()


def current_attempt_password(profile=None):
    current = dict(profile or {})
    if current.get("password"):
        return str(current.get("password") or "")
    return str(_rf.last_profile().get("password") or "")


def capture_failure_screenshot(
    *,
    batch_id="",
    worker_id=0,
    email="",
    failure_type="",
    log_callback=None,
):
    """保存当前活动页面；页面不存在或已经断开时返回空路径。"""
    current_page = _active_page()
    if current_page is None:
        return ""

    def _safe_part(value, fallback):
        normalized = re.sub(r"[^A-Za-z0-9._@-]+", "_", str(value or "").strip())
        return normalized.strip("._-")[:80] or fallback

    folder = os.path.join(DATA_DIR, "screenshots", "registration-failures")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = "-".join(
        (
            _safe_part(batch_id, "batch"),
            f"w{max(int(worker_id or 0), 0) + 1}",
            _safe_part(email, "unknown"),
            _safe_part(failure_type, "failure"),
            stamp,
            secrets.token_hex(2),
        )
    ) + ".png"
    path = os.path.abspath(os.path.join(folder, filename))
    try:
        os.makedirs(folder, exist_ok=True)
        current_page.screenshot(path=path, full_page=True)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return ""
        if log_callback:
            log_callback(f"[截图] 浏览器失败现场已保存: {path}")
        return path
    except Exception as exc:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        if log_callback:
            log_callback(f"[Debug] 浏览器失败截图保存失败: {exc}")
        return ""


def is_outlookemail_registration(provider="") -> bool:
    value = str(provider or config.get("email_provider", "") or "").strip().lower()
    return value == "outlookemail"


def default_email_disable_detail(provider="", _legacy_detail=None) -> dict:
    if not is_outlookemail_registration(provider):
        status = "not_applicable"
    elif not bool(config.get("outlookemail_disable_after_registration_success", False)):
        status = "feature_disabled"
    elif get_outlookemail_source() != "accounts":
        status = "unsupported_source"
    else:
        status = "not_attempted"
    return {
        "status": status,
        "account_id": "",
        "disabled_at": "",
        "error": "",
    }


def persist_registration_result(
    *,
    batch_id,
    source,
    started_at,
    email="",
    password="",
    status="failure",
    provider="",
    worker_id=0,
    platform="xai",
    email_disable_detail=None,
    failure_type="",
    failure_reason="",
    screenshot_path="",
    account_file="",
    extra=None,
    log_callback=None,
):
    """统一保存 Web 注册结果；写库异常不打断注册流程。"""
    finished_epoch = time.time()
    try:
        started_epoch = float(started_at or finished_epoch)
    except (TypeError, ValueError):
        started_epoch = finished_epoch
    started_text = datetime.datetime.fromtimestamp(started_epoch).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    finished_text = datetime.datetime.fromtimestamp(finished_epoch).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    provider_name = provider or str(config.get("email_provider", "") or "")
    extra_data = dict(extra or {})
    disable_detail = default_email_disable_detail(provider_name)
    disable_detail.update(dict(email_disable_detail or {}))
    try:
        repository = get_registration_repository()
        registration_id = repository.add_result(
            {
                "batch_id": batch_id,
                "source": source,
                "started_at": started_text,
                "finished_at": finished_text,
                "duration_seconds": max(finished_epoch - started_epoch, 0),
                "email": email,
                "password": password,
                "status": status,
                "success": status == "success",
                "provider": provider_name,
                "worker_id": worker_id,
                "platform": platform,
                "email_account_id": disable_detail.get("account_id", ""),
                "email_disable_status": disable_detail.get("status", "not_attempted"),
                "email_disabled_at": disable_detail.get("disabled_at", ""),
                "email_disable_error": disable_detail.get("error", ""),
                "failure_type": failure_type,
                "failure_reason": str(failure_reason or ""),
                "screenshot_path": screenshot_path,
                "account_file": account_file,
                "extra": extra_data,
            }
        )
        return registration_id
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] SQLite 保存注册结果失败: {exc}")
        return None



def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    config["browser_engine"] = _bs.normalize_browser_engine(
        config.get("browser_engine", "camoufox")
    )
    return config


def parse_account_interval() -> float:
    """解析 account_interval 配置，返回等待秒数。

    "0" / "" → 0（不等待）
    "30" → 30.0（固定 30 秒）
    "60-120" → 60~120 之间的随机值
    """
    raw = str(config.get("account_interval", "0") or "0").strip()
    if not raw or raw == "0":
        return 0.0
    if "-" in raw:
        parts = raw.split("-", 1)
        try:
            lo = max(int(parts[0].strip()), 0)
            hi = max(int(parts[1].strip()), lo)
            return float(random.randint(lo, hi))
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(int(raw))
    except ValueError:
        return 0.0


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


load_config()

# turnstilePatch 是 Chrome 扩展，Camoufox 基于 Firefox 不兼容，已移除。
# Turnstile 交互由 signup_flow.getTurnstileToken 统一处理。
EXTENSION_PATH = ""


DUCKMAIL_API_BASE_DEFAULT = duckmail_provider.API_BASE_DEFAULT


def get_proxies():
    proxy = resolve_proxy_url(config.get("proxy", ""))
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def reset_network_route_logs():
    with _network_route_log_lock:
        _network_route_log_keys.clear()


def _log_actual_http_route(method, url, *, proxies=None, proxy=""):
    """记录实际请求的接口和路由；相同方法/接口/路由只记录一次。"""
    parsed = urlsplit(str(url or ""))
    display_url = (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        if parsed.netloc
        else str(url or "")
    )
    proxy_value = str(proxy or "").strip()
    if not proxy_value and isinstance(proxies, dict):
        proxy_value = str(
            proxies.get(parsed.scheme)
            or proxies.get("all")
            or proxies.get("https")
            or proxies.get("http")
            or ""
        ).strip()
    route = f"代理 {redact_proxy_url(proxy_value)}" if proxy_value else "直连（不使用代理）"
    key = (str(method or "GET").upper(), display_url, route)
    with _network_route_log_lock:
        if key in _network_route_log_keys:
            return
        _network_route_log_keys.add(key)
    registration_log(f"[*] [网络] {key[0]} {display_url} -> {route}")


def get_duckmail_api_base():
    return duckmail_provider.normalize_base(str(config.get("duckmail_api_base", "") or ""))


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")



def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_custom_auth():
    """全局访问密码（cloudflare_temp_email 的 PASSWORDS）。"""
    return str(config.get("cloudflare_custom_auth", "") or "").strip()


def cloudflare_apply_custom_auth(headers):
    return cloudflare_provider.apply_custom_auth(headers, get_cloudflare_custom_auth())


def get_cloudflare_path(key, default_path):
    return cloudflare_provider.path_from_config(config, key, default_path)


def cloudflare_build_headers(content_type=False):
    return cloudflare_provider.build_headers(
        get_cloudflare_api_key(),
        get_cloudflare_auth_mode(),
        get_cloudflare_custom_auth(),
        content_type=content_type,
    )


def cloudflare_apply_auth_params(params=None):
    return cloudflare_provider.apply_auth_params(
        params, get_cloudflare_api_key(), get_cloudflare_auth_mode()
    )


def cloudflare_next_default_domain():
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    domain, _cf_domain_index = cloudflare_provider.next_default_domain(domains, _cf_domain_index)
    return domain


def cloudflare_is_admin_create_path(path):
    return cloudflare_provider.is_admin_create_path(path)


def _pick_list_payload(data):
    return _pick_list(data)


def cloudflare_create_temp_address(api_base):
    return cloudflare_provider.create_temp_address(
        http_post,
        api_base,
        accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/admin/new_address"),
        domain=cloudflare_next_default_domain(),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        name=generate_username(10),
    )


MAILNEST_API_BASE = mailnest_provider.API_BASE
MAILNEST_DEFAULT_PROJECT_CODE = mailnest_provider.DEFAULT_PROJECT_CODE


def get_mailnest_api_key():
    key = str(config.get("mailnest_api_key", "") or "").strip()
    if not key:
        raise Exception(f"请在配置文件中配置 mailnest_api_key | 注册网址：{MAILNEST_API_BASE}")
    return key


def get_mailnest_project_code():
    code = str(config.get("mailnest_project_code", "") or "").strip()
    return code or MAILNEST_DEFAULT_PROJECT_CODE


def mailnest_buy_email():
    return mailnest_provider.buy_email(http_post, get_mailnest_api_key(), get_mailnest_project_code())


def mailnest_receive_email(email):
    return mailnest_provider.receive_email(http_post, get_mailnest_api_key(), email)


def mailnest_get_code(email, timeout=60, poll_interval=3, log_callback=None, cancel_callback=None):
    return mailnest_provider.wait_for_code(
        http_post,
        get_mailnest_api_key(),
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def get_outlookemail_api_base():
    return str(config.get("outlookemail_api_base", "") or "").strip().rstrip("/")


def get_outlookemail_api_key():
    return str(config.get("outlookemail_api_key", "") or "").strip()


def get_outlookemail_source():
    return outlookemail_provider.normalize_source(config.get("outlookemail_source", "accounts"))


def get_outlookemail_group_id() -> str:
    return str(config.get("outlookemail_group_id", "") or "").strip()


def get_outlookemail_code_timeout_group_id() -> str:
    return str(config.get("outlookemail_code_timeout_group_id", "") or "").strip()


def list_outlookemail_groups() -> list[dict]:
    return outlookemail_provider.list_groups(
        http_get,
        direct_http_session,
        get_outlookemail_api_base(),
        api_key=get_outlookemail_api_key(),
        web_password=str(config.get("outlookemail_web_password", "") or ""),
        session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
        proxies={},
    )


def _outlookemail_account_already_saved(email):
    return email_registered_successfully(email)


def reset_outlookemail_runtime_state():
    outlookemail_provider.reset_runtime_state()


def disable_outlookemail_after_registration_success(email, log_callback=None) -> dict:
    """注册成功后停用 accounts 来源邮箱；停用结果不改变注册成功状态。"""
    detail = default_email_disable_detail("outlookemail")
    if detail["status"] != "not_attempted":
        if log_callback and detail["status"] == "feature_disabled":
            log_callback("[*] OutlookEmail 注册成功后停用功能未开启")
        return detail

    normalized_email = str(email or "").strip()
    try:
        account = outlookemail_provider.account_for_email(
            http_get,
            get_outlookemail_api_base(),
            get_outlookemail_api_key(),
            normalized_email,
            group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
        )
        detail["account_id"] = str(account.get("id") or "")
        if log_callback:
            log_callback(
                f"[OutlookEmail] 注册成功，正在停用邮箱账号 ID={detail['account_id'] or '-'}"
            )
        result = outlookemail_provider.disable_account(
            http_get,
            direct_http_session,
            get_outlookemail_api_base(),
            normalized_email,
            api_key=get_outlookemail_api_key(),
            group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
            web_password=str(config.get("outlookemail_web_password", "") or ""),
            session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
            proxies={},
        )
        detail.update(
            status="success",
            account_id=str(result.get("account_id") or detail.get("account_id") or ""),
            disabled_at=RegistrationRepository.now_text(),
            error="",
        )
        if log_callback:
            suffix = "（原本已停用）" if result.get("already_inactive") else ""
            log_callback(f"[+] OutlookEmail 邮箱已停用{suffix}: {normalized_email}")
    except Exception as exc:
        detail.update(status="failed", error=str(exc))
        if log_callback:
            log_callback(f"[!] OutlookEmail 邮箱停用失败，注册成功记录保留: {exc}")
    return detail


def maybe_disable_outlookemail_for_consumed_failure(
    kind,
    email,
    *,
    reason: str = "",
    log_callback=None,
) -> dict | None:
    """账号已注册时停用 Outlook 邮箱；邮箱已消耗，避免再次取用。"""
    if kind != FAIL_ALREADY_REGISTERED:
        return None
    if not is_outlookemail_registration() or not str(email or "").strip():
        return None
    label = FAIL_LABELS.get(kind, kind)
    fail_email = str(email).strip()
    if log_callback:
        log_callback(f"[*] {label}，按自动停用开关处理 Outlook 邮箱: {fail_email}")
    detail = disable_outlookemail_consumed(
        fail_email,
        reason=reason or label,
        log_callback=log_callback,
    )
    status = str((detail or {}).get("status") or "")
    if log_callback:
        if status == "success":
            log_callback(f"[+] {label}：Outlook 邮箱停用完成: {fail_email}")
        elif status == "feature_disabled":
            log_callback(f"[*] {label}：自动停用开关未开启，跳过停用 Outlook: {fail_email}")
        elif status == "failed":
            log_callback(
                f"[!] {label}：Outlook 邮箱停用失败: {fail_email}"
                f" — {(detail or {}).get('error') or ''}"
            )
    return detail


def disable_outlookemail_consumed(
    email,
    *,
    reason: str = "",
    log_callback=None,
) -> dict:
    """在账号已存在等邮箱已消耗场景下停用 Outlook 邮箱，避免重复取用。

    与注册成功后停用共用同一开关 ``outlookemail_disable_after_registration_success``：
    仅在开关开启且来源为 accounts 时才会远程停用。
    """
    if not is_outlookemail_registration():
        return default_email_disable_detail("", None)
    if get_outlookemail_source() != "accounts":
        return {
            "status": "unsupported_source",
            "account_id": "",
            "disabled_at": "",
            "error": "",
        }
    if not bool(config.get("outlookemail_disable_after_registration_success", False)):
        if log_callback:
            log_callback("[*] OutlookEmail 自动停用功能未开启，跳过停用")
        return {
            "status": "feature_disabled",
            "account_id": "",
            "disabled_at": "",
            "error": "",
        }

    normalized_email = str(email or "").strip()
    detail = {
        "status": "not_attempted",
        "account_id": "",
        "disabled_at": "",
        "error": "",
    }
    if not normalized_email:
        detail.update(status="failed", error="缺少邮箱地址")
        return detail
    try:
        account = outlookemail_provider.account_for_email(
            http_get,
            get_outlookemail_api_base(),
            get_outlookemail_api_key(),
            normalized_email,
            group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
        )
        detail["account_id"] = str(account.get("id") or "")
        reason_text = str(reason or "").strip()
        if log_callback:
            already = ("已注册" in reason_text) or ("already" in reason_text.lower()) or (
                "existing account" in reason_text.lower()
            )
            if already:
                log_callback(
                    f"[OutlookEmail] 账号已注册，正在停用 Outlook 邮箱: {normalized_email}"
                    f" (ID={detail['account_id'] or '-'})"
                )
                if reason_text:
                    log_callback(f"[OutlookEmail] 已注册详情: {reason_text}")
            else:
                suffix = f"（原因: {reason_text}）" if reason_text else ""
                log_callback(
                    f"[OutlookEmail] 正在停用已消耗邮箱 ID={detail['account_id'] or '-'}{suffix}"
                )
        result = outlookemail_provider.disable_account(
            http_get,
            direct_http_session,
            get_outlookemail_api_base(),
            normalized_email,
            api_key=get_outlookemail_api_key(),
            group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
            web_password=str(config.get("outlookemail_web_password", "") or ""),
            session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
            proxies={},
        )
        detail.update(
            status="success",
            account_id=str(result.get("account_id") or detail.get("account_id") or ""),
            disabled_at=RegistrationRepository.now_text(),
            error="",
        )
        if log_callback:
            extra = "（原本已停用）" if result.get("already_inactive") else ""
            already = ("已注册" in reason_text) or ("already" in reason_text.lower()) or (
                "existing account" in reason_text.lower()
            )
            if already:
                log_callback(
                    f"[+] 账号已注册场景：Outlook 邮箱已停用{extra}: {normalized_email}"
                )
            else:
                log_callback(f"[+] OutlookEmail 邮箱已停用{extra}: {normalized_email}")
    except Exception as exc:
        detail.update(status="failed", error=str(exc))
        if log_callback:
            already = ("已注册" in str(reason or "")) or ("already" in str(reason or "").lower())
            prefix = "账号已注册场景：" if already else ""
            log_callback(f"[!] {prefix}OutlookEmail 邮箱停用失败: {exc}")
    return detail


def outlookemail_get_email_and_token():
    return outlookemail_provider.acquire_email(
        http_get,
        direct_http_session,
        get_outlookemail_api_base(),
        api_key=get_outlookemail_api_key(),
        source=get_outlookemail_source(),
        group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
        web_password=str(config.get("outlookemail_web_password", "") or ""),
        session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
        temp_tag_ids=str(config.get("outlookemail_temp_tag_ids", "") or ""),
        pick_mode=str(config.get("outlookemail_pick_mode", "random") or "random"),
        proxies={},
        is_unavailable=_outlookemail_account_already_saved,
    )


def maybe_move_outlookemail_for_code_timeout(
    email,
    *,
    reason: str = "",
    log_callback=None,
) -> dict | None:
    """验证码超时把 Outlook 邮箱移到指定分组，不停用。

    只看 outlookemail_code_timeout_group_id：留空不移动，填了就移动。
    不复用取号分组 outlookemail_group_id。
    """
    if not is_outlookemail_registration() or not str(email or "").strip():
        return None
    if get_outlookemail_source() != "accounts":
        return None
    target = get_outlookemail_code_timeout_group_id()
    if not target:
        return None
    source = get_outlookemail_group_id()
    if source and outlookemail_provider.same_group_id(source, target):
        if log_callback:
            log_callback("[!] 验证码超时目标分组与取号分组相同，跳过移动")
        return {
            "status": "skipped_same_group",
            "account_id": "",
            "group_id": target,
            "error": "",
        }

    normalized_email = str(email).strip()
    detail = {
        "status": "not_attempted",
        "account_id": "",
        "group_id": target,
        "error": "",
    }
    try:
        if log_callback:
            suffix = f"（{reason}）" if str(reason or "").strip() else ""
            log_callback(
                f"[OutlookEmail] 验证码超时，正在把邮箱移到分组 {target}: {normalized_email}{suffix}"
            )
        result = outlookemail_provider.move_account_to_group(
            http_get,
            direct_http_session,
            get_outlookemail_api_base(),
            normalized_email,
            target,
            api_key=get_outlookemail_api_key(),
            group_id=source,
            web_password=str(config.get("outlookemail_web_password", "") or ""),
            session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
            proxies={},
        )
        detail.update(
            status="success",
            account_id=str(result.get("account_id") or ""),
            group_id=str(result.get("group_id") or target),
            error="",
        )
        if log_callback:
            extra = "（原本已在该分组）" if result.get("already_moved") else ""
            log_callback(
                f"[+] 验证码超时：Outlook 邮箱已移到分组 {detail['group_id']}{extra}: {normalized_email}"
            )
    except Exception as exc:
        detail.update(status="failed", error=str(exc))
        if log_callback:
            log_callback(f"[!] 验证码超时：Outlook 邮箱移动分组失败: {exc}")
    return detail


def outlookemail_get_oai_code(
    email,
    timeout=60,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    min_received_at=None,
):
    try:
        return outlookemail_provider.wait_for_code(
            http_get,
            direct_http_session,
            get_outlookemail_api_base(),
            email,
            api_key=get_outlookemail_api_key(),
            source=get_outlookemail_source(),
            web_password=str(config.get("outlookemail_web_password", "") or ""),
            session_cookie=str(config.get("outlookemail_session_cookie", "") or "").strip(),
            folder=str(config.get("outlookemail_folder", "all") or "all"),
            top=config.get("outlookemail_top", 10),
            proxies={},
            timeout=timeout,
            poll_interval=poll_interval,
            min_received_at=min_received_at,
            raise_if_cancelled=raise_if_cancelled,
            sleep_with_cancel=sleep_with_cancel,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    except RegistrationCancelled:
        raise
    except Exception as exc:
        if "未收到验证码" in str(exc):
            maybe_move_outlookemail_for_code_timeout(
                email,
                reason=str(exc),
                log_callback=log_callback,
            )
        raise


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    # 通用 HTTP 默认直连。只有 xAI/Grok 调用方可以显式传入 get_proxies()。
    request_kwargs["proxies"] = proxies or {}
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def _http_request(method, url, **kwargs):
    kwargs.pop("_allow_direct_fallback", None)
    with direct_http_session() as session:
        return session.request(method, url, **_build_request_kwargs(**kwargs))


def http_get(url, **kwargs):
    return _http_request("GET", url, **kwargs)


def http_post(url, **kwargs):
    return _http_request("POST", url, **kwargs)


def http_delete(url, **kwargs):
    return _http_request("DELETE", url, **kwargs)


def direct_http_session():
    """创建不读取项目代理或环境代理的 HTTP 会话。"""
    session = requests.Session(trust_env=False)
    raw_request = session.request

    def logged_request(method, url, *args, **kwargs):
        _log_actual_http_route(
            method,
            url,
            proxies=kwargs.get("proxies"),
            proxy=kwargs.get("proxy", ""),
        )
        return raw_request(method, url, *args, **kwargs)

    session.request = logged_request
    return session


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    return duckmail_provider.get_domains(
        http_get,
        get_duckmail_api_base(),
        api_key=api_key or get_duckmail_api_key(),
    )


def create_account(address, password, api_key=None, expires_in=0):
    return duckmail_provider.create_account(
        http_post,
        get_duckmail_api_base(),
        address,
        password,
        api_key=api_key or get_duckmail_api_key(),
        expires_in=expires_in,
    )


def get_token(address, password):
    return duckmail_provider.get_token(
        http_post,
        get_duckmail_api_base(),
        address,
        password,
    )


def get_messages(token):
    return duckmail_provider.get_messages(
        http_get,
        get_duckmail_api_base(),
        token,
    )


def get_message_detail(token, message_id):
    return duckmail_provider.get_message_detail(
        http_get,
        get_duckmail_api_base(),
        token,
        message_id,
    )



def cloudflare_get_domains(api_base, api_key=None):
    return cloudflare_provider.get_domains(
        http_get,
        api_base,
        domains_path=get_cloudflare_path("cloudflare_path_domains", "/domains"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    return cloudflare_provider.create_account(
        http_post,
        api_base,
        address,
        password,
        accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/accounts"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        expires_in=expires_in,
    )


def cloudflare_get_token(api_base, address, password, api_key=None):
    return cloudflare_provider.get_token(
        http_post,
        api_base,
        address,
        password,
        token_path=get_cloudflare_path("cloudflare_path_token", "/token"),
        api_key=api_key or get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_get_messages(api_base, token):
    return cloudflare_provider.get_messages(
        http_get,
        api_base,
        token,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


def cloudflare_get_message_detail(api_base, token, message_id):
    return cloudflare_provider.get_message_detail(
        http_get,
        api_base,
        token,
        message_id,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
    )


YYDS_API_BASE = yyds_provider.API_BASE


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def get_yyds_default_domain():
    return str(config.get("yyds_default_domain", "") or "").strip()


def yyds_get_domains(api_key=None, jwt=None):
    return yyds_provider.get_domains(http_get, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_create_account(local_part=None, domain=None, api_key=None, jwt=None):
    return yyds_provider.create_account(
        http_post,
        local_part=local_part or "",
        domain=domain or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_get_token(address, api_key=None, jwt=None):
    return yyds_provider.get_token(http_post, address, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    return yyds_provider.get_messages(
        http_get,
        address,
        token=token or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    return yyds_provider.get_message_detail(
        http_get,
        message_id,
        token=token or "",
        api_key=api_key or get_yyds_api_key(),
        jwt=jwt or get_yyds_jwt(),
    )


def yyds_generate_username(length=10):
    return yyds_provider.generate_username(length)


def yyds_pick_domain(api_key=None, jwt=None):
    return yyds_provider.pick_domain(http_get, api_key=api_key or get_yyds_api_key(), jwt=jwt or get_yyds_jwt())


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = get_yyds_default_domain() or yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        local_part=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(token, address, timeout=60, poll_interval=3, log_callback=None, jwt=None, cancel_callback=None):
    return yyds_provider.wait_for_code(
        http_get,
        token,
        address,
        timeout=timeout,
        poll_interval=poll_interval,
        jwt=jwt or get_yyds_jwt(),
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )



def generate_username(length=10):
    return _generate_username(length)


def pick_domain(api_key=None):
    return duckmail_provider.pick_domain(get_domains(api_key=api_key))


def get_cloudmail_url():
    return str(os.environ.get("CLOUDMAIL_URL") or config.get("cloudmail_url", "") or "").strip().rstrip("/")


def get_cloudmail_admin_email():
    return str(os.environ.get("CLOUDMAIL_ADMIN_EMAIL") or config.get("cloudmail_admin_email", "") or "").strip()


def get_cloudmail_password():
    return str(os.environ.get("CLOUDMAIL_PASSWORD") or config.get("cloudmail_password", "") or "")


def cloudmail_get_email_and_token():
    raw_domains = str(config.get("defaultDomains", "") or "")
    domains = [item.strip() for item in re.split(r"[,，\s]+", raw_domains) if item.strip()]
    return cloudmail_provider.create_mailbox(
        http_post,
        get_cloudmail_url(),
        get_cloudmail_admin_email(),
        get_cloudmail_password(),
        domains,
        username=generate_username(10),
    )


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=60,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    del dev_token
    return cloudmail_provider.wait_for_code(
        http_post,
        http_delete,
        get_cloudmail_url(),
        get_cloudmail_admin_email(),
        get_cloudmail_password(),
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=resend_callback,
    )


def get_email_provider():
    return config.get("email_provider", "cloudflare")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "outlookemail":
        return outlookemail_get_email_and_token()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        return cloudmail_get_email_and_token()
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            create_path = get_cloudflare_path("cloudflare_path_accounts", "/admin/new_address")
            try:
                return cloudflare_provider.create_mailbox_fallback(
                    http_get,
                    http_post,
                    api_base,
                    domains_path=get_cloudflare_path("cloudflare_path_domains", "/domains"),
                    accounts_path=get_cloudflare_path("cloudflare_path_accounts", "/accounts"),
                    token_path=get_cloudflare_path("cloudflare_path_token", "/token"),
                    api_key=api_key or get_cloudflare_api_key(),
                    auth_mode=get_cloudflare_auth_mode(),
                    custom_auth=get_cloudflare_custom_auth(),
                )
            except Exception as fallback_exc:
                raise Exception(
                    "Cloudflare 创建邮箱失败；"
                    f"主接口 {create_path}: "
                    f"{primary_exc.__class__.__name__}: {primary_exc}；"
                    f"兼容回退: "
                    f"{fallback_exc.__class__.__name__}: {fallback_exc}"
                ) from fallback_exc
    if provider == "mailnest":
        return mailnest_buy_email(), "_"
    return duckmail_provider.create_mailbox(
        http_get,
        http_post,
        get_duckmail_api_base(),
        api_key=api_key or get_duckmail_api_key(),
        expires_in=0,
    )


def get_oai_code(
    dev_token,
    email,
    timeout=60,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
    min_received_at=None,
):
    provider = get_email_provider()
    if provider == "outlookemail":
        return outlookemail_get_oai_code(
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            min_received_at=min_received_at,
        )
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "mailnest":
        return mailnest_get_code(
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )



def extract_verification_code(text, subject=""):
    return _extract_code(text, subject)


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=60,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    return duckmail_provider.wait_for_code(
        http_get,
        get_duckmail_api_base(),
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        extract_code=extract_verification_code,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=60,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    return cloudflare_provider.wait_for_code(
        http_get,
        get_cloudflare_api_base(),
        dev_token,
        email,
        messages_path=get_cloudflare_path("cloudflare_path_messages", "/messages"),
        api_key=get_cloudflare_api_key(),
        auth_mode=get_cloudflare_auth_mode(),
        custom_auth=get_cloudflare_custom_auth(),
        timeout=timeout,
        poll_interval=poll_interval,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=resend_callback,
    )


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    """安全预览 HTTP 响应体；gRPC/二进制内容不直接当文本打印。"""
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(getattr(res, "headers", {}) or {}).items()}
        content_type = headers.get("content-type", "")
        raw = getattr(res, "content", None)
        if raw is None:
            try:
                raw = (res.text or "").encode("utf-8", errors="replace")
            except Exception:
                raw = b""
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(raw).encode("utf-8", errors="replace")
        raw = bytes(raw)

        # gRPC / protobuf 常见 content-type 或正文以不可打印字节为主
        is_binaryish = (
            "grpc" in content_type
            or "protobuf" in content_type
            or "octet-stream" in content_type
            or (raw[:1] in (b"\x00", b"\x01") and b"grpc-status" in raw)
        )
        if is_binaryish or (raw and sum(1 for b in raw[:64] if b < 9 or (13 < b < 32)) > 8):
            # 尽量抽出可读的 trailer 片段（如 grpc-status:0）
            readable = re.findall(rb"[ -~]{3,}", raw)
            text = " ".join(part.decode("ascii", errors="ignore") for part in readable)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                text = f"<binary {len(raw)} bytes>"
            return text[:limit]

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception:
        return ""


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def is_debug_mode():
    return bool(config.get("debug_mode", False))


def is_browser_headless():
    force_headed = str(os.environ.get("GROK_FORCE_HEADED", "") or "").strip().lower()
    if force_headed in {"1", "true", "yes", "on"}:
        return False
    return bool(config.get("browser_headless", False))


def get_browser_engine() -> str:
    return _bs.normalize_browser_engine(config.get("browser_engine", "camoufox"))


def get_browser_locale() -> str:
    value = str(config.get("browser_locale", "en-US") or "en-US").strip()
    return value if value in {"en-US", "zh-CN"} else "en-US"


def is_browser_low_traffic_mode() -> bool:
    return bool(config.get("browser_low_traffic_mode", False))


def get_browser_traffic_savings_level() -> str:
    value = str(config.get("browser_traffic_savings_level") or "standard").strip().lower()
    if value in {"more", "max"}:
        return "more"
    return "standard"


def should_close_browser_after_run(user_stopped: bool) -> bool:
    """正常结束时非调试模式关闭；手动停止时严格以勾选项为准。"""
    if user_stopped:
        return bool(config.get("close_browser_on_stop", False))
    return not is_debug_mode()


def maybe_stop_browser(user_stopped: bool = False, log_callback=None):
    if should_close_browser_after_run(user_stopped):
        # 手动勾选关闭时应优先于调试模式，因此这里显式 force。
        stop_browser(force=True)
        if log_callback:
            reason = "用户停止" if user_stopped else "任务结束"
            log_callback(f"[*] {reason}：已执行浏览器关闭")
        return
    if log_callback:
        if user_stopped:
            log_callback("[*] 用户停止：按当前勾选设置保留浏览器")
        else:
            log_callback("[*] 调试模式：正常结束后保留浏览器")


def get_log_level() -> str:
    level = str(config.get("log_level", "info") or "info").strip().lower()
    return level if level in ("info", "debug") else "info"


def should_emit_log(message: str) -> bool:
    """info 级别过滤 [Debug] 行；debug 全开。"""
    if get_log_level() == "debug":
        return True
    text = str(message or "")
    if text.lstrip().startswith("[Debug]") or " [Debug] " in text:
        return False
    return True


def _wire_runtime_modules():
    """向浏览器运行时和页面流程注入本次任务依赖。"""
    _bs.configure(
        get_proxies=get_proxies,
        is_debug=is_debug_mode,
        is_headless=is_browser_headless,
        get_locale=get_browser_locale,
        get_engine=get_browser_engine,
        is_low_traffic=is_browser_low_traffic_mode,
        get_traffic_savings_level=get_browser_traffic_savings_level,
        extension_path=EXTENSION_PATH,
    )
    _rf.configure(
        get_email_and_token=get_email_and_token,
        get_oai_code=get_oai_code,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        RegistrationCancelled=RegistrationCancelled,
        EmailDomainRejected=EmailDomainRejected,
        AccountRetryNeeded=AccountRetryNeeded,
        email_unavailable=email_registered_successfully,
        prepare_exit_ip=prepare_registration_exit_ip,
    )

# 页面步骤由 registration.signup_flow 实现。

class RegistrationStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


def registration_log(message):
    if not should_emit_log(message):
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)


def run_registration(count, flow_adapter=None):
    """执行共享注册编排；flow_adapter 只替换目标站点的页面步骤。"""
    flow_open_signup_page = getattr(flow_adapter, "open_signup_page", open_signup_page)
    flow_fill_email_and_submit = getattr(
        flow_adapter, "fill_email_and_submit", fill_email_and_submit
    )
    flow_fill_code_and_submit = getattr(
        flow_adapter, "fill_code_and_submit", fill_code_and_submit
    )
    flow_fill_profile_and_submit = getattr(
        flow_adapter, "fill_profile_and_submit", fill_profile_and_submit
    )
    flow_wait_for_completion = getattr(
        flow_adapter, "wait_for_registration_completion", wait_for_registration_completion
    )
    flow_account_file = getattr(flow_adapter, "account_file_for_email", account_file_for_email)
    flow_last_email = getattr(flow_adapter, "last_acquired_email", _rf.last_acquired_email)
    flow_last_profile = getattr(flow_adapter, "last_profile", _rf.last_profile)
    flow_failure_classifier = getattr(flow_adapter, "classify_failure", None)
    failure_labels = dict(FAIL_LABELS)
    failure_labels.update(dict(getattr(flow_adapter, "FAILURE_LABELS", {}) or {}))
    target_label = str(getattr(flow_adapter, "target_label", "xAI / Grok") or "xAI / Grok")
    result_source = str(getattr(flow_adapter, "result_source", "web") or "web")
    result_platform = str(getattr(flow_adapter, "result_platform", "xai") or "xai")
    forced_workers = getattr(flow_adapter, "force_workers", None)
    connectivity_runner = getattr(
        flow_adapter, "run_connectivity_checks", _conn.run_connectivity_checks
    )
    blocking_connectivity_failure = getattr(
        flow_adapter, "has_blocking_connectivity_failure", _conn.has_blocking_xai_failure
    )
    blocked_connectivity_message = str(
        getattr(
            flow_adapter,
            "blocked_connectivity_message",
            "xAI 注册页被 Cloudflare 拦截，已停止建号；请更换当前 proxy 后重试",
        )
    )

    def _classify(exc):
        if callable(flow_failure_classifier):
            custom = str(flow_failure_classifier(exc) or "").strip()
            if custom:
                return custom
        return classify_failure(exc)

    def _attempt_email(email="", exc=None):
        return str(
            getattr(exc, "email", "")
            or email
            or flow_last_email()
            or ""
        ).strip()

    def _attempt_password(profile=None):
        current = dict(profile or {})
        if current.get("password"):
            return str(current.get("password") or "")
        return str(flow_last_profile().get("password") or "")

    controller = RegistrationStopController()
    reset_network_route_logs()
    if get_email_provider() == "outlookemail":
        reset_outlookemail_runtime_state()

    success_count = 0
    fail_count = 0
    fail_stats = empty_fail_stats()
    batch_source = str(getattr(flow_adapter, "batch_source", "web") or "web")
    batch_id = new_registration_batch_id(batch_source)
    retry_count_for_slot = 0
    max_slot_retry = 3
    accounts_output_file = ""  # 已改为按邮箱单独保存，不再使用批量文件
    workers = max(1, min(int(config.get("register_workers", 1) or 1), 8, int(count or 1)))
    if forced_workers is not None:
        workers = max(1, min(int(forced_workers or 1), 8, int(count or 1)))
    registration_log(f"[*] Web 任务启动，目标数量: {count} | 并发: {workers}")
    _interval_raw = str(config.get("account_interval", "0") or "0").strip()
    if _interval_raw and _interval_raw != "0":
        registration_log(f"[*] 账号间注册间隔: {_interval_raw}s")
    registration_log(f"[*] {target_label} 注册模式：仅保存账号邮箱和密码")
    traceback_log_lock = threading.Lock()
    logged_traceback_signatures = set()
    # 启动前清理上次崩溃 / 强杀残留的临时 profile 目录
    try:
        _cleanup_stale_profiles(log_callback=registration_log)
    except Exception:
        pass
    try:
        startup_checks = connectivity_runner(config, http_get, http_post)
        for name, ok, detail in startup_checks:
            registration_log(f"[检查] [{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if blocking_connectivity_failure(startup_checks):
            registration_log(f"[!] {blocked_connectivity_message}")
            return
    except Exception as exc:
        registration_log(f"[!] 启动连通性检查异常，继续注册: {exc}")

    def _record_failure(exc):
        nonlocal fail_count
        kind = _classify(exc)
        fail_count += 1
        fail_stats[kind] = fail_stats.get(kind, 0) + 1
        return kind

    def _persist_result(*, started_at, worker_id=0, **kwargs):
        extra = dict(kwargs.get("extra") or {})
        exit_ip = _exit_ip.current_exit_ip()
        start_exit_ip = _exit_ip.current_start_exit_ip()
        if exit_ip:
            extra["exit_ip"] = exit_ip
        if start_exit_ip and start_exit_ip != exit_ip:
            extra["exit_ip_at_start"] = start_exit_ip
        trace_text = ""
        if str(kwargs.get("status") or "").strip().lower() == "failure":
            trace_text = current_exception_traceback()
            if trace_text:
                extra["exception_traceback"] = trace_text
                extra["exception_type"] = trace_text.rstrip().splitlines()[-1]
                signature = hash(trace_text)
                with traceback_log_lock:
                    should_log_traceback = signature not in logged_traceback_signatures
                    if should_log_traceback:
                        logged_traceback_signatures.add(signature)
                if should_log_traceback:
                    registration_log(
                        "[异常堆栈]\n"
                        + current_exception_traceback(TRACEBACK_LOG_MAX_CHARS)
                    )
        if extra:
            kwargs["extra"] = extra
        if (
            str(kwargs.get("status") or "").strip().lower() == "failure"
            and not kwargs.get("screenshot_path")
        ):
            kwargs["screenshot_path"] = capture_failure_screenshot(
                batch_id=batch_id,
                worker_id=worker_id,
                email=str(kwargs.get("email") or ""),
                failure_type=str(kwargs.get("failure_type") or FAIL_OTHER),
                log_callback=registration_log,
            )
        return persist_registration_result(
            batch_id=batch_id,
            source=result_source,
            platform=result_platform,
            started_at=started_at,
            provider=str(config.get("email_provider", "") or ""),
            worker_id=int(worker_id) + 1,
            log_callback=registration_log,
            **kwargs,
        )

    if workers > 1:
        # Web 并发：多线程，每线程独立浏览器（thread-local）
        stats_lock = threading.Lock()
        accounts_lock = threading.Lock()
        base, rem = divmod(count, workers)
        chunks = [base + (1 if i < rem else 0) for i in range(workers)]
        threads = []
        shared = {"success": 0, "fail": 0, "fail_stats": empty_fail_stats()}

        def worker(n, wid):
            local_success = 0
            local_fail = 0
            local_fail_stats = empty_fail_stats()
            try:
                boot_started_at = time.time()
                try:
                    start_browser(
                        log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                        cancel_callback=controller.should_stop,
                    )
                except Exception as boot_exc:
                    if controller.should_stop():
                        registration_log(f"[W{wid+1}] [!] 已停止，浏览器启动中断: {boot_exc}")
                        return
                    local_fail = n
                    local_fail_stats[FAIL_BROWSER] = local_fail_stats.get(FAIL_BROWSER, 0) + n
                    registration_log(f"[W{wid+1}] [-] 浏览器启动失败，{n} 个任务均记为失败: {boot_exc}")
                    for _ in range(max(int(n or 0), 0)):
                        _persist_result(
                            started_at=boot_started_at,
                            worker_id=wid,
                            status="failure",
                            failure_type=FAIL_BROWSER,
                            failure_reason=str(boot_exc),
                        )
                    return
                i = 0
                retry = 0
                while i < n and not controller.should_stop():
                    attempt_started_at = time.time()
                    email = ""
                    profile = {}
                    email_file = ""
                    try:
                        flow_open_signup_page(
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        email, dev_token, submitted_at = flow_fill_email_and_submit(
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        code = flow_fill_code_and_submit(
                            email,
                            dev_token,
                            submitted_at=submitted_at,
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        profile = flow_fill_profile_and_submit(
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        flow_wait_for_completion(
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            cancel_callback=controller.should_stop,
                        )
                        line = f"{email}----{profile.get('password','')}\n"
                        try:
                            with accounts_lock:
                                # 以邮箱命名单独保存
                                email_file = flow_account_file(email)
                                with open(email_file, "w", encoding="utf-8") as f:
                                    f.write(line)
                        except Exception as file_exc:
                            registration_log(
                                f"[W{wid+1}] [!] 保存账号文件失败，当前账号不计为成功: {file_exc}"
                            )
                            raise RuntimeError(f"保存账号文件失败: {file_exc}") from file_exc
                        email_disable_detail = (
                            disable_outlookemail_after_registration_success(
                                email,
                                log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                            )
                            if is_outlookemail_registration()
                            else default_email_disable_detail("")
                        )
                        local_success += 1
                        i += 1
                        retry = 0
                        registration_log(f"[W{wid+1}] [+] 注册成功: {email}")
                        _persist_result(
                            started_at=attempt_started_at,
                            worker_id=wid,
                            email=email,
                            password=_attempt_password(profile),
                            status="success",
                            email_disable_detail=email_disable_detail,
                            account_file=email_file,
                            extra={"任务序号": i, "并发数": workers},
                        )
                    except RegistrationCancelled:
                        cancelled_email = _attempt_email(email)
                        if cancelled_email:
                            _persist_result(
                                started_at=attempt_started_at,
                                worker_id=wid,
                                email=cancelled_email,
                                password=_attempt_password(profile),
                                status="cancelled",
                                failure_reason="用户停止注册",
                                account_file=email_file,
                            )
                        break
                    except EmailDomainRejected as exc:
                        kind = _classify(exc)
                        local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                        local_fail += 1
                        i += 1
                        retry = 0
                        _persist_result(
                            started_at=attempt_started_at,
                            worker_id=wid,
                            email=_attempt_email(email, exc),
                            password=_attempt_password(profile),
                            status="failure",
                            failure_type=kind,
                            failure_reason=str(exc),
                        )
                        registration_log(f"[W{wid+1}] [-] 域名拒绝: {exc}")
                    except AccountRetryNeeded as exc:
                        retry += 1
                        if retry > max_slot_retry:
                            retry_used = retry
                            kind = _classify(exc)
                            local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                            local_fail += 1
                            i += 1
                            retry = 0
                            _persist_result(
                                started_at=attempt_started_at,
                                worker_id=wid,
                                email=_attempt_email(email, exc),
                                password=_attempt_password(profile),
                                status="failure",
                                failure_type=kind,
                                failure_reason=str(exc),
                                account_file=email_file,
                                extra={"重试次数": retry_used},
                            )
                            registration_log(f"[W{wid+1}] [-] 卡住跳过: {exc}")
                    except Exception as exc:
                        kind = _classify(exc)
                        local_fail_stats[kind] = local_fail_stats.get(kind, 0) + 1
                        local_fail += 1
                        i += 1
                        retry = 0
                        fail_email = _attempt_email(email, exc)
                        email_disable_detail = maybe_disable_outlookemail_for_consumed_failure(
                            kind,
                            fail_email,
                            reason=f"{failure_labels.get(kind, kind)}: {exc}",
                            log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                        )
                        _persist_result(
                            started_at=attempt_started_at,
                            worker_id=wid,
                            email=fail_email,
                            password=_attempt_password(profile),
                            status="failure",
                            email_disable_detail=email_disable_detail,
                            failure_type=kind,
                            failure_reason=str(exc),
                            account_file=email_file,
                        )
                        registration_log(f"[W{wid+1}] [-] 失败 [{failure_labels.get(kind, kind)}]: {exc}")
                    finally:
                        if i < n and not controller.should_stop():
                            try:
                                stop_browser()
                                time.sleep(0.3)
                            except Exception:
                                pass
            finally:
                try:
                    maybe_stop_browser(
                        user_stopped=bool(controller.should_stop()),
                        log_callback=lambda m: registration_log(f"[W{wid+1}] {m}"),
                    )
                except Exception:
                    pass
                with stats_lock:
                    shared["success"] += local_success
                    shared["fail"] += local_fail
                    for k, v in local_fail_stats.items():
                        shared["fail_stats"][k] = shared["fail_stats"].get(k, 0) + v

        for wid, n in enumerate(chunks):
            if n <= 0:
                continue
            t = threading.Thread(target=worker, args=(n, wid), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        success_count = shared["success"]
        fail_count = shared["fail"]
        fail_stats = shared["fail_stats"]
        registration_log(
            f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}"
            + (f" | {format_fail_stats(fail_stats, failure_labels)}" if fail_count else "")
        )
        return

    try:
        boot_started_at = time.time()
        try:
            start_browser(log_callback=registration_log, cancel_callback=controller.should_stop)
        except Exception as boot_exc:
            if controller.should_stop():
                registration_log(f"[!] 已停止，浏览器启动中断: {boot_exc}")
                return
            fail_count += count
            fail_stats[FAIL_BROWSER] = fail_stats.get(FAIL_BROWSER, 0) + count
            registration_log(f"[-] 浏览器启动失败，{count} 个任务均记为失败: {boot_exc}")
            for _ in range(max(int(count or 0), 0)):
                _persist_result(
                    started_at=boot_started_at,
                    status="failure",
                    failure_type=FAIL_BROWSER,
                    failure_reason=str(boot_exc),
                )
            return
        registration_log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if controller.should_stop():
                break
            registration_log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
            attempt_started_at = time.time()
            email = ""
            profile = {}
            email_file = ""
            try:
                dev_token = ""
                code = ""
                mail_ok = False
                max_mail_retry = 3
                for mail_try in range(1, max_mail_retry + 1):
                    mail_attempt_started_at = time.time()
                    registration_log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                    flow_open_signup_page(
                        log_callback=registration_log, cancel_callback=controller.should_stop
                    )
                    registration_log("[*] 2. 创建邮箱并提交")
                    email, dev_token, submitted_at = flow_fill_email_and_submit(
                        log_callback=registration_log, cancel_callback=controller.should_stop
                    )
                    registration_log(f"[*] 邮箱: {email}")
                    registration_log("[Debug] 邮箱验证码会话已建立")
                    registration_log("[*] 3. 拉取验证码")
                    try:
                        code = flow_fill_code_and_submit(
                            email,
                            dev_token,
                            submitted_at=submitted_at,
                            log_callback=registration_log,
                            cancel_callback=controller.should_stop,
                        )
                        mail_ok = True
                        break
                    except Exception as mail_exc:
                        msg = str(mail_exc)
                        if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                            _persist_result(
                                started_at=mail_attempt_started_at,
                                email=email,
                                status="failure",
                                failure_type=_classify(mail_exc),
                                failure_reason=str(mail_exc),
                                extra={"邮箱已更换重试": True, "邮箱尝试次数": mail_try},
                            )
                            registration_log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                            restart_browser(log_callback=registration_log, cancel_callback=controller.should_stop)
                            sleep_with_cancel(1, controller.should_stop)
                            continue
                        raise

                if not mail_ok:
                    raise Exception("验证码阶段失败，已达到最大重试次数")
                registration_log(f"[*] 验证码: {code}")
                registration_log("[*] 4. 填写资料")
                profile = flow_fill_profile_and_submit(
                    log_callback=registration_log, cancel_callback=controller.should_stop
                )
                registration_log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                registration_log("[*] 5. 确认注册完成")
                flow_wait_for_completion(
                    log_callback=registration_log, cancel_callback=controller.should_stop
                )
                try:
                    line = f"{email}----{profile.get('password','')}\n"
                    # 以邮箱命名单独保存
                    email_file = flow_account_file(email)
                    with open(email_file, "w", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    registration_log(f"[!] 保存账号文件失败，当前账号不计为成功: {file_exc}")
                    raise RuntimeError(f"保存账号文件失败: {file_exc}") from file_exc
                email_disable_detail = (
                    disable_outlookemail_after_registration_success(
                        email, log_callback=registration_log
                    )
                    if is_outlookemail_registration()
                    else default_email_disable_detail("")
                )
                success_count += 1
                counted_success = True
                retry_count_for_slot = 0
                i += 1
                registration_log(f"[+] 注册成功: {email}")
                _persist_result(
                    started_at=attempt_started_at,
                    email=email,
                    password=_attempt_password(profile),
                    status="success",
                    email_disable_detail=email_disable_detail,
                    account_file=email_file,
                    extra={"任务序号": i, "并发数": 1},
                )
                registration_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                if (
                    counted_success
                    and success_count > 0
                    and success_count % MEMORY_CLEANUP_INTERVAL == 0
                    and i < count
                ):
                    cleanup_runtime_memory(
                        log_callback=registration_log,
                        reason=f"已成功 {success_count} 个账号，执行定期清理",
                    )
            except RegistrationCancelled:
                cancelled_email = _attempt_email(email)
                if cancelled_email:
                    _persist_result(
                        started_at=attempt_started_at,
                        email=cancelled_email,
                        password=_attempt_password(profile),
                        status="cancelled",
                        failure_reason="用户停止注册",
                        account_file=email_file,
                    )
                registration_log("[!] 注册被停止")
                break
            except EmailDomainRejected as exc:
                kind = _record_failure(exc)
                retry_count_for_slot = 0
                i += 1
                _persist_result(
                    started_at=attempt_started_at,
                    email=_attempt_email(email, exc),
                    password=_attempt_password(profile),
                    status="failure",
                    failure_type=kind,
                    failure_reason=str(exc),
                )
                registration_log(f"[-] 邮箱域名被 {target_label} 拒绝 [{failure_labels.get(kind, kind)}]: {exc}")
                registration_log("[!] 请更换邮箱提供商或域名（如 Cloudflare 自建域 / MailNest），公共临时域常被拉黑")
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    registration_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    retry_used = retry_count_for_slot
                    kind = _record_failure(exc)
                    retry_count_for_slot = 0
                    i += 1
                    _persist_result(
                        started_at=attempt_started_at,
                        email=_attempt_email(email, exc),
                        password=_attempt_password(profile),
                        status="failure",
                        failure_type=kind,
                        failure_reason=str(exc),
                        account_file=email_file,
                        extra={"重试次数": retry_used},
                    )
                    registration_log(f"[-] 当前账号已达到最大重试次数，跳过 [{failure_labels.get(kind, kind)}]: {exc}")
            except Exception as exc:
                kind = _record_failure(exc)
                retry_count_for_slot = 0
                i += 1
                fail_email = _attempt_email(email, exc)
                email_disable_detail = maybe_disable_outlookemail_for_consumed_failure(
                    kind,
                    fail_email,
                    reason=f"{failure_labels.get(kind, kind)}: {exc}",
                    log_callback=registration_log,
                )
                _persist_result(
                    started_at=attempt_started_at,
                    email=fail_email,
                    password=_attempt_password(profile),
                    status="failure",
                    email_disable_detail=email_disable_detail,
                    failure_type=kind,
                    failure_reason=str(exc),
                    account_file=email_file,
                )
                registration_log(f"[-] 注册失败 [{failure_labels.get(kind, kind)}]: {exc}")
            if controller.should_stop():
                break
            # 每轮结束只关浏览器；下一轮按需重新启动并导航到官网。
            if i < count:
                wait_sec = parse_account_interval()
                if wait_sec > 0:
                    registration_log(f"[*] 下一个账号前等待 {wait_sec:.0f} 秒...")
                    sleep_with_cancel(wait_sec, controller.should_stop)
                try:
                    stop_browser()
                    time.sleep(0.5)
                except RegistrationCancelled:
                    break
                except Exception as close_exc:
                    if controller.should_stop():
                        break
                    registration_log(f"[Debug] 轮次关闭浏览器失败: {close_exc}")
    except RegistrationCancelled:
        registration_log("[!] 注册被停止")
    except Exception as exc:
        registration_log(f"[!] 任务异常: {exc}")
    finally:
        try:
            user_stopped = bool(controller.should_stop())
            if user_stopped:
                maybe_stop_browser(user_stopped=True, log_callback=registration_log)
            else:
                cleanup_runtime_memory(log_callback=registration_log, reason="任务结束")
        except BaseException:
            pass
        try:
            registration_log(
                f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}"
                + (f" | {format_fail_stats(fail_stats, failure_labels)}" if fail_count else "")
            )
        except BaseException:
            pass
