"""JWT 工具模块"""

import json
import time
import hmac
import hashlib
import base64
import requests
from typing import Optional

from .config import GETOXSRF_URL
from .exceptions import AccountAuthError, AccountRequestError
from .account_manager import account_manager


def url_safe_b64encode(data: bytes) -> str:
    """URL安全的Base64编码，不带padding"""
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def kq_encode(s: str) -> str:
    """模拟JS的kQ函数"""
    byte_arr = bytearray()
    for char in s:
        val = ord(char)
        if val > 255:
            byte_arr.append(val & 255)
            byte_arr.append(val >> 8)
        else:
            byte_arr.append(val)
    return url_safe_b64encode(bytes(byte_arr))


def decode_xsrf_token(xsrf_token: str) -> bytes:
    """将 xsrfToken 解码为字节数组（用于HMAC签名）"""
    padding = 4 - len(xsrf_token) % 4
    if padding != 4:
        xsrf_token += '=' * padding
    return base64.urlsafe_b64decode(xsrf_token)


def create_jwt(key_bytes: bytes, key_id: str, csesidx: str) -> str:
    """创建JWT token"""
    now = int(time.time())

    header = {
        "alg": "HS256",
        "typ": "JWT",
        "kid": key_id
    }

    payload = {
        "iss": "https://business.gemini.google",
        "aud": "https://biz-discoveryengine.googleapis.com",
        "sub": f"csesidx/{csesidx}",
        "iat": now,
        "exp": now + 300,
        "nbf": now
    }

    header_b64 = kq_encode(json.dumps(header, separators=(',', ':')))
    payload_b64 = kq_encode(json.dumps(payload, separators=(',', ':')))
    message = f"{header_b64}.{payload_b64}"

    signature = hmac.new(key_bytes, message.encode('utf-8'), hashlib.sha256).digest()
    signature_b64 = url_safe_b64encode(signature)

    return f"{message}.{signature_b64}"


def _strip_google_json_prefix(text: str) -> str:
    """移除 Google 返回的安全前缀"""
    if text.startswith(")]}'\n") or text.startswith(")]}'"):
        return text[4:].strip()
    return text


def _build_cookie_string(account: dict) -> str:
    """根据账号信息构建完整 Cookie 字符串"""
    cookie_parts = []
    secure_c_ses = account.get("secure_c_ses")
    host_c_oses = account.get("host_c_oses")

    if secure_c_ses:
        cookie_parts.append(f"__Secure-C_SES={secure_c_ses}")
    if host_c_oses:
        cookie_parts.append(f"__Host-C_OSES={host_c_oses}")

    extra_cookies = account.get("extra_cookies") or {}
    if isinstance(extra_cookies, dict):
        for name, value in extra_cookies.items():
            if name and value:
                cookie_parts.append(f"{name}={value}")

    return "; ".join(cookie_parts)


def get_jwt_for_account(account: dict, proxy: str, account_idx: Optional[int] = None) -> str:
    """为指定账号获取JWT"""
    from .utils import raise_for_account_response
    
    secure_c_ses = account.get("secure_c_ses")
    host_c_oses = account.get("host_c_oses")
    csesidx = account.get("csesidx")

    if not secure_c_ses or not csesidx:
        raise ValueError("缺少 secure_c_ses 或 csesidx")

    url = f"{GETOXSRF_URL}?csesidx={csesidx}"
    proxies = {"http": proxy, "https": proxy} if proxy else None

    headers = {
        "accept": "*/*",
        "user-agent": account.get('user_agent', 'Mozilla/5.0'),
        "cookie": f'__Secure-C_SES={secure_c_ses}; __Host-C_OSES={host_c_oses}',
    }

    try:
        resp = requests.get(url, headers=headers, proxies=proxies, verify=False, timeout=30)
    except requests.RequestException as e:
        raise AccountRequestError(f"获取JWT 请求失败: {e}") from e

    if resp.status_code != 200:
        if account_idx is not None:
            raise_for_account_response(resp, "获取JWT", account_idx)
        else:
            raise AccountAuthError(f"获取JWT失败: HTTP {resp.status_code}")

    # 处理Google安全前缀
    text = resp.text
    if text.startswith(")]}'\n") or text.startswith(")]}'"): 
        text = text[4:].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AccountAuthError(f"解析JWT响应失败: {e}") from e

    key_id = data.get("keyId")
    xsrf_token = data.get("xsrfToken")
    if not key_id or not xsrf_token:
        raise AccountAuthError(f"JWT 响应缺少 keyId/xsrfToken: {data}")

    print(f"账号: {account.get('csesidx')} 账号可用! key_id: {key_id}")

    key_bytes = decode_xsrf_token(xsrf_token)

    return create_jwt(key_bytes, key_id, csesidx)
