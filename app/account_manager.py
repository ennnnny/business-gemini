"""账号管理器模块"""

import json
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from .config import (
    CONFIG_FILE, AUTH_ERROR_COOLDOWN_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS,
    GENERIC_ERROR_COOLDOWN_SECONDS, ZoneInfo
)
from .exceptions import NoAvailableAccount
from .logger import set_log_level


class AccountManager:
    """多账号管理器，支持轮训策略"""
    
    def __init__(self):
        self.config = None
        self.accounts = []  # 账号列表
        self.current_index = 0  # 当前轮训索引
        self.account_states = {}  # 账号状态: {index: {jwt, jwt_time, session, available, cooldown_until, cooldown_reason, quota_usage, quota_reset_date}}
        self.conversation_sessions = {}  # 对话 session 映射: {account_idx: {conversation_id: session_name}}
        self.lock = threading.Lock()
        self.auth_error_cooldown = AUTH_ERROR_COOLDOWN_SECONDS
        self.rate_limit_cooldown = RATE_LIMIT_COOLDOWN_SECONDS
        self.generic_error_cooldown = GENERIC_ERROR_COOLDOWN_SECONDS
        # 浏览器会话管理: {account_idx: {browser, context, page, last_refresh_time, playwright, latest_cookies}}
        # latest_cookies 用于线程安全地存储最新的 Cookie（避免跨线程访问浏览器对象）
        self.browser_sessions = {}
        
        # 数据库支持
        self.use_database = False
        self._init_storage()
    
    def _init_storage(self):
        """初始化存储系统（数据库或 JSON）"""
        try:
            from .database import init_db, SessionLocal, Account
            from .migration import migrate_json_to_db
            
            # 初始化数据库
            init_db()
            
            # 检查数据库是否有数据
            db = SessionLocal()
            try:
                account_count = db.query(Account).count()
                if account_count > 0:
                    # 数据库有数据，使用数据库
                    self.use_database = True
                    print("[存储] 使用数据库存储")
                else:
                    # 数据库为空，检查 JSON
                    if CONFIG_FILE.exists():
                        # 自动迁移 JSON 到数据库
                        print("[存储] 数据库为空，检测到 JSON 配置，开始自动迁移...")
                        if migrate_json_to_db():
                            self.use_database = True
                            print("[存储] ✓ 已切换到数据库存储")
                        else:
                            print("[存储] 迁移失败，继续使用 JSON")
                            self.use_database = False
                    else:
                        print("[存储] 使用数据库存储（新安装）")
                        self.use_database = True
            finally:
                db.close()
        except ImportError:
            # SQLAlchemy 未安装，使用 JSON
            print("[存储] SQLAlchemy 未安装，使用 JSON 存储")
            self.use_database = False
        except Exception as e:
            print(f"[存储] 初始化数据库失败: {e}，使用 JSON 存储")
            self.use_database = False
    
    def load_config(self):
        """加载配置（支持数据库和 JSON）"""
        # 确保 config 至少是空字典
        if self.config is None:
            self.config = {}
        try:
            if self.use_database:
                return self._load_from_db()
            else:
                return self._load_from_json()
        except Exception as e:
            from .logger import print
            print(f"[配置加载] 加载配置失败: {e}", _level="ERROR")
            # 确保 config 至少是空字典
            if self.config is None:
                self.config = {}
            return self.config
    
    def _load_from_db(self):
        """从数据库加载配置"""
        # 确保 config 至少是空字典
        if self.config is None:
            self.config = {}
        try:
            from .database import SessionLocal, Account, Model, SystemConfig
            db = SessionLocal()
            try:
                # 加载系统配置
                system_configs = db.query(SystemConfig).all()
                self.config = {}
                for sc in system_configs:
                    value = sc.value
                    # 类型转换
                    if sc.value_type == "bool":
                        value = value.lower() == "true"
                    elif sc.value_type == "int":
                        try:
                            value = int(value)
                        except:
                            pass
                    elif sc.value_type == "json":
                        try:
                            value = json.loads(value)
                        except:
                            pass
                    self.config[sc.key] = value
                
                # 处理特殊配置
                if "log_level" in self.config:
                    try:
                        set_log_level(self.config.get("log_level"), persist=False)
                    except Exception:
                        pass
                if "admin_secret_key" in self.config:
                    from . import auth
                    auth.ADMIN_SECRET_KEY = self.config.get("admin_secret_key")
                
                # 加载账号
                accounts_db = db.query(Account).order_by(Account.id).all()
                self.accounts = []
                for acc in accounts_db:
                    self.accounts.append({
                        "team_id": acc.team_id,
                        "secure_c_ses": acc.secure_c_ses,
                        "host_c_oses": acc.host_c_oses,
                        "csesidx": acc.csesidx,
                        "user_agent": acc.user_agent,
                        "available": acc.available,
                        "tempmail_url": acc.tempmail_url,
                        "tempmail_name": acc.tempmail_name,
                        "quota_usage": acc.quota_usage,
                        "quota_reset_date": acc.quota_reset_date,
                    })
                
                # 加载模型
                models_db = db.query(Model).order_by(Model.id).all()
                self.config["models"] = []
                for model in models_db:
                    self.config["models"].append({
                        "id": model.model_id,
                        "name": model.name,
                        "description": model.description,
                        "api_model_id": model.api_model_id,
                        "context_length": model.context_length,
                        "max_tokens": model.max_tokens,
                        "price_per_1k_tokens": model.price_per_1k_tokens,
                        "enabled": model.enabled,
                        "account_index": model.account_index,
                    })
                
                # 初始化账号状态（同原有逻辑）
                need_save = False
                with self.lock:
                    for i, acc in enumerate(self.accounts):
                        available = acc.get("available", True)
                        # 被动检测模式：不再维护配额使用量字段
                        quota_usage = {}  # 保留字段用于向后兼容，但不再使用
                        quota_reset_date = None  # 保留字段用于向后兼容，但不再使用
                    
                        self.account_states[i] = {
                            "jwt": None,
                            "jwt_time": 0,
                            "session": None,
                            "available": available,
                            "cooldown_until": acc.get("cooldown_until"),
                            "cooldown_reason": acc.get("unavailable_reason") or acc.get("cooldown_reason") or "",
                            "quota_usage": quota_usage,  # 保留用于向后兼容
                            "quota_reset_date": quota_reset_date,  # 保留用于向后兼容
                            "cookie_expired": acc.get("cookie_expired", False)  # 同步 cookie_expired 状态
                        }
                
                if need_save:
                    self.save_config()
                
                return self.config
            finally:
                db.close()
        except Exception as e:
            print(f"[加载] 从数据库加载失败: {e}，回退到 JSON")
            import traceback
            traceback.print_exc()
            self.use_database = False
            # 确保 config 至少是空字典
            if self.config is None:
                self.config = {}
            return self._load_from_json()
    
    def _load_from_json(self):
        """从 JSON 加载配置（原有逻辑）"""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self.config = json.load(f)
                if "log_level" in self.config:
                    try:
                        set_log_level(self.config.get("log_level"), persist=False)
                    except Exception:
                        pass
                if "admin_secret_key" in self.config:
                    from . import auth
                    auth.ADMIN_SECRET_KEY = self.config.get("admin_secret_key")
                self.accounts = self.config.get("accounts", [])
                # 初始化账号状态
                need_save = False
                with self.lock:
                    for i, acc in enumerate(self.accounts):
                        available = acc.get("available", True)  # 默认可用
                        # 被动检测模式：不再维护配额使用量字段
                        # quota_usage 和 quota_reset_date 字段保留用于向后兼容，但不再使用
                        quota_usage = {}  # 不再读取，使用空字典
                        quota_reset_date = None  # 不再读取
                    
                        self.account_states[i] = {
                            "jwt": None,
                            "jwt_time": 0,
                            "session": None,
                            "available": available,
                            "cooldown_until": acc.get("cooldown_until"),
                            "cooldown_reason": acc.get("unavailable_reason") or acc.get("cooldown_reason") or "",
                            "quota_usage": quota_usage,
                            "quota_reset_date": quota_reset_date,
                            "cookie_expired": acc.get("cookie_expired", False)  # 同步 cookie_expired 状态
                        }
                
                # 在释放锁后保存配置，避免阻塞
                if need_save:
                    self.save_config()
        return self.config
    
    def save_config(self):
        """保存配置（支持数据库和 JSON）"""
        if self.use_database:
            self._save_to_db()
        else:
            self._save_to_json()
    
    def _save_to_db(self):
        """保存到数据库"""
        try:
            from .database import SessionLocal, Account, Model, SystemConfig
            
            if not self.config:
                return
            
            db = SessionLocal()
            try:
                # 保存系统配置
                for key, value in self.config.items():
                    if key not in ["accounts", "models"]:
                        # 确定值类型
                        value_type = "string"
                        if isinstance(value, bool):
                            value_type = "bool"
                        elif isinstance(value, int):
                            value_type = "int"
                        elif isinstance(value, (list, dict)):
                            value_type = "json"
                            value = json.dumps(value, ensure_ascii=False)
                        
                        existing = db.query(SystemConfig).filter(
                            SystemConfig.key == key
                        ).first()
                        if existing:
                            existing.value = str(value)
                            existing.value_type = value_type
                        else:
                            db.add(SystemConfig(key=key, value=str(value), value_type=value_type))
                
                # 保存账号
                for i, acc_data in enumerate(self.accounts):
                    # 通过 ID 查找账号（ID 从 1 开始）
                    account = db.query(Account).filter(Account.id == i + 1).first()
                    if account:
                        # 更新现有账号
                        account.team_id = acc_data.get("team_id")
                        account.secure_c_ses = acc_data.get("secure_c_ses")
                        account.host_c_oses = acc_data.get("host_c_oses")
                        account.csesidx = acc_data.get("csesidx")
                        account.user_agent = acc_data.get("user_agent")
                        account.available = acc_data.get("available", True)
                        account.tempmail_url = acc_data.get("tempmail_url")
                        account.tempmail_name = acc_data.get("tempmail_name")
                        # 被动检测模式：不再维护配额使用量字段
                        # 保留字段用于向后兼容，但不再读取或更新
                        # account.quota_usage = acc_data.get("quota_usage", {})
                        # account.quota_reset_date = acc_data.get("quota_reset_date")
                    else:
                        # 新建账号
                        account = Account(
                            team_id=acc_data.get("team_id"),
                            secure_c_ses=acc_data.get("secure_c_ses"),
                            host_c_oses=acc_data.get("host_c_oses"),
                            csesidx=acc_data.get("csesidx"),
                            user_agent=acc_data.get("user_agent"),
                            available=acc_data.get("available", True),
                            tempmail_url=acc_data.get("tempmail_url"),
                            tempmail_name=acc_data.get("tempmail_name"),
                            # 被动检测模式：不再维护配额使用量字段
                            quota_usage=None,  # 保留字段用于向后兼容
                            quota_reset_date=None,  # 保留字段用于向后兼容
                        )
                        db.add(account)
                
                # 保存模型
                models = self.config.get("models", [])
                for model_data in models:
                    model_id = model_data.get("id")
                    if not model_id:
                        continue
                    
                    existing = db.query(Model).filter(
                        Model.model_id == model_id
                    ).first()
                    
                    if existing:
                        # 更新现有模型
                        existing.name = model_data.get("name", existing.name)
                        existing.description = model_data.get("description", existing.description)
                        existing.api_model_id = model_data.get("api_model_id", existing.api_model_id)
                        existing.context_length = model_data.get("context_length", existing.context_length)
                        existing.max_tokens = model_data.get("max_tokens", existing.max_tokens)
                        existing.price_per_1k_tokens = model_data.get("price_per_1k_tokens", existing.price_per_1k_tokens)
                        existing.enabled = model_data.get("enabled", existing.enabled)
                        existing.account_index = model_data.get("account_index", existing.account_index)
                    else:
                        # 新建模型
                        model = Model(
                            model_id=model_id,
                            name=model_data.get("name", ""),
                            description=model_data.get("description"),
                            api_model_id=model_data.get("api_model_id"),
                            context_length=model_data.get("context_length", 32768),
                            max_tokens=model_data.get("max_tokens", 8192),
                            price_per_1k_tokens=model_data.get("price_per_1k_tokens"),
                            enabled=model_data.get("enabled", True),
                            account_index=model_data.get("account_index", 0),
                        )
                        db.add(model)
                
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[保存] ✗ 保存到数据库失败: {e}")
                import traceback
                traceback.print_exc()
            finally:
                db.close()
        except ImportError:
            # SQLAlchemy 未安装，回退到 JSON
            self._save_to_json()
        except Exception as e:
            print(f"[保存] ✗ 保存到数据库失败: {e}，回退到 JSON")
            self._save_to_json()
    
    def _save_to_json(self):
        """保存到 JSON（原有逻辑）"""
        if self.config and CONFIG_FILE.exists():
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
    
    def mark_account_unavailable(self, index: int, reason: str = ""):
        """标记账号不可用"""
        need_save = False
        cookie_expired = False
        with self.lock:
            if 0 <= index < len(self.accounts):
                self.accounts[index]["available"] = False
                self.accounts[index]["unavailable_reason"] = reason
                self.accounts[index]["unavailable_time"] = datetime.now().isoformat()
                self.account_states[index]["available"] = False
                # 检测是否是 Cookie 过期
                if "401" in reason or "403" in reason or "认证失败" in reason:
                    self.accounts[index]["cookie_expired"] = True
                    self.accounts[index]["cookie_expired_time"] = datetime.now().isoformat()
                    self.account_states[index]["cookie_expired"] = True  # 同时更新 account_states
                    cookie_expired = True
                    print(f"[!] 账号 {index} Cookie 可能已过期，需要刷新")
                need_save = True
                print(f"[!] 账号 {index} 已标记为不可用: {reason}")
        
        # 在释放锁后保存配置，避免阻塞
        if need_save:
            self.save_config()
        
        # 如果检测到 Cookie 过期且自动刷新已启用，立即触发刷新检查
        if cookie_expired:
            auto_refresh_enabled = self.config.get("auto_refresh_cookie", False)
            if auto_refresh_enabled:
                try:
                    # 使用延迟导入避免循环导入
                    import sys
                    cookie_refresh_module = sys.modules.get('app.cookie_refresh')
                    if cookie_refresh_module and hasattr(cookie_refresh_module, '_immediate_refresh_event'):
                        cookie_refresh_module._immediate_refresh_event.set()
                        print(f"[Cookie 自动刷新] ⚡ 账号 {index} Cookie 过期，已触发立即刷新检查")
                except (ImportError, AttributeError):
                    # cookie_refresh 模块可能还未加载，忽略
                    pass
    
    def mark_cookie_refreshed(self, index: int):
        """标记账号 Cookie 已刷新"""
        need_save = False
        with self.lock:
            if 0 <= index < len(self.accounts):
                if "cookie_expired" in self.accounts[index] or "cookie_expired_time" in self.accounts[index]:
                    self.accounts[index].pop("cookie_expired", None)
                    self.accounts[index].pop("cookie_expired_time", None)
                    need_save = True
                # 同时清除 account_states 中的 cookie_expired 标记
                if index in self.account_states and "cookie_expired" in self.account_states[index]:
                    self.account_states[index].pop("cookie_expired", None)
                
                # 清除冷却状态（Cookie 已刷新，账号应该立即恢复可用）
                state = self.account_states.get(index, {})
                if "cooldown_until" in state:
                    state.pop("cooldown_until", None)
                    need_save = True
                if "cooldown_reason" in state:
                    state.pop("cooldown_reason", None)
                if "cooldown_until" in self.accounts[index]:
                    self.accounts[index].pop("cooldown_until", None)
                    need_save = True
                
                # 恢复账号可用状态
                self.accounts[index]["available"] = True
                state["available"] = True
                
                print(f"[✓] 账号 {index} Cookie 已刷新，冷却状态已清除")
        
        # 在释放锁后保存配置，避免阻塞
        if need_save:
            self.save_config()

    def mark_quota_error(self, index: int, status_code: int, detail: str = "", quota_type: Optional[str] = None):
        """标记账号配额错误（被动检测方式，支持按配额类型冷却）
        
        Args:
            index: 账号索引
            status_code: HTTP 状态码（401, 403, 429 表示配额/权限错误）
            detail: 错误详情
            quota_type: 配额类型（"images", "videos", "text_queries"），如果为 None 则冷却整个账号
        """
        need_save = False
        with self.lock:
            if 0 <= index < len(self.accounts):
                now_ts = time.time()
                
                # 429 通常是配额超限，按配额类型冷却到第二天 PT 午夜；401/403 是认证错误，冷却整个账号（短时间）
                if status_code == 429:
                    # 如果是配额错误且指定了配额类型，冷却到第二天 PT 午夜
                    # if quota_type:
                    #     from .utils import seconds_until_next_pt_midnight
                    #     cooldown_seconds = seconds_until_next_pt_midnight(now_ts)
                    #     # cooldown_seconds = 3600
                    # else:
                    #     # 未指定配额类型，使用短时间冷却
                    #     cooldown_seconds = self.rate_limit_cooldown
                    cooldown_seconds = 10
                elif status_code in (401, 403):
                    # 认证错误，使用短时间冷却
                    cooldown_seconds = self.auth_error_cooldown
                else:
                    # 其他错误，使用通用冷却时间
                    cooldown_seconds = self.generic_error_cooldown
                
                new_until = now_ts + cooldown_seconds
                print(f"[!] 账号 {index} {quota_type} 配额错误 (HTTP {status_code})，进入冷却 {cooldown_seconds} 秒")
                state = self.account_states.setdefault(index, {})
                
                if quota_type:
                    # 按配额类型冷却
                    if "quota_type_cooldowns" not in state:
                        state["quota_type_cooldowns"] = {}
                    
                    current_until = state["quota_type_cooldowns"].get(quota_type, 0)
                    # 如果已有更长的冷却，则不重复更新
                    if current_until > now_ts and current_until >= new_until:
                        print(f"[!] 账号 {index} {quota_type} 配额错误 (HTTP {status_code})，已有更长冷却期，跳过")
                        return
                    
                    until = max(new_until, current_until)
                    state["quota_type_cooldowns"][quota_type] = until
                    reason = f"{quota_type} 配额错误 (HTTP {status_code})"
                    if detail:
                        reason += f": {detail[:100]}"
                    
                    # 记录配额错误信息（用于前端显示）
                    if "quota_errors" not in self.accounts[index]:
                        self.accounts[index]["quota_errors"] = []
                    quota_error = {
                        "status_code": status_code,
                        "quota_type": quota_type,
                        "detail": detail[:200] if detail else "",
                        "time": datetime.now().isoformat()
                    }
                    # 只保留最近 5 条错误记录
                    self.accounts[index]["quota_errors"].append(quota_error)
                    if len(self.accounts[index]["quota_errors"]) > 5:
                        self.accounts[index]["quota_errors"] = self.accounts[index]["quota_errors"][-5:]
                    
                    # 格式化冷却时间显示
                    if status_code == 429 and quota_type:
                        hours = cooldown_seconds // 3600
                        minutes = (cooldown_seconds % 3600) // 60
                        print(f"[!] 账号 {index} {quota_type} 配额错误 (HTTP {status_code})，该类型进入冷却直到第二天 PT 午夜（约 {hours} 小时 {minutes} 分钟）")
                    else:
                        print(f"[!] 账号 {index} {quota_type} 配额错误 (HTTP {status_code})，该类型进入冷却 {cooldown_seconds} 秒")
                else:
                    # 冷却整个账号（用于 401/403 等认证错误）
                    current_until = state.get("cooldown_until") or 0
                    # 如果已有更长的冷却，则不重复更新
                    if current_until > now_ts and current_until >= new_until:
                        return

                    until = max(new_until, current_until)
                    state["cooldown_until"] = until
                    reason = f"配额/权限错误 (HTTP {status_code})"
                    if detail:
                        reason += f": {detail[:100]}"
                    state["cooldown_reason"] = reason
                    state["jwt"] = None
                    state["jwt_time"] = 0
                    state["session"] = None

                    # 在配置中记录冷却信息，便于前端展示
                    self.accounts[index]["cooldown_until"] = until
                    self.accounts[index]["unavailable_reason"] = reason
                    self.accounts[index]["unavailable_time"] = datetime.now().isoformat()
                    
                    # 记录配额错误信息（用于前端显示）
                    if "quota_errors" not in self.accounts[index]:
                        self.accounts[index]["quota_errors"] = []
                    quota_error = {
                        "status_code": status_code,
                        "detail": detail[:200] if detail else "",
                        "time": datetime.now().isoformat()
                    }
                    # 只保留最近 5 条错误记录
                    self.accounts[index]["quota_errors"].append(quota_error)
                    if len(self.accounts[index]["quota_errors"]) > 5:
                        self.accounts[index]["quota_errors"] = self.accounts[index]["quota_errors"][-5:]
                    
                    print(f"[!] 账号 {index} 检测到配额/权限错误 (HTTP {status_code})，整个账号进入冷却 {cooldown_seconds} 秒")

                need_save = True
        
        # 在释放锁后保存配置，避免阻塞
        if need_save:
            self.save_config()
    
    def _is_quota_type_in_cooldown(self, index: int, quota_type: str, now_ts: Optional[float] = None) -> bool:
        """检查账号的特定配额类型是否处于冷却期"""
        now_ts = now_ts or time.time()
        state = self.account_states.get(index, {})
        quota_type_cooldowns = state.get("quota_type_cooldowns", {})
        cooldown_until = quota_type_cooldowns.get(quota_type)
        if not cooldown_until:
            return False
        return now_ts < cooldown_until

    def mark_account_cooldown(self, index: int, reason: str = "", cooldown_seconds: Optional[int] = None):
        """临时拉黑账号（冷却），在冷却时间内不会被选择"""
        if cooldown_seconds is None:
            cooldown_seconds = self.generic_error_cooldown

        need_save = False
        with self.lock:
            if 0 <= index < len(self.accounts):
                now_ts = time.time()
                new_until = now_ts + cooldown_seconds
                state = self.account_states.setdefault(index, {})
                current_until = state.get("cooldown_until") or 0
                # 如果已有更长的冷却，则不重复更新
                if current_until > now_ts and current_until >= new_until:
                    return

                until = max(new_until, current_until)
                state["cooldown_until"] = until
                state["cooldown_reason"] = reason
                state["jwt"] = None
                state["jwt_time"] = 0
                state["session"] = None

                # 在配置中记录冷却信息，便于前端展示
                self.accounts[index]["cooldown_until"] = until
                self.accounts[index]["unavailable_reason"] = reason
                self.accounts[index]["unavailable_time"] = datetime.now().isoformat()

                need_save = True
                print(f"[!] 账号 {index} 进入冷却 {cooldown_seconds} 秒: {reason}")
        
        # 在释放锁后保存配置，避免阻塞
        if need_save:
            self.save_config()

    def _is_in_cooldown(self, index: int, now_ts: Optional[float] = None) -> bool:
        """检查账号是否处于冷却期"""
        now_ts = now_ts or time.time()
        state = self.account_states.get(index, {})
        cooldown_until = state.get("cooldown_until")
        if not cooldown_until:
            return False
        return now_ts < cooldown_until

    def get_next_cooldown_info(self) -> Optional[dict]:
        """获取最近即将结束冷却的账号信息"""
        now_ts = time.time()
        candidates = []
        for idx, state in self.account_states.items():
            cooldown_until = state.get("cooldown_until")
            if cooldown_until and cooldown_until > now_ts and state.get("available", True):
                candidates.append((cooldown_until, idx))
        if not candidates:
            return None
        cooldown_until, idx = min(candidates, key=lambda x: x[0])
        return {"index": idx, "cooldown_until": cooldown_until}

    def is_account_available(self, index: int, quota_type: Optional[str] = None) -> bool:
        """计算账号当前是否可用（考虑冷却和手动禁用）
        
        Args:
            index: 账号索引
            quota_type: 配额类型（"images", "videos", "text_queries"），如果提供，则检查该配额类型是否可用
        """
        state = self.account_states.get(index, {})
        if not state.get("available", True):
            return False
        if self._is_in_cooldown(index):
            return False
        
        # 如果指定了配额类型，检查该配额类型是否在冷却期
        if quota_type:
            if self._is_quota_type_in_cooldown(index, quota_type):
                return False
        
        return True
    
    def get_available_accounts(self, quota_type: Optional[str] = None):
        """获取可用账号列表
        
        Args:
            quota_type: 配额类型（"images", "videos", "text_queries"），如果提供，则只返回该配额类型可用的账号
        """
        now_ts = time.time()
        available_accounts = []
        for i, acc in enumerate(self.accounts):
            state = self.account_states.get(i, {})
            if not state.get("available", True):
                continue
            if self._is_in_cooldown(i, now_ts):
                continue
            
            # 如果指定了配额类型，检查该配额类型是否在冷却期
            if quota_type:
                if self._is_quota_type_in_cooldown(i, quota_type, now_ts):
                    continue
            
            available_accounts.append((i, acc))
        return available_accounts
    
    def get_next_account(self, quota_type: Optional[str] = None):
        """轮训获取下一个可用账号
        
        Args:
            quota_type: 可选的配额类型，如果提供，则只返回该配额可用的账号
        """
        with self.lock:
            available = self.get_available_accounts(quota_type)
            if not available:
                cooldown_info = self.get_next_cooldown_info()
                if cooldown_info:
                    remaining = int(max(0, cooldown_info["cooldown_until"] - time.time()))
                    raise NoAvailableAccount(f"没有可用的账号（最近冷却账号 {cooldown_info['index']}，约 {remaining} 秒后可重试）")
                raise NoAvailableAccount("没有可用的账号")
            
            # 轮训选择
            self.current_index = self.current_index % len(available)
            idx, account = available[self.current_index]
            self.current_index = (self.current_index + 1) % len(available)
            return idx, account
    
    def _get_current_date_str(self) -> str:
        """获取当前日期字符串（PT时区）"""
        now_utc = datetime.now(timezone.utc)
        if ZoneInfo:
            try:
                pt_tz = ZoneInfo("America/Los_Angeles")
                now_pt = now_utc.astimezone(pt_tz)
            except Exception:
                # 如果时区数据不可用（如缺少 tzdata 包），使用回退方案
                # 兼容旧版本 Python 的简易回退（不考虑夏令时）
                now_pt = now_utc - timedelta(hours=8)
        else:
            # 兼容旧版本 Python 的简易回退（不考虑夏令时）
            now_pt = now_utc - timedelta(hours=8)
        return now_pt.date().isoformat()
    
    def _check_and_reset_quota(self, account_idx: int, quota_reset_date: Optional[str] = None):
        """检查并重置配额（已弃用，被动检测模式不再使用此方法）
        
        注意：此方法保留用于向后兼容，但被动检测模式下不再调用。
        配额管理改为通过 HTTP 错误码（401/403/429）被动检测。
        """
        # 被动检测模式：不再重置配额使用量
        pass
    
    def check_quota(self, account_idx: int, quota_type: str) -> tuple[bool, dict]:
        """检查账号配额是否足够（已弃用，被动检测模式不再使用此方法）
        
        注意：此方法保留用于向后兼容，但被动检测模式下不再调用。
        配额检查改为通过 HTTP 错误码（401/403/429）被动检测。
        """
        # 被动检测模式：始终返回可用（实际配额通过错误检测管理）
        return True, {
            "quota_type": quota_type,
            "current": 0,  # 不再计数
            "limit": None,  # 不再维护限制值
            "remaining": None,  # 不再计算剩余
            "reset_date": None,
            "note": "被动检测模式：配额通过 HTTP 错误码管理"
        }
    
    def record_quota_usage(self, account_idx: int, quota_type: str, count: int = 1):
        """记录配额使用量（已弃用，被动检测模式不再使用此方法）
        
        注意：此方法保留用于向后兼容，但被动检测模式下不再调用。
        配额管理改为通过 HTTP 错误码（401/403/429）被动检测。
        """
        # 被动检测模式：不再记录配额使用量
        pass
    
    def get_quota_info(self, account_idx: int) -> dict:
        """获取账号配额信息（快速版本，最小化锁持有时间）"""
        # 快速边界检查（无锁）
        if account_idx < 0:
            return {}
        
        # 获取数据快照（最小化锁持有时间）
        quota_usage = {}
        quota_reset_date = None
        try:
            with self.lock:
                # 边界检查
                if account_idx >= len(self.accounts):
                    return {}
                
                if account_idx not in self.account_states:
                    return {}
                
                state = self.account_states[account_idx]
                cooldown_until = state.get("cooldown_until")
                cooldown_reason = state.get("cooldown_reason", "")
                quota_type_cooldowns = state.get("quota_type_cooldowns", {})
                quota_errors = self.accounts[account_idx].get("quota_errors", [])
            
            # 在锁外构建返回数据，避免阻塞
            now_ts = time.time()
            is_in_cooldown = cooldown_until and cooldown_until > now_ts
            cooldown_remaining = max(0, int(cooldown_until - now_ts)) if is_in_cooldown else 0
            
            quota_info = {
                "mode": "passive_detection",  # 标记为被动检测模式
                "status": "available" if not is_in_cooldown else "cooldown",
                "cooldown_until": cooldown_until,
                "cooldown_remaining": cooldown_remaining,
                "cooldown_reason": cooldown_reason,
                "quota_errors": quota_errors[-5:] if quota_errors else [],  # 最近5条错误记录
                "quota_types": {}
            }
            
            # 从实际的配额错误记录中动态提取配额类型（被动检测模式）
            # 收集所有出现过的配额类型（从冷却记录和错误记录中）
            all_quota_types = set()
            all_quota_types.update(quota_type_cooldowns.keys())
            for err in quota_errors:
                if err.get("quota_type"):
                    all_quota_types.add(err.get("quota_type"))
            
            # 为每种实际出现过的配额类型显示状态
            for quota_type in all_quota_types:
                # 检查该配额类型是否在冷却期
                type_cooldown_until = quota_type_cooldowns.get(quota_type)
                is_type_in_cooldown = type_cooldown_until and type_cooldown_until > now_ts
                type_cooldown_remaining = max(0, int(type_cooldown_until - now_ts)) if is_type_in_cooldown else 0
                
                # 检查是否有该类型的配额错误
                has_error = any(
                    err.get("quota_type") == quota_type and err.get("status_code") in (401, 403, 429)
                    for err in quota_errors
                )
                
                # 确定状态：如果该类型在冷却，显示冷却；如果有错误但不在冷却，显示错误；否则显示可用
                if is_type_in_cooldown:
                    status = "cooldown"
                    status_text = f"冷却中（剩余 {type_cooldown_remaining // 3600} 小时 {(type_cooldown_remaining % 3600) // 60} 分钟）"
                    status_class = "status-warning"
                elif has_error:
                    status = "error"
                    status_text = "错误"
                    status_class = "status-error"
                else:
                    status = "available"
                    status_text = "可用"
                    status_class = "status-success"
                
                quota_info["quota_types"][quota_type] = {
                    "status": status,
                    "status_text": status_text,
                    "status_class": status_class,
                    "cooldown_until": type_cooldown_until,
                    "cooldown_remaining": type_cooldown_remaining,
                    "note": "配额通过被动检测（HTTP 错误码）管理"
                }
            
            return quota_info
            
        except Exception as e:
            from .logger import print
            print(f"[错误] 获取账号 {account_idx} 配额信息时发生异常: {e}", _level="ERROR")
            import traceback
            print(traceback.format_exc(), _level="ERROR")
            return {}
    
    def get_account_count(self):
        """获取账号数量统计"""
        total = len(self.accounts)
        available = len(self.get_available_accounts())
        return total, available


# 全局账号管理器实例
account_manager = AccountManager()

