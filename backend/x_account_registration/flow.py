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
from urllib.parse import urlsplit

from backend.registration import engine
from backend.registration.signup_flow import AccountAlreadyRegistered, build_profile


SIGNUP_URL = "https://x.com/i/flow/signup"
DEFAULT_TIMEOUT = 45
target_label = "X.com"
result_source = "x_account"
result_platform = "x"
batch_source = "x-account-web"
force_workers = 1
NAME_INPUT_SELECTORS = ("input[name='name']", "input[autocomplete='name']")
EMAIL_INPUT_SELECTORS = (
    "input[name='email']",
    "input[autocomplete='email']",
)
# X.com 会按地区/出口返回两种首屏：旧版先填姓名与生日，新版先显示
# “Email or username”。新版输入框目前使用 name=text，另外保留可访问属性选择器，
# 避免只依赖易变的 CSS class。
EMAIL_FIRST_INPUT_SELECTORS = (
    "input[aria-label='Email or username']",
    "input[placeholder='Email or username']",
    "input[aria-label='邮箱或用户名']",
    "input[placeholder='邮箱或用户名']",
    "input[autocomplete='username']",
    "input[data-testid='ocfEnterTextTextInput']",
    "input[name='text']",
)
UNIFIED_IDENTITY_INPUT_SELECTORS = (
    "input[name='username_or_email']",
    "input[autocomplete='username webauthn']",
    "input[autocomplete~='webauthn']",
)
CREATE_ACCOUNT_SELECTORS = (
    "[data-testid='ocfSignupButton']",
    "a[href='/i/flow/signup']",
    "[role='button']:has-text('Create account')",
    "[role='button']:has-text('创建账号')",
)
CODE_INPUT_SELECTORS = (
    "input[name='verfication_code']",
    "input[name='verification_code']",
    "input[autocomplete='one-time-code']",
)
PHONE_INPUT_SELECTORS = (
    "input[name='phone_number']",
    "input[name='phone']",
    "input[autocomplete='tel']",
    "input[inputmode='tel']",
)
PASSWORD_INPUT_SELECTORS = (
    "input[name='password']",
    "input[autocomplete='new-password']",
)
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


def _remember_log_callback(log: Callable[[str], None] | None) -> None:
    """Keep the current worker logger available to low-level timeout diagnostics."""
    if callable(log):
        _runtime.log_callback = log


def _runtime_log(message: str) -> None:
    callback = getattr(_runtime, "log_callback", None)
    if not callable(callback):
        return
    try:
        callback(message)
    except Exception:
        # Diagnostics must never replace the original registration exception.
        pass


def _redact_diagnostic_text(value: Any, limit: int = 320) -> str:
    """Return a single-line preview without input values, mail addresses or URLs."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "<email>",
        text,
    )
    text = re.sub(r"(?i)(token|password|code)=([^&\s]+)", r"\1=<redacted>", text)
    return text[: max(int(limit), 1)]


def _safe_diagnostic_url(value: Any) -> str:
    """Strip credentials, query strings and fragments from diagnostic URLs."""
    raw = str(value or "").strip()
    if not raw:
        return "<empty>"
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        origin = f"{parsed.scheme}://{hostname}{port}" if parsed.scheme else hostname
        return _redact_diagnostic_text(f"{origin}{parsed.path}", 220)
    except Exception:
        return _redact_diagnostic_text(raw.split("?", 1)[0].split("#", 1)[0], 220)


def _iter_page_scopes(page: Any) -> list[tuple[str, Any]]:
    """Search the top-level document and child frames, avoiding the main-frame duplicate."""
    scopes: list[tuple[str, Any]] = [("page", page)]
    try:
        frames = list(page.frames)
        main_frame = getattr(page, "main_frame", None)
    except Exception:
        return scopes
    child_index = 0
    for frame in frames:
        if frame is main_frame:
            continue
        child_index += 1
        scopes.append((f"frame{child_index}", frame))
    return scopes


def _cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel and cancel():
        raise engine.RegistrationCancelled("用户停止注册")


def _visible(locator: Any) -> bool:
    try:
        if not bool(locator.count()) or not bool(locator.first.is_visible()):
            return False
        # Playwright deliberately considers opacity:0 elements visible. X keeps
        # a transparent password input under the email/username field, so using
        # is_visible() alone mistakes the preloaded login control for a new step.
        rendered = locator.first.evaluate(
            """element => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return {
                    display: style.display,
                    visibility: style.visibility,
                    opacity: Number(style.opacity || 0),
                    width: rect.width,
                    height: rect.height,
                };
            }"""
        )
        if isinstance(rendered, dict):
            if rendered.get("display") == "none" or rendered.get("visibility") == "hidden":
                return False
            if float(rendered.get("opacity", 1) or 0) <= 0.01:
                return False
            if float(rendered.get("width", 0) or 0) <= 0 or float(
                rendered.get("height", 0) or 0
            ) <= 0:
                return False
        return True
    except Exception:
        try:
            return bool(locator.count()) and bool(locator.first.is_visible())
        except Exception:
            return False


def _find(page: Any, selectors: Iterable[str]) -> Any | None:
    for selector in selectors:
        for _scope_label, scope in _iter_page_scopes(page):
            try:
                locator = scope.locator(selector)
                if _visible(locator):
                    return locator.first
            except Exception:
                continue
    return None


def _find_email_first_input(page: Any) -> Any | None:
    """Locate X's email-first textbox even when attributes or frame placement change."""
    # X's unified landing uses this control for the begin_login API. Despite the
    # visible "Email or username" copy, it is not an email-registration field.
    if _find(page, UNIFIED_IDENTITY_INPUT_SELECTORS) is not None:
        return None
    locator = _find(page, EMAIL_FIRST_INPUT_SELECTORS)
    if locator is not None:
        return locator

    label_pattern = re.compile(r"^(email or username|邮箱或用户名)$", re.IGNORECASE)
    for _scope_label, scope in _iter_page_scopes(page):
        for method_name, args in (
            ("get_by_label", (label_pattern,)),
            ("get_by_role", ("textbox",)),
        ):
            try:
                method = getattr(scope, method_name)
                candidate = (
                    method(*args, name=label_pattern)
                    if method_name == "get_by_role"
                    else method(*args)
                )
                if _visible(candidate):
                    return candidate.first
            except Exception:
                continue

    # Some X variants draw the prompt as adjacent text rather than associating a
    # label/placeholder with the input. Only use this broad fallback on that exact
    # email-first screen and never read or log the input's current value.
    for _scope_label, scope in _iter_page_scopes(page):
        try:
            body_text = str(scope.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            body_text = ""
        if "email or username" not in body_text and "邮箱或用户名" not in body_text:
            continue
        try:
            inputs = scope.locator("input")
            input_count = min(int(inputs.count()), 20)
        except Exception:
            continue
        for index in range(input_count):
            try:
                candidate = inputs.nth(index)
                input_type = str(candidate.get_attribute("type") or "text").lower()
                if input_type in {"text", "email"} and candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def _find_exact_text_button(page: Any, labels: Iterable[str]) -> Any | None:
    """Find a visible button by its rendered text, including nested-label variants."""
    expected = {
        re.sub(r"\s+", " ", str(label or "")).strip().casefold()
        for label in labels
        if str(label or "").strip()
    }
    if not expected:
        return None
    for _scope_label, scope in _iter_page_scopes(page):
        try:
            buttons = scope.locator("button, [role='button']")
            button_count = min(int(buttons.count()), 30)
        except Exception:
            continue
        for index in range(button_count):
            try:
                candidate = buttons.nth(index)
                if not candidate.is_visible():
                    continue
                rendered_text = re.sub(
                    r"\s+", " ", str(candidate.inner_text(timeout=500) or "")
                ).strip().casefold()
                if rendered_text in expected:
                    return candidate
            except Exception:
                continue
    return None


def _is_unified_signup_landing(page: Any) -> bool:
    """Return true for X's new signup modal whose email box actually logs in."""
    # X initially redirects with ?mode=signup, then removes the query string
    # while keeping the same React view. The stable discriminator is the exact
    # identity control used by jf.x.com's begin_login action.
    return _find(page, UNIFIED_IDENTITY_INPUT_SELECTORS) is not None


def _enter_email_signup_from_unified_landing(
    page: Any,
    log: Callable[[str], None],
    cancel: Callable[[], bool],
) -> None:
    """Enter X's actual signup branch and switch from phone to email when offered."""
    log("[*] 已识别 X.com 新版统一登录/注册页，进入官方注册分支")
    deadline = time.time() + 12
    phone_entry = None
    while time.time() < deadline:
        _cancelled(cancel)
        phone_entry = _find_exact_text_button(
            page,
            ("Continue with phone", "使用手机号继续"),
        )
        if phone_entry is not None:
            break
        time.sleep(0.25)
    if phone_entry is None:
        _emit_timeout_diagnostics(page, ("button:has-text('Continue with phone')",))
        raise XAccountRegistrationError("X.com 新版注册入口未出现")
    phone_entry.click(timeout=10_000)

    # Older variants expose Use email instead after entering the phone branch.
    # Current X may omit it entirely and require SMS; do not consume a temporary
    # email until we have confirmed that an email registration form exists.
    phone_input = None
    deadline = time.time() + 12
    while time.time() < deadline:
        _cancelled(cancel)
        email_input = _find(page, EMAIL_INPUT_SELECTORS)
        if email_input is not None:
            log("[*] X.com 注册分支已提供邮箱输入框")
            return
        use_email = _find_exact_text_button(
            page,
            ("Use email instead", "使用邮箱", "改用电子邮件"),
        )
        if use_email is not None:
            use_email.click(timeout=10_000)
            _wait_for(page, EMAIL_INPUT_SELECTORS, timeout=12, cancel=cancel)
            log("[*] 已从 X.com 手机注册页切换为邮箱注册")
            return
        phone_input = _find(page, PHONE_INPUT_SELECTORS)
        if phone_input is not None:
            # Give late-rendered alternatives a short chance to appear.
            time.sleep(2)
            use_email = _find_exact_text_button(
                page,
                ("Use email instead", "使用邮箱", "改用电子邮件"),
            )
            if use_email is not None:
                use_email.click(timeout=10_000)
                _wait_for(page, EMAIL_INPUT_SELECTORS, timeout=12, cancel=cancel)
                log("[*] 已从 X.com 手机注册页切换为邮箱注册")
                return
            break
        time.sleep(0.25)

    if phone_input is not None:
        raise PhoneVerificationRequired(
            "X.com 当前官方注册页仅提供手机号和短信验证，没有“Use email instead”；"
            "临时邮箱无法单独完成本次注册"
        )
    _emit_timeout_diagnostics(page, PHONE_INPUT_SELECTORS + EMAIL_INPUT_SELECTORS)
    raise XAccountRegistrationError("进入 X.com 官方注册分支后未出现手机号或邮箱控件")


def _diagnostic_attr(locator: Any, name: str) -> str:
    try:
        return _redact_diagnostic_text(locator.get_attribute(name) or "-", 90)
    except Exception as exc:
        return f"<error:{type(exc).__name__}>"


def _emit_timeout_diagnostics(page: Any, selectors: Iterable[str]) -> None:
    """Emit enough safe DOM state to diagnose a changed X screen from Web monitoring."""
    selector_list = tuple(selectors)
    try:
        title = _redact_diagnostic_text(page.title(), 160)
    except Exception as exc:
        title = f"<error:{type(exc).__name__}>"
    try:
        ready_state = _redact_diagnostic_text(page.evaluate("document.readyState"), 40)
    except Exception as exc:
        ready_state = f"<error:{type(exc).__name__}>"
    _runtime_log(
        "[诊断] X.com 控件等待超时 | "
        f"URL={_safe_diagnostic_url(getattr(page, 'url', ''))} | "
        f"title={title or '<empty>'} | readyState={ready_state}"
    )
    _runtime_log("[诊断] 等待的选择器: " + " || ".join(selector_list))

    scopes = _iter_page_scopes(page)
    frame_details = []
    for scope_label, scope in scopes:
        frame_details.append(
            f"{scope_label}={_safe_diagnostic_url(getattr(scope, 'url', getattr(page, 'url', '')))}"
        )
    _runtime_log(f"[诊断] 页面作用域({len(scopes)}): " + " | ".join(frame_details))

    selector_details = []
    for selector in selector_list:
        per_scope = []
        for scope_label, scope in scopes:
            try:
                candidate = scope.locator(selector)
                count = int(candidate.count())
                visible = bool(count and candidate.first.is_visible())
                per_scope.append(f"{scope_label}:{count}/visible={str(visible).lower()}")
            except Exception as exc:
                per_scope.append(f"{scope_label}:error={type(exc).__name__}:{exc}")
        selector_details.append(f"{selector} => " + ", ".join(per_scope))
    for detail in selector_details:
        _runtime_log("[诊断] 选择器匹配: " + _redact_diagnostic_text(detail, 700))

    input_details = []
    button_details = []
    body_preview = ""
    for scope_label, scope in scopes:
        try:
            inputs = scope.locator("input")
            input_count = min(int(inputs.count()), 20)
            for index in range(input_count):
                candidate = inputs.nth(index)
                try:
                    visible = bool(candidate.is_visible())
                except Exception:
                    visible = False
                input_details.append(
                    f"{scope_label}[{index}] visible={str(visible).lower()} "
                    f"type={_diagnostic_attr(candidate, 'type')} "
                    f"name={_diagnostic_attr(candidate, 'name')} "
                    f"aria-label={_diagnostic_attr(candidate, 'aria-label')} "
                    f"placeholder={_diagnostic_attr(candidate, 'placeholder')} "
                    f"autocomplete={_diagnostic_attr(candidate, 'autocomplete')} "
                    f"testid={_diagnostic_attr(candidate, 'data-testid')}"
                )
        except Exception as exc:
            input_details.append(f"{scope_label}:error={type(exc).__name__}:{exc}")

        try:
            buttons = scope.locator("button, [role='button']")
            button_count = min(int(buttons.count()), 20)
            for index in range(button_count):
                candidate = buttons.nth(index)
                if not candidate.is_visible():
                    continue
                text = _redact_diagnostic_text(candidate.inner_text(timeout=500), 100)
                button_details.append(
                    f"{scope_label}[{index}] text={text or '<empty>'} "
                    f"testid={_diagnostic_attr(candidate, 'data-testid')}"
                )
        except Exception as exc:
            button_details.append(f"{scope_label}:error={type(exc).__name__}:{exc}")

        if not body_preview:
            try:
                body_preview = _redact_diagnostic_text(
                    scope.locator("body").inner_text(timeout=1000), 500
                )
            except Exception:
                pass

    _runtime_log(
        f"[诊断] 输入框({len(input_details)}): "
        + (" | ".join(input_details) if input_details else "未发现")
    )
    _runtime_log(
        f"[诊断] 可见按钮({len(button_details)}): "
        + (" | ".join(button_details) if button_details else "未发现")
    )
    _runtime_log(f"[诊断] 页面文本预览: {body_preview or '<empty>'}")


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
    selector_list = tuple(selectors)
    deadline = time.time() + max(float(timeout), 1.0)
    while time.time() < deadline:
        _cancelled(cancel)
        reason = _challenge_reason(page)
        if reason:
            raise ManualChallengeRequired(reason)
        locator = _find(page, selector_list)
        if locator is None and any(
            selector in EMAIL_FIRST_INPUT_SELECTORS for selector in selector_list
        ):
            locator = _find_email_first_input(page)
        if locator is not None:
            return locator
        time.sleep(0.25)
    _emit_timeout_diagnostics(page, selector_list)
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
    # X's current onboarding screen renders the Continue label inside nested
    # elements. Playwright's :text-is() therefore does not match the parent
    # button, while :has-text('Continue') would also match Continue with phone,
    # Google and Apple. Resolve the visible button by its exact rendered text.
    exact_button = _find_exact_text_button(
        page,
        ("Continue", "继续", "Next", "下一步"),
    )
    if exact_button is not None:
        _cancelled(cancel)
        exact_button.click(timeout=10_000)
        return True
    return _click(
        page,
        (
            "[data-testid='ocfEnterTextNextButton']",
            "[data-testid='ocfSignupReviewNextButton']",
            # :text-is() 是精确匹配，不能用 :has-text('Continue')；否则会误点
            # 首屏更靠前的 “Continue with phone/Google/Apple”。
            "button:text-is('Continue')",
            "[role='button']:text-is('Continue')",
            "button:text-is('继续')",
            "[role='button']:text-is('继续')",
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
    if _find(page, NAME_INPUT_SELECTORS) is not None:
        log("[*] 已识别 X.com 资料优先注册页")
        return
    if _is_unified_signup_landing(page):
        _enter_email_signup_from_unified_landing(page, log, cancel)
        return
    if _find_email_first_input(page) is not None:
        log("[*] 已识别 X.com 邮箱优先注册页")
        return

    # domcontentloaded 时 React 表单可能尚未渲染。先同时等待两种表单和旧版
    # Create account 入口，避免邮箱优先表单稍晚出现时被误判为按钮缺失。
    first_control = _wait_for(
        page,
        NAME_INPUT_SELECTORS
        + UNIFIED_IDENTITY_INPUT_SELECTORS
        + EMAIL_FIRST_INPUT_SELECTORS
        + CREATE_ACCOUNT_SELECTORS,
        timeout=20,
        cancel=cancel,
    )
    if _find(page, NAME_INPUT_SELECTORS) is not None:
        log("[*] 已识别 X.com 资料优先注册页")
        return
    if _is_unified_signup_landing(page):
        _enter_email_signup_from_unified_landing(page, log, cancel)
        return
    if _find_email_first_input(page) is not None:
        log("[*] 已识别 X.com 邮箱优先注册页")
        return

    first_control.click(timeout=20_000)
    first_input = _wait_for(
        page,
        NAME_INPUT_SELECTORS + EMAIL_FIRST_INPUT_SELECTORS,
        cancel=cancel,
    )
    try:
        input_name = str(first_input.get_attribute("name") or "").lower()
        input_label = str(first_input.get_attribute("aria-label") or "").lower()
    except Exception:
        input_name = ""
        input_label = ""
    if input_name == "text" or "email" in input_label or "邮箱" in input_label:
        log("[*] 已识别 X.com 邮箱优先注册页")
    else:
        log("[*] 已识别 X.com 资料优先注册页")


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
    _remember_log_callback(log)
    cancel = cancel_callback or (lambda: False)
    _cancelled(cancel)
    _runtime.last_email = ""
    _runtime.last_profile = {}
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            log(f"[*] X.com 注册页识别尝试 {attempt}/3")
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
            log(
                f"[诊断] X.com 注册页识别第 {attempt}/3 次失败: "
                f"{type(exc).__name__}: {exc}"
            )
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


def _fill_name_and_birthdate_if_present(
    page: Any,
    profile: dict[str, str],
    *,
    cancel: Callable[[], bool],
    require_birthdate: bool,
) -> bool:
    """填写当前可见的资料表单；邮箱优先流程允许姓名与生日分步出现。"""
    name_input = _find(page, NAME_INPUT_SELECTORS)
    if name_input is None:
        return False
    _fill(name_input, str(profile.get("display_name") or ""))
    try:
        select_count = int(page.locator("select").count())
    except Exception:
        select_count = 0
    if select_count >= 3:
        profile["birthdate"] = _set_birthdate(page)
    elif require_birthdate:
        raise XAccountRegistrationError("X.com 出生日期控件未出现")
    _runtime.last_profile = profile
    _next(page, cancel=cancel)
    return True


def _fill_standalone_birthdate_if_present(
    page: Any,
    profile: dict[str, str],
    *,
    cancel: Callable[[], bool],
) -> bool:
    """兼容新版流程把出生日期拆成姓名之后的独立页面。"""
    try:
        select_count = int(page.locator("select").count())
    except Exception:
        return False
    if select_count < 3:
        return False
    profile["birthdate"] = _set_birthdate(page)
    _runtime.last_profile = profile
    _next(page, cancel=cancel)
    return True


def _wait_for_email_verification_step(
    page: Any,
    email: str,
    profile: dict[str, str],
    *,
    timeout: float,
    cancel: Callable[[], bool],
) -> None:
    """等待邮箱验证码页，同时兼容邮箱之后才出现资料表单的新版流程。"""
    deadline = time.time() + max(float(timeout), 1.0)
    profile_submitted = False
    while time.time() < deadline:
        _cancelled(cancel)
        reason = _challenge_reason(page)
        if reason:
            raise ManualChallengeRequired(reason)
        _raise_if_email_rejected(page, email)

        if _find(page, CODE_INPUT_SELECTORS + ("input[type='tel']",)) is not None:
            return
        if _find(page, PHONE_INPUT_SELECTORS) is not None:
            raise PhoneVerificationRequired("X.com 要求手机号验证，当前任务仅配置了邮箱注册")
        if _find(page, PASSWORD_INPUT_SELECTORS) is not None:
            # 新邮箱在完成邮箱验证码之前不应直接进入密码页；这种情况通常表示
            # 邮箱已属于现有账号，不能把登录流程误记成注册成功。
            raise AccountAlreadyRegistered(f"X.com 邮箱已注册: {email}")
        if not profile_submitted and _find(page, NAME_INPUT_SELECTORS) is not None:
            _fill_name_and_birthdate_if_present(
                page,
                profile,
                cancel=cancel,
                require_birthdate=False,
            )
            profile_submitted = True
            time.sleep(0.75)
            continue
        if _fill_standalone_birthdate_if_present(
            page,
            profile,
            cancel=cancel,
        ):
            profile_submitted = True
            time.sleep(0.75)
            continue

        # 资料确认/个性化页面的按钮因地区不同可能显示 Sign up 或 Next。
        if _click(
            page,
            (
                "[data-testid='ocfSignupReviewNextButton']",
                "[role='button']:text-is('Sign up')",
                "button:text-is('Sign up')",
                "[role='button']:text-is('注册')",
                "button:text-is('注册')",
            ),
            timeout=1,
            required=False,
            cancel=cancel,
        ):
            time.sleep(0.75)
            continue
        time.sleep(0.25)
    raise XAccountRegistrationError("提交邮箱后等待 X.com 验证码页面超时")


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

    name_input = _find(page, NAME_INPUT_SELECTORS)
    email_first_input = None if name_input is not None else _find_email_first_input(page)

    if email_first_input is not None:
        _fill(email_first_input, email)
        log(f"[*] 已创建邮箱并填写 X.com 邮箱优先注册页: {email}")
        verification_requested_at = time.time() - 5
        _next(page, cancel=cancel)
        _wait_for_email_verification_step(
            page,
            email,
            profile,
            timeout=timeout,
            cancel=cancel,
        )
        return email, mail_token, verification_requested_at

    if name_input is None:
        name_input = _wait_for(
            page,
            NAME_INPUT_SELECTORS,
            timeout=timeout,
            cancel=cancel,
        )
    _fill(name_input, display_name)

    email_input = _find(page, EMAIL_INPUT_SELECTORS)
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
            page, EMAIL_INPUT_SELECTORS, cancel=cancel
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
                *CODE_INPUT_SELECTORS,
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
        PHONE_INPUT_SELECTORS,
    ) is not None:
        raise PhoneVerificationRequired("X.com 要求手机号验证，当前任务仅配置了邮箱注册")

    _wait_for(
        page,
        CODE_INPUT_SELECTORS + ("input[type='tel']",) + PHONE_INPUT_SELECTORS,
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
        CODE_INPUT_SELECTORS + ("input[type='tel']",) + PHONE_INPUT_SELECTORS,
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
        deadline = time.time() + 35
        profile = last_profile()
        while time.time() < deadline:
            _cancelled(cancel)
            reason = _challenge_reason(page)
            if reason:
                raise ManualChallengeRequired(reason)
            if _find(page, PHONE_INPUT_SELECTORS) is not None:
                raise PhoneVerificationRequired(
                    "X.com 要求手机号验证，当前任务仅配置了邮箱注册"
                )
            if _find(page, PASSWORD_INPUT_SELECTORS) is not None:
                break
            if _find(page, NAME_INPUT_SELECTORS) is not None:
                _fill_name_and_birthdate_if_present(
                    page,
                    profile,
                    cancel=cancel,
                    require_birthdate=False,
                )
                time.sleep(0.75)
                continue
            if _fill_standalone_birthdate_if_present(
                page,
                profile,
                cancel=cancel,
            ):
                time.sleep(0.75)
                continue
            time.sleep(0.25)
        else:
            raise XAccountRegistrationError("邮箱验证码通过后，X.com 密码页未出现")
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
        PASSWORD_INPUT_SELECTORS,
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
            PHONE_INPUT_SELECTORS,
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
            page, PASSWORD_INPUT_SELECTORS
        ) is None
        code_gone = _find(
            page,
            CODE_INPUT_SELECTORS,
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
