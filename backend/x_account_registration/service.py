"""X.com 注册功能对 Web 管理系统暴露的服务边界。"""
from __future__ import annotations

from typing import Any, Dict

from backend.registration import engine
from . import flow


class XAccountRegistrationService:
    module_name = "XAccountRegistration"
    runtime = engine

    @staticmethod
    def load_config() -> Dict[str, Any]:
        engine.load_config()
        return engine.config

    @staticmethod
    def run(count: int) -> None:
        flow.run_registration(count)

    @staticmethod
    def repository():
        return engine.get_registration_repository()


x_account_registration_service = XAccountRegistrationService()
