"""X.com 注册任务协调器。"""

from backend.web.jobs import RegistrationJobCoordinator


x_account_job_coordinator = RegistrationJobCoordinator(
    service_kind="x_account",
    source="x_account",
    persist_snapshot=False,
    thread_name="web-x-account-registration",
)
