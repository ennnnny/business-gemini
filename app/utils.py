"""工具函数模块"""

import requests
from typing import Optional

from .exceptions import AccountAuthError, AccountRateLimitError, AccountRequestError


def check_proxy(proxy: str) -> bool:
    """检测代理是否可用"""
    if not proxy:
        return False
    try:
        proxies = {"http": proxy, "https": proxy}
        resp = requests.get("https://www.google.com", proxies=proxies, 
                          verify=False, timeout=10)
        return resp.status_code == 200
    except:
        return False


def get_proxy() -> Optional[str]:
    """获取代理配置，根据proxy_enabled开关决定是否返回代理地址
    
    如果 proxy_enabled 为 False 或未设置，即使配置中有代理也返回 None，
    这样可以避免使用系统环境变量中的默认代理。
    
    Returns:
        代理地址字符串，如果禁用代理或代理为空则返回 None
    """
    from .account_manager import account_manager
    
    if account_manager.config is None:
        return None
    
    # 检查 proxy_enabled 开关
    if not account_manager.config.get("proxy_enabled", False):
        return None
    
    # 获取代理地址
    proxy = account_manager.config.get("proxy")
    
    # 如果代理为空字符串或无效，返回 None
    if not proxy or not isinstance(proxy, str) or not proxy.strip():
        return None
    
    proxy = proxy.strip()
    
    # 检查代理格式是否有效
    if not (proxy.startswith("http://") or proxy.startswith("https://") or proxy.startswith("socks5://")):
        return None
    
    return proxy


def raise_for_account_response(resp: requests.Response, action: str, account_idx: Optional[int] = None, quota_type: Optional[str] = None):
    """根据响应状态码抛出相应的账号异常，并被动检测配额错误
    
    Args:
        resp: HTTP 响应对象
        action: 操作名称（用于错误消息）
        account_idx: 账号索引（用于标记配额错误）
        quota_type: 配额类型（"images", "videos", "text_queries"），用于按类型冷却
    """
    status = resp.status_code
    try:
        error_data = resp.json()
        error_msg = error_data.get("error", {}).get("message", "") or str(error_data)
    except:
        error_msg = resp.text[:200] or f"HTTP {status}"
    
    msg = f"{action} 失败: {error_msg}"
    if account_idx is not None:
        msg = f"账号 {account_idx} {msg}"
    
    # 被动检测配额错误：检测到 401, 403, 429 时标记账号配额错误
    # 429 通常是配额错误，按配额类型冷却；401/403 是认证错误，冷却整个账号
    if account_idx is not None and status in (401, 403, 429):
        from .account_manager import account_manager
        # 429 错误且指定了配额类型，按类型冷却；否则冷却整个账号
        if status == 429 and quota_type:
            print(f"[!] 账号 {account_idx} {quota_type} 配额错误 (HTTP {status})，暂不冷却")
            # account_manager.mark_quota_error(account_idx, status, error_msg, quota_type)
        else:
            # 401/403 或未指定配额类型，冷却整个账号
            account_manager.mark_quota_error(account_idx, status, error_msg, None)
    
    if status in (401, 403):
        raise AccountAuthError(msg, status)
    elif status == 429:
        raise AccountRateLimitError(msg, status)
    else:
        raise AccountRequestError(msg, status)


def seconds_until_next_pt_midnight(now_ts: Optional[float] = None) -> int:
    """计算距离下一个 PT 午夜的秒数，用于配额冷却"""
    from datetime import datetime, timedelta, timezone
    from .config import ZoneInfo
    
    now_utc = datetime.now(timezone.utc) if now_ts is None else datetime.fromtimestamp(now_ts, tz=timezone.utc)
    if ZoneInfo:
        pt_tz = ZoneInfo("America/Los_Angeles")
        now_pt = now_utc.astimezone(pt_tz)
    else:
        # 兼容旧版本 Python 的简易回退（不考虑夏令时）
        now_pt = now_utc - timedelta(hours=8)

    tomorrow = (now_pt + timedelta(days=1)).date()
    midnight_pt = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now_pt.tzinfo)
    delta = (midnight_pt - now_pt).total_seconds()
    return max(0, int(delta))

