"""XRegistration 对 Web 管理系统暴露的稳定服务边界。

浏览器与邮箱实现继续复用成熟的 ``backend.registration`` 基础设施；Web 层只
通过本服务启动注册，从而与账号管理、设置和其他控制台功能保持解耦。
"""
from __future__ import annotations

from typing import Any, Dict

from backend.registration import engine


class XRegistrationService:
    """运行只输出邮箱和密码的账号注册任务。"""

    module_name = "XRegistration"
    runtime = engine

    @staticmethod
    def load_config() -> Dict[str, Any]:
        engine.load_config()
        return engine.config

    @staticmethod
    def run(count: int) -> None:
        engine.run_registration(count)

    @staticmethod
    def repository():
        return engine.get_registration_repository()


x_registration_service = XRegistrationService()
