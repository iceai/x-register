"""X.com（原 Twitter）注册页面适配器。

注册编排、邮箱与验证码、重试、浏览器生命周期、结果验证和落库全部使用
Grok 注册引擎；本模块只描述 X.com 官方页面的控件差异。若站点要求手机号
或人工挑战，会停止当前账号并保留失败记录，不尝试规避平台验证。
"""
from __future__ import annotations

import datetime
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.registration import engine
from backend.registration.signup_flow import AccountAlreadyRegistered, build_profile


SIGNUP_URL = "https://x.com/i/flow/signup"
DEFAULT_TIMEOUT = 45
target_label = "X.com"
result_source = "x_account"
result_platform = "x"
batch_source = "x-account-web"
force_workers = 1
FAILURE_LABELS = {
    "manual_challenge": "人工安全验证",
    "phone_required": "需要手机号",
}

_runtime = threading.local()


class XAccountRegistrationError(RuntimeError):
    pass


class ManualChallengeRequired(XAccountRegistrationError):
    pass


class PhoneVerificationRequired(XAccountRegistrationError):
    pass


def _cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel and cancel():
        raise engine.RegistrationCancelled("用户停止注册")


def _visible(locator: Any) -> bool:
    try:
        return bool(locator.count()) and bool(locator.first.is_visible())
    except Exception:
        return False


def _find(page: Any, selectors: Iterable[str]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if _visible(locator):
                return locator.first
        except Exception:
            continue
    return None


def _challenge_reason(page: Any) -> str:
    challenge = _find(
        page,
        (
            "iframe[src*='arkose']",
            "iframe[src*='captcha']",
            "[data-testid='arkoseFrame']",
            "text=/authenticate your account|证明你不是机器人|验证你的身份/i",
        ),
    )
    return "X.com 要求完成人工安全验证" if challenge is not None else ""


def _wait_for(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cancel: Callable[[], bool] | None = None,
) -> Any:
    deadline = time.time() + max(float(timeout), 1.0)
    while time.time() < deadline:
        _cancelled(cancel)
        reason = _challenge_reason(page)
        if reason:
            raise ManualChallengeRequired(reason)
        locator = _find(page, selectors)
        if locator is not None:
            return locator
        time.sleep(0.25)
    raise XAccountRegistrationError("等待 X.com 注册页面控件超时")


def _click(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = 12,
    required: bool = True,
    cancel: Callable[[], bool] | None = None,
) -> bool:
    try:
        locator = _wait_for(page, selectors, timeout=timeout, cancel=cancel)
    except ManualChallengeRequired:
        # 可选按钮不存在时可以继续，但人工挑战绝不能被“可选”逻辑吞掉。
        raise
    except XAccountRegistrationError:
        if required:
            raise
        return False
    locator.click(timeout=max(1, int(timeout * 1000)))
    return True


def _fill(locator: Any, value: str) -> None:
    locator.fill("")
    locator.press_sequentially(str(value), delay=random.randint(35, 80))


def _raw_page(log: Callable[[str], None], cancel: Callable[[], bool]) -> Any:
    _cancelled(cancel)
    if engine._active_browser() is None:
        engine.start_browser(log_callback=log, cancel_callback=cancel)
    adapter = engine._active_page()
    if adapter is None:
        raise XAccountRegistrationError("浏览器页面未就绪")
    return adapter.raw_page


def _set_birthdate(page: Any) -> str:
    today = datetime.date.today()
    year = today.year - random.randint(20, 38)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    selects = page.locator("select")
    if selects.count() < 3:
        raise XAccountRegistrationError("X.com 出生日期控件未出现")
    choices = (
        (str(month), month),
        (str(day), day),
        (str(year), None),
    )
    for index, (value, fallback_index) in enumerate(choices):
        select = selects.nth(index)
        try:
            select.select_option(value=value)
        except Exception:
            try:
                select.select_option(label=value)
            except Exception:
                if fallback_index is None:
                    raise XAccountRegistrationError("无法选择 X.com 出生年份")
                # 月份可能使用 January 等本地化文本，日期下拉首项是占位符。
                select.select_option(index=fallback_index)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _next(page: Any, *, required: bool = True, cancel: Callable[[], bool]) -> bool:
    return _click(
        page,
        (
            "[data-testid='ocfEnterTextNextButton']",
            "[data-testid='ocfSignupReviewNextButton']",
            "button:has-text('Next')",
            "[role='button']:has-text('Next')",
            "button:has-text('下一步')",
            "[role='button']:has-text('下一步')",
        ),
        timeout=10,
        required=required,
        cancel=cancel,
    )


def _prepare_signup_form(page: Any, log: Callable[[str], None], cancel: Callable[[], bool]) -> None:
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45_000)
    log("[*] 已打开 X.com 官方注册页")
    if _find(page, ("input[name='name']", "input[autocomplete='name']")) is None:
        _click(
            page,
            (
                "[data-testid='ocfSignupButton']",
                "a[href='/i/flow/signup']",
                "[role='button']:has-text('Create account')",
                "[role='button']:has-text('创建账号')",
            ),
            timeout=20,
            cancel=cancel,
        )
    _wait_for(page, ("input[name='name']", "input[autocomplete='name']"), cancel=cancel)


def last_acquired_email() -> str:
    return str(getattr(_runtime, "last_email", "") or "")


def last_profile() -> dict[str, str]:
    return dict(getattr(_runtime, "last_profile", {}) or {})


def classify_failure(exc: BaseException) -> str:
    if isinstance(exc, ManualChallengeRequired):
        return "manual_challenge"
    if isinstance(exc, PhoneVerificationRequired):
        return "phone_required"
    if isinstance(exc, XAccountRegistrationError):
        if "验证码" in str(exc):
            return ""
        return engine.FAIL_STUCK
    return ""


def run_connectivity_checks(config: dict, http_get: Callable, http_post: Callable):
    return engine._conn.run_x_account_connectivity_checks(config, http_get, http_post)


def has_blocking_connectivity_failure(results: list[tuple[str, bool, str]]) -> bool:
    return any(
        name == engine._conn.X_SIGNUP_CHECK_NAME and not ok
        for name, ok, _ in results
    )


blocked_connectivity_message = "X.com 注册页无法访问，已停止建号；请检查当前 proxy 后重试"


def open_signup_page(log_callback=None, cancel_callback=None) -> None:
    log = log_callback or (lambda _message: None)
    cancel = cancel_callback or (lambda: False)
    _cancelled(cancel)
    _runtime.last_email = ""
    _runtime.last_profile = {}
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            if attempt > 1:
                engine.restart_browser(log_callback=log, cancel_callback=cancel)
            elif engine._active_browser() is None:
                engine.start_browser(log_callback=log, cancel_callback=cancel)
                log("[*] 浏览器已启动")
            engine.prepare_registration_exit_ip(log_callback=log)
            page = _raw_page(log, cancel)
            _prepare_signup_form(page, log, cancel)
            return
        except engine.RegistrationCancelled:
            raise
        except (ManualChallengeRequired, PhoneVerificationRequired):
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                log(f"[!] 打开 X.com 注册页失败，重启浏览器后重试 ({attempt + 1}/3): {exc}")
    raise XAccountRegistrationError(f"打开 X.com 注册页失败: {last_error or 'unknown'}")


def _new_available_email(log: Callable[[str], None], cancel: Callable[[], bool]):
    for candidate_index in range(1, 51):
        _cancelled(cancel)
        email, mail_token = engine.get_email_and_token()
        if not email or not mail_token:
            raise XAccountRegistrationError("获取邮箱失败")
        if not engine.email_registered_successfully(email):
            _runtime.last_email = str(email)
            return str(email), str(mail_token)
        log(f"[*] 邮箱已注册或已消耗，已跳过并获取下一个 ({candidate_index}/50): {email}")
    raise XAccountRegistrationError(
        "连续获取到已注册/已消耗邮箱，已达到更换上限；请补充新邮箱或停用已占用账号"
    )


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1000) or "")
    except Exception:
        return ""


def _raise_if_email_rejected(page: Any, email: str) -> None:
    text = _body_text(page).lower()
    already_used_markers = (
        "email has already been taken",
        "email is already in use",
        "邮箱已被使用",
    )
    if any(marker in text for marker in already_used_markers):
        raise AccountAlreadyRegistered(f"X.com 邮箱已注册: {email}")
    rejected_markers = (
        "invalid email",
        "unsupported email",
        "电子邮件地址无效",
    )
    if any(marker in text for marker in rejected_markers):
        raise engine.EmailDomainRejected(email, "X.com 拒绝当前邮箱")


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    log = log_callback or (lambda _message: None)
    cancel = cancel_callback or (lambda: False)
    _cancelled(cancel)
    page = _raw_page(log, cancel)
    email, mail_token = _new_available_email(log, cancel)
    given_name, family_name, password = build_profile()
    display_name = f"{given_name} {family_name}".strip()
    profile = {
        "given_name": given_name,
        "family_name": family_name,
        "display_name": display_name,
        "password": password,
    }
    _runtime.last_profile = profile

    name_input = _wait_for(
        page,
        ("input[name='name']", "input[autocomplete='name']"),
        timeout=timeout,
        cancel=cancel,
    )
    _fill(name_input, display_name)

    email_input = _find(page, ("input[name='email']", "input[autocomplete='email']"))
    if email_input is None:
        _click(
            page,
            (
                "[role='button']:has-text('Use email instead')",
                "button:has-text('Use email instead')",
                "[role='button']:has-text('改用电子邮件')",
                "[role='button']:has-text('使用邮箱')",
            ),
            timeout=10,
            cancel=cancel,
        )
        email_input = _wait_for(
            page, ("input[name='email']", "input[autocomplete='email']"), cancel=cancel
        )
    _fill(email_input, email)
    birthdate = _set_birthdate(page)
    profile["birthdate"] = birthdate
    _runtime.last_profile = profile
    log(f"[*] 已创建邮箱并填写 X.com 注册资料: {email}")
    verification_requested_at = time.time() - 5
    _next(page, cancel=cancel)

    # 定制体验/资料确认步骤在不同地区可能出现一到两页。
    for _ in range(3):
        _cancelled(cancel)
        if _find(
            page,
            (
                "input[name='verfication_code']",
                "input[name='verification_code']",
                "input[autocomplete='one-time-code']",
            ),
        ) is not None:
            break
        if _click(
            page,
            (
                "[data-testid='ocfSignupReviewNextButton']",
                "[role='button']:has-text('Sign up')",
                "button:has-text('Sign up')",
                "[role='button']:has-text('注册')",
            ),
            timeout=5,
            required=False,
            cancel=cancel,
        ):
            time.sleep(1)
            continue
        if not _next(page, required=False, cancel=cancel):
            break
        time.sleep(1)

    _raise_if_email_rejected(page, email)
    if _find(
        page,
        ("input[name='phone_number']", "input[autocomplete='tel']"),
    ) is not None:
        raise PhoneVerificationRequired("X.com 要求手机号验证，当前任务仅配置了邮箱注册")

    _wait_for(
        page,
        (
            "input[name='verfication_code']",
            "input[name='verification_code']",
            "input[autocomplete='one-time-code']",
            "input[type='tel']",
            "input[name='phone_number']",
        ),
        timeout=timeout,
        cancel=cancel,
    )
    return email, mail_token, verification_requested_at


def fill_code_and_submit(
    email,
    dev_token,
    timeout=180,
    log_callback=None,
    cancel_callback=None,
    submitted_at=None,
):
    log = log_callback or (lambda _message: None)
    cancel = cancel_callback or (lambda: False)
    page = _raw_page(log, cancel)

    verification_input = _wait_for(
        page,
        (
            "input[name='verfication_code']",
            "input[name='verification_code']",
            "input[autocomplete='one-time-code']",
            "input[type='tel']",
            "input[name='phone_number']",
        ),
        timeout=min(max(float(timeout), 1), 45),
        cancel=cancel,
    )
    try:
        input_type = str(verification_input.get_attribute("type") or "").lower()
        input_name = str(verification_input.get_attribute("name") or "").lower()
        input_autocomplete = str(
            verification_input.get_attribute("autocomplete") or ""
        ).lower()
    except Exception:
        input_type = ""
        input_name = ""
        input_autocomplete = ""
    is_code_input = "code" in input_name or input_autocomplete == "one-time-code"
    if "phone" in input_name or (input_type == "tel" and not is_code_input):
        raise PhoneVerificationRequired("X.com 要求手机号验证，当前任务仅配置了邮箱注册")
    code_input = verification_input
    def resend_code():
        return _click(
            page,
            (
                "[role='button']:has-text('Resend')",
                "button:has-text('Resend')",
                "[role='button']:has-text('重新发送')",
            ),
            timeout=3,
            required=False,
            cancel=cancel,
        )

    log("[*] 等待 X.com 邮箱验证码")
    code = engine.get_oai_code(
        dev_token,
        email,
        log_callback=log,
        cancel_callback=cancel,
        resend_callback=resend_code,
        min_received_at=submitted_at,
    )
    if not code:
        raise XAccountRegistrationError("获取验证码失败")
    try:
        _fill(code_input, str(code).replace("-", "").strip())
        _next(page, cancel=cancel)
        _wait_for(
            page,
            ("input[name='password']", "input[autocomplete='new-password']"),
            timeout=35,
            cancel=cancel,
        )
    except engine.RegistrationCancelled:
        raise
    except (ManualChallengeRequired, PhoneVerificationRequired):
        raise
    except Exception as exc:
        raise XAccountRegistrationError(
            f"验证码提交后的页面验证失败，X.com 密码页未出现: {exc}"
        ) from exc
    return code


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    log = log_callback or (lambda _message: None)
    cancel = cancel_callback or (lambda: False)
    page = _raw_page(log, cancel)
    profile = last_profile()
    password = str(profile.get("password") or "")
    if not password:
        raise XAccountRegistrationError("X.com 注册密码未生成")

    password_input = _wait_for(
        page,
        ("input[name='password']", "input[autocomplete='new-password']"),
        timeout=min(max(float(timeout), 1), 45),
        cancel=cancel,
    )
    try:
        _fill(password_input, str(password))
        _next(page, cancel=cancel)
    except engine.RegistrationCancelled:
        raise
    except (ManualChallengeRequired, PhoneVerificationRequired):
        raise
    except Exception as exc:
        raise engine.AccountRetryNeeded(f"X.com 密码提交失败: {exc}") from exc
    log("[*] 已填写并提交 X.com 注册密码")
    return profile


def wait_for_registration_completion(timeout=120, log_callback=None, cancel_callback=None):
    log = log_callback or (lambda _message: None)
    cancel = cancel_callback or (lambda: False)
    page = _raw_page(log, cancel)
    email = last_acquired_email()
    # 账号创建后可能继续进入头像、用户名等可跳过的引导。
    deadline = time.time() + max(float(timeout), 1)
    started_at = time.time()
    username = ""
    last_log_at = 0.0
    while time.time() < deadline:
        _cancelled(cancel)
        reason = _challenge_reason(page)
        if reason:
            raise ManualChallengeRequired(reason)
        if _find(
            page,
            ("input[name='phone_number']", "input[autocomplete='tel']"),
        ) is not None:
            raise PhoneVerificationRequired("X.com 要求手机号验证，当前任务仅配置了邮箱注册")
        body_text = _body_text(page).lower()
        if any(
            marker in body_text
            for marker in (
                "something went wrong. try reloading",
                "something went wrong, but don’t fret",
                "出了点问题，请尝试重新加载",
                "超过频率限制",
            )
        ):
            raise engine.AccountRetryNeeded("X.com 注册页面出现临时错误")
        if "/home" in str(page.url or "") or _find(
            page, ("[data-testid='AppTabBar_Home_Link']", "a[href='/home']")
        ) is not None:
            profile = last_profile()
            profile["username"] = username
            _runtime.last_profile = profile
            log("[+] X.com 注册页面已确认完成")
            return {"url": str(page.url or ""), "username": username}
        username_input = _find(page, ("input[name='username']",))
        if username_input is not None:
            base = re.sub(r"[^A-Za-z0-9_]", "", email.split("@", 1)[0])[:10] or "user"
            username = f"{base}{random.randint(1000, 9999)}"
            _fill(username_input, username)
            _next(page, cancel=cancel)
            time.sleep(1)
            continue
        if _click(
            page,
            (
                "[role='button']:has-text('Skip for now')",
                "[role='button']:has-text('Skip')",
                "[role='button']:has-text('暂时跳过')",
                "[role='button']:has-text('跳过')",
            ),
            timeout=3,
            required=False,
            cancel=cancel,
        ):
            time.sleep(1)
            continue
        # 密码已提交且进入后续 onboarding，账号已经创建。
        password_gone = _find(
            page, ("input[name='password']", "input[autocomplete='new-password']")
        ) is None
        code_gone = _find(
            page,
            (
                "input[name='verfication_code']",
                "input[name='verification_code']",
                "input[autocomplete='one-time-code']",
            ),
        ) is None
        if (
            time.time() - started_at >= 5
            and "/i/flow/" in str(page.url or "")
            and password_gone
            and code_gone
        ):
            profile = last_profile()
            profile["username"] = username
            _runtime.last_profile = profile
            log("[+] X.com 账号已创建，进入后续引导")
            return {"url": str(page.url or ""), "username": username}
        if time.time() - last_log_at >= 5:
            last_log_at = time.time()
            log(f"[*] 等待 X.com 注册完成... url={str(page.url or '')[:100]}")
        time.sleep(0.5)

    raise engine.AccountRetryNeeded("X.com 账号创建后的引导流程未完成")


def account_file_for_email(email: str) -> str:
    root = Path(engine.ensure_accounts_dir()) / "x"
    root.mkdir(parents=True, exist_ok=True)
    safe_email = str(email or "").strip().replace("/", "_").replace("\\", "_")
    return str((root / f"{safe_email}.txt").resolve())


def run_registration(count: int) -> None:
    engine.run_registration(count, flow_adapter=__import__(__name__, fromlist=["*"]))
