"""认证系统模块"""

import os
import json
import time
import hmac
import hashlib
import base64
import secrets
from typing import Optional
from functools import wraps
from flask import request, jsonify, g

from werkzeug.security import generate_password_hash, check_password_hash
from .account_manager import account_manager

# 全局变量
ADMIN_SECRET_KEY = None


def _split_token_and_forced_account(token: str):
    """拆分出基础密钥与指定账号序号"""
    if not token:
        return None, None, None
    if "@#@" not in token:
        return token, None, None
    
    base_token, idx_str = token.rsplit("@#@", 1)
    if not base_token:
        return None, None, "缺少密钥部分"
    idx_str = idx_str.strip()
    if not idx_str:
        return None, None, "缺少账号序号"
    if not idx_str.isdigit():
        return None, None, "账号序号必须是数字"
    
    return base_token, int(idx_str), None


def get_forced_account_index() -> Optional[int]:
    """获取当前请求指定的账号序号"""
    return getattr(g, "forced_account_idx", None)


def get_admin_secret_key() -> str:
    """获取/初始化后台密钥"""
    global ADMIN_SECRET_KEY
    if ADMIN_SECRET_KEY:
        return ADMIN_SECRET_KEY
    if account_manager.config is None:
        ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "change_me_secret")
        return ADMIN_SECRET_KEY
    secret = account_manager.config.get("admin_secret_key") or os.getenv("ADMIN_SECRET_KEY")
    if not secret:
        secret = secrets.token_urlsafe(32)
        account_manager.config["admin_secret_key"] = secret
        account_manager.save_config()
    ADMIN_SECRET_KEY = secret
    return ADMIN_SECRET_KEY




def get_admin_password_hash() -> Optional[str]:
    """获取管理员密码哈希"""
    if account_manager.config:
        return account_manager.config.get("admin_password_hash")
    return None


def set_admin_password(password: str):
    """设置管理员密码"""
    if not password:
        raise ValueError("密码不能为空")
    if account_manager.config is None:
        account_manager.config = {}
    account_manager.config["admin_password_hash"] = generate_password_hash(password)
    account_manager.save_config()


def create_admin_token(exp_seconds: int = 86400) -> str:
    """创建管理员 token"""
    payload = {
        "exp": time.time() + exp_seconds,
        "ts": int(time.time())
    }
    payload_b = json.dumps(payload, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(payload_b).decode().rstrip("=")
    secret = get_admin_secret_key().encode()
    signature = hmac.new(secret, b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{signature}"


def verify_admin_token(token: str) -> bool:
    """验证管理员 token"""
    if not token:
        return False
    try:
        b64, sig = token.split(".", 1)
    except ValueError:
        return False
    expected_sig = hmac.new(get_admin_secret_key().encode(), b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False
    padding = '=' * (-len(b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(b64 + padding).decode())
    except Exception:
        return False
    if payload.get("exp", 0) < time.time():
        return False
    return True


def is_valid_api_token(token: str) -> bool:
    """检查 API token 是否有效（支持管理员 token 和 API 密钥）"""
    if not token:
        return False
    
    base_token, forced_idx, parse_error = _split_token_and_forced_account(token)
    if parse_error:
        return False
    token_to_verify = base_token
    
    # 1. 检查是否是管理员 token
    if verify_admin_token(token_to_verify):
        return True
    
    # 2. 检查是否是 API 密钥（数据库）
    try:
        from .api_key_manager import verify_api_key
        api_key_obj = verify_api_key(token_to_verify)
        if api_key_obj:
            return True
    except Exception:
        # 如果数据库未初始化或出错，忽略
        pass
    
    return False


def get_api_key_from_token(token: str):
    """从 token 获取 API 密钥对象（如果存在）"""
    if not token:
        return None
    
    base_token, _, parse_error = _split_token_and_forced_account(token)
    if parse_error:
        return None
    
    try:
        from .api_key_manager import verify_api_key
        return verify_api_key(base_token)
    except Exception:
        return None


def is_admin_authenticated() -> bool:
    """检查管理员是否已认证"""
    token = (
        request.headers.get("X-Admin-Token")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or request.cookies.get("admin_token")
    )
    return verify_admin_token(token)


def require_api_auth(func):
    """开放接口需要 api_token、API 密钥或 admin token"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = (
            request.headers.get("X-API-Token")
            or request.headers.get("Authorization", "").replace("Bearer ", "")
            or request.cookies.get("admin_token")
        )
        base_token, forced_idx, parse_error = _split_token_and_forced_account(token)
        if parse_error:
            return jsonify({"error": f"API 密钥格式错误: {parse_error}"}), 400
        if forced_idx is not None:
            g.forced_account_idx = forced_idx
        if not is_valid_api_token(base_token):
            return jsonify({"error": "未授权"}), 401
        
        # 如果是 API 密钥，更新使用统计
        api_key_obj = get_api_key_from_token(base_token)
        if api_key_obj:
            from .api_key_manager import update_api_key_usage
            update_api_key_usage(api_key_obj.id)
        
        return func(*args, **kwargs)
    return wrapper


def require_admin(func):
    """管理接口需要 admin token"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not is_admin_authenticated():
            return jsonify({"error": "未授权"}), 401
        return func(*args, **kwargs)
    return wrapper
