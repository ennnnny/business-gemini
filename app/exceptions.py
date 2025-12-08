"""自定义异常类"""

from typing import Optional


class AccountError(Exception):
    """基础账号异常"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class AccountAuthError(AccountError):
    """凭证/权限相关异常"""


class AccountRateLimitError(AccountError):
    """配额或限流异常"""

class AccountResourceExhaustedError(AccountError):
    """资源耗尽异常"""

class AccountRequestError(AccountError):
    """其他请求异常"""


class NoAvailableAccount(AccountError):
    """无可用账号异常"""

