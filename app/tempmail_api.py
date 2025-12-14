"""
临时邮箱 API 客户端
支持 cloudflare_temp_email 项目的 API
"""

import json
import time
import base64
import requests
import quopri
from typing import Optional, List, Dict
from urllib.parse import urlparse, parse_qs, unquote
# 尝试导入 logger，如果失败则使用 print
try:
    from .logger import print as log_print
except ImportError:
    def log_print(msg, _level="INFO"):
        print(f"[{_level}] {msg}")


class TempMailAPIClient:
    """临时邮箱 API 客户端"""
    
    def __init__(self, tempmail_url: str, worker_url: Optional[str] = None):
        """初始化客户端
        
        Args:
            tempmail_url: 临时邮箱 URL（包含 JWT token）
            worker_url: Worker API 地址（可选，如果不提供则从 tempmail_url 提取）
        """
        self.tempmail_url = tempmail_url
        self.jwt_token = self._extract_jwt()
        
        # 优先使用提供的 worker_url，否则从 tempmail_url 提取
        if worker_url:
            self.worker_url = worker_url.rstrip('/')
        else:
            self.worker_url = self._extract_worker_url()
        
        if not self.jwt_token:
            raise ValueError("无法从 URL 中提取 JWT token")
        
        # 记录已处理的最大邮件 ID（用于重试模式）
        self.last_max_id = 0
        
        # 初始化信息（简化）
        # log_print(f"[临时邮箱 API] 初始化成功\n  Worker URL: {self.worker_url}\n  JWT 长度: {len(self.jwt_token) if self.jwt_token else 0}")
    
    def _extract_jwt(self) -> Optional[str]:
        """从 URL 中提取 JWT token"""
        try:
            parsed = urlparse(self.tempmail_url)
            params = parse_qs(parsed.query)
            if 'jwt' in params:
                return params['jwt'][0]
        except Exception as e:
            log_print(f"[临时邮箱 API] 提取 JWT 失败: {e}", _level="WARNING")
        return None
    
    def _extract_worker_url(self) -> str:
        """提取 Worker 基础 URL"""
        parsed = urlparse(self.tempmail_url)
        return f"{parsed.scheme}://{parsed.netloc}"
    
    def get_email_address(self) -> Optional[str]:
        """从 JWT token 中提取邮箱地址"""
        if not self.jwt_token:
            return None
        
        try:
            parts = self.jwt_token.split('.')
            if len(parts) < 2:
                return None
            
            payload = parts[1]
            padding = '=' * (4 - len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload + padding)
            data = json.loads(decoded)
            
            if 'address' in data:
                return data['address']
        except Exception as e:
            log_print(f"[临时邮箱 API] 从 JWT 提取邮箱失败: {e}", _level="WARNING")
        
        return None
    
    def get_mails(
        self,
        limit: int = 20,
        offset: int = 0,
        keyword: Optional[str] = None,
        address: Optional[str] = None
    ) -> List[Dict]:
        """获取邮件列表
        
        Args:
            limit: 返回邮件数量限制
            offset: 偏移量（分页）
            keyword: 关键词过滤（可选）
        
        Returns:
            邮件列表
        """
        try:
            url = f"{self.worker_url}/api/mails"
            params = {
                "limit": limit,
                "offset": offset
            }
            if keyword:
                params["keyword"] = keyword
            if address:
                params["address"] = address
            
            headers = {
                "Authorization": f"Bearer {self.jwt_token}",
                "Content-Type": "application/json"
            }
            
            # 调试信息（已关闭）
            # if not hasattr(self, '_debug_logged'):
            #     log_print(f"[临时邮箱 API] 请求信息:\n  URL: {url}\n  Params: {params}\n  JWT 前20字符: {self.jwt_token[:20]}...")
            #     self._debug_logged = True
            
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            # 检查是否返回 HTML（说明请求的是前端地址而不是 Worker 地址）
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                if not hasattr(self, '_html_warning_logged'):
                    log_print(
                        f"[临时邮箱 API] ⚠ 检测到返回 HTML 页面，说明请求的是前端地址而不是 Worker 地址\n"
                        f"  当前 URL: {url}\n"
                        f"  请检查：\n"
                        f"  1. Worker 地址是否与前端地址不同？\n"
                        f"  2. 是否需要在系统设置中配置 'tempmail_worker_url'？\n"
                        f"  3. Worker 地址格式通常是: https://worker-name.your-subdomain.workers.dev\n"
                        f"  响应前200字符: {response.text[:200]}",
                        _level="WARNING"
                    )
                    self._html_warning_logged = True
                return []
            
            # 调试：打印响应信息（仅第一次失败时）
            if response.status_code != 200 or not response.text:
                if not hasattr(self, '_error_logged'):
                    log_print(
                        f"[临时邮箱 API] 请求详情:\n"
                        f"  URL: {url}\n"
                        f"  状态码: {response.status_code}\n"
                        f"  响应头: {dict(response.headers)}\n"
                        f"  响应内容长度: {len(response.text)}\n"
                        f"  响应前500字符: {response.text[:500]}",
                        _level="WARNING"
                    )
                    self._error_logged = True
            
            # 检查响应状态码
            if response.status_code != 200:
                log_print(
                    f"[临时邮箱 API] 获取邮件列表失败: HTTP {response.status_code}\n"
                    f"URL: {url}\n"
                    f"响应: {response.text[:200]}",
                    _level="WARNING"
                )
                return []
            
            # 检查响应内容类型
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/json" not in content_type:
                log_print(
                    f"[临时邮箱 API] 响应不是 JSON 格式: {content_type}\n"
                    f"URL: {url}\n"
                    f"响应前200字符: {response.text[:200]}",
                    _level="WARNING"
                )
                return []
            
            # 检查响应是否为空
            if not response.text or not response.text.strip():
                log_print(
                    f"[临时邮箱 API] 响应为空\n"
                    f"URL: {url}",
                    _level="WARNING"
                )
                return []
            
            # 尝试解析 JSON
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                # 详细错误信息（仅第一次）
                if not hasattr(self, '_json_error_logged'):
                    log_print(
                        f"[临时邮箱 API] JSON 解析失败: {e}\n"
                        f"  URL: {url}\n"
                        f"  状态码: {response.status_code}\n"
                        f"  Content-Type: {response.headers.get('Content-Type', 'N/A')}\n"
                        f"  响应长度: {len(response.text)}\n"
                        f"  响应前500字符: {response.text[:500]}\n"
                        f"  完整响应: {response.text}",
                        _level="WARNING"
                    )
                    self._json_error_logged = True
                return []
            
            # 处理不同的响应格式
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                if "mails" in data:
                    return data["mails"]
                elif "data" in data:
                    return data["data"]
                elif "result" in data:
                    return data["result"]
                elif "results" in data:
                    # 支持 results 格式（如 cloudflare_temp_email）
                    return data["results"]
            
            log_print(
                f"[临时邮箱 API] 未知的响应格式: {type(data)}\n"
                f"响应内容: {str(data)[:200]}",
                _level="WARNING"
            )
            return []
            
        except requests.RequestException as e:
            log_print(
                f"[临时邮箱 API] 请求异常: {e}\n"
                f"URL: {url if 'url' in locals() else 'N/A'}",
                _level="WARNING"
            )
            return []
        except Exception as e:
            log_print(
                f"[临时邮箱 API] 未知错误: {e}\n"
                f"类型: {type(e).__name__}",
                _level="WARNING"
            )
            return []
    
    def get_verification_code(
        self,
        timeout: int = 120,
        retry_mode: bool = False,
        extract_code_func=None
    ) -> Optional[str]:
        """获取验证码
        
        Args:
            timeout: 超时时间（秒）
            retry_mode: 是否为重试模式
            extract_code_func: 验证码提取函数（从 auto_login_with_email 导入）
        
        Returns:
            验证码字符串，如果未找到则返回 None
        """
        # log_print(f"[临时邮箱 API] 使用 API 方式获取验证码...")
        
        # 获取邮箱地址（不显示）
        email_address = self.get_email_address()
        if not email_address:
            log_print(f"[临时邮箱 API] ⚠ 无法从 JWT 中提取邮箱地址", _level="WARNING")
        
        if not extract_code_func:
            # 如果没有提供提取函数，尝试导入
            try:
                import sys
                from pathlib import Path
                project_root = Path(__file__).parent.parent
                if str(project_root) not in sys.path:
                    sys.path.insert(0, str(project_root))
                from auto_login_with_email import extract_verification_code
                extract_code_func = extract_verification_code
            except ImportError:
                log_print("[临时邮箱 API] ✗ 无法导入验证码提取函数", _level="ERROR")
                return None
        
        # 获取目标邮箱地址用于过滤
        target_email = self.get_email_address()
        
        start_time = time.time()
        attempts = 0
        max_attempts = timeout // 5  # 改为每 5 秒检查一次
        # 使用实例变量 last_max_id，以便在重试模式下记住已处理的最大 ID
        last_max_id = self.last_max_id if retry_mode else 0
        last_mail_count = 0  # 记录上次的邮件数量，避免重复打印
        initial_max_id_set = (retry_mode and self.last_max_id > 0)  # 重试模式下，如果已有 last_max_id，则跳过初始处理
        
        # 第一次调用时，先获取当前的最大邮件ID，然后等待新邮件到达
        # 注意：检测到"验证码邮件发送成功"提示后，才调用此函数
        # 所以应该等待新邮件到达，而不是处理现有的最新邮件
        initial_max_id = 0
        if not retry_mode:
            # log_print(f"[临时邮箱 API] 等待验证码邮件（最多 {timeout} 秒）...")
            # 先获取一次邮件列表，记录当前的最大ID（在检测到提示时，邮件可能还没到达）
            try:
                initial_mails = self.get_mails(limit=5)  # 获取多封，确保获取到真正的最大ID
                if initial_mails:
                    initial_max_id = max(mail.get("id", 0) for mail in initial_mails)
                    log_print(f"[临时邮箱 API] 检测到提示时的最大邮件 ID: {initial_max_id}，将等待新邮件（ID > {initial_max_id}）", _level="INFO")
            except:
                pass
            # 等待 10 秒，确保验证码邮件已发送并到达
            # 注意：即使检测到提示，邮件也可能需要10-30秒才能到达邮箱服务器
            # 增加等待时间，减少后续循环中的等待
            log_print(f"[临时邮箱 API] 等待验证码邮件到达（10秒）...", _level="INFO")
            time.sleep(10)
        
        keywords = ['gemini', 'google', 'verify', 'verification', 'code', '验证', '验证码']
        
        while attempts < max_attempts:
            attempts += 1
            elapsed = int(time.time() - start_time)
            
            if elapsed >= timeout:
                log_print(f"[临时邮箱 API] ✗ 超时（{timeout} 秒）未获取到验证码", _level="WARNING")
                break
            
            # 根据模式调整 limit：重试模式下使用更大的 limit 以获取更多邮件
            mail_limit = 50 if retry_mode else 20
            
            # 策略1：先尝试使用关键词和地址过滤（精确匹配）
            mails = []
            strategy_used = None
            for keyword in keywords:
                mails = self.get_mails(limit=mail_limit, keyword=keyword, address=target_email)
                if mails:
                    strategy_used = f"关键词+地址过滤 (keyword='{keyword}', address='{target_email}')"
                    break
            
            # 策略2：如果策略1失败，尝试只使用地址过滤（不使用关键词）
            if not mails and target_email:
                mails = self.get_mails(limit=mail_limit, address=target_email)
                if mails:
                    strategy_used = f"地址过滤 (address='{target_email}')"
                    log_print(f"[临时邮箱 API] 使用地址过滤获取到 {len(mails)} 封邮件（未使用关键词）")
            
            # 策略3：如果策略2也失败，尝试只使用关键词过滤（不使用地址）
            if not mails:
                for keyword in keywords:
                    mails = self.get_mails(limit=mail_limit, keyword=keyword)
                    if mails:
                        strategy_used = f"关键词过滤 (keyword='{keyword}')"
                        log_print(f"[临时邮箱 API] 使用关键词 '{keyword}' 获取到 {len(mails)} 封邮件（未使用地址过滤）")
                        break
            
            # 策略4：如果策略3也失败，获取所有邮件（不使用任何过滤）
            if not mails:
                mails = self.get_mails(limit=mail_limit)
                if mails:
                    strategy_used = "无过滤（获取所有邮件）"
                    log_print(f"[临时邮箱 API] 获取所有邮件（未使用过滤），共 {len(mails)} 封")
            
            # 在重试模式下，如果获取到邮件，显示使用的策略和邮件数量
            if retry_mode and mails and strategy_used:
                log_print(f"[临时邮箱 API] 重试模式：使用策略 '{strategy_used}'，获取到 {len(mails)} 封邮件", _level="INFO")
            
            # 只在有新邮件或邮件数量变化时打印日志
            current_mail_count = len(mails) if mails else 0
            if current_mail_count != last_mail_count:
                # if current_mail_count > 0:
                #     log_print(f"[临时邮箱 API] 获取到 {current_mail_count} 封邮件（目标邮箱: {target_email or '未知'}）")
                last_mail_count = current_mail_count
            
            if not mails:
                if attempts % 4 == 0:  # 每 4 次尝试（约 20 秒）打印一次日志
                    log_print(f"[临时邮箱 API] 等待邮件到达... (已等待 {elapsed} 秒)")
                if not retry_mode:
                    time.sleep(5)  # 改为 5 秒检查一次
                    continue
            
            # 按 ID 排序，获取最新邮件
            mails.sort(key=lambda x: x.get("id", 0), reverse=True)
            
            # 记录当前获取到的最大邮件 ID（用于调试）
            if mails:
                current_max_id = max(mail.get("id", 0) for mail in mails)
                if retry_mode and current_max_id > last_max_id:
                    log_print(f"[临时邮箱 API] 当前邮件列表最大 ID: {current_max_id}，上次处理的最大 ID: {last_max_id}", _level="INFO")
            
            # 第一次调用时，只处理新邮件（ID > initial_max_id），而不是处理现有的最新邮件
            # 这样可以确保处理的是检测到提示后新发送的验证码邮件，而不是之前就存在的旧邮件
            # 但是，如果点击了"重新发送验证码"（last_max_id被更新为当前最大ID），应该允许处理当前最新的邮件
            if not initial_max_id_set:
                if mails:
                    current_max_id = max(mail.get("id", 0) for mail in mails)
                    # 检查是否点击了"重新发送验证码"
                    # 如果last_max_id等于当前最大ID，说明点击了"重新发送验证码"并已更新last_max_id
                    # 此时应该允许处理当前最新的邮件（即使ID <= initial_max_id）
                    resend_clicked = (last_max_id > 0 and last_max_id == current_max_id)
                    
                    if resend_clicked:
                        # 点击了"重新发送验证码"，处理当前最新的邮件
                        mails.sort(key=lambda x: x.get("id", 0), reverse=True)
                        latest_mail = mails[0]
                        latest_id = latest_mail.get("id", 0)
                        new_mails = [latest_mail]
                        log_print(f"[临时邮箱 API] ✓ 检测到重新发送验证码，处理当前最新邮件（ID: {latest_id}）", _level="INFO")
                    else:
                        # 只处理新邮件（ID > initial_max_id），确保是新发送的验证码邮件
                        new_mails = [mail for mail in mails if mail.get("id", 0) > initial_max_id]
                        if new_mails:
                            # 按ID排序，处理最新的新邮件
                            new_mails.sort(key=lambda x: x.get("id", 0), reverse=True)
                            latest_mail = new_mails[0]
                            latest_id = latest_mail.get("id", 0)
                            new_mails = [latest_mail]  # 只处理最新的一封新邮件
                            log_print(f"[临时邮箱 API] ✓ 发现新邮件（ID: {latest_id}），开始处理（检测到提示时的最大ID: {initial_max_id}）", _level="INFO")
                        else:
                            # 没有新邮件且当前最大 ID 仍是提示时的 ID，继续等待，不要误判为重新发送
                            elapsed = int(time.time() - start_time)
                            if attempts % 2 == 0:  # 每 10 秒打印一次等待状态（每2次循环，每次5秒）
                                current_max = max(mail.get("id", 0) for mail in mails) if mails else 0
                                log_print(f"[临时邮箱 API] 等待新邮件到达（检测到提示时的最大ID: {initial_max_id}，当前最大ID: {current_max}，已等待 {elapsed} 秒）...", _level="INFO")
                            if not retry_mode:
                                time.sleep(5)
                            continue
                    
                    # 记录初始最大 ID，如果处理失败，下次将等待 ID > latest_id 的新邮件
                    initial_max_id_set = True
                    # 初始化 processed_max_id，用于跟踪已处理的邮件
                    processed_max_id = last_max_id
                    # 记录初始处理的邮件 ID，用于后续判断是否应该等待新邮件
                    initial_processed_id = latest_id
                else:
                    # 如果没有邮件，继续等待
                    if not retry_mode:
                        time.sleep(5)
                    continue
            else:
                # 后续调用：处理新邮件（ID > last_max_id）或重试同一封邮件（ID == last_max_id）
                new_mails = [mail for mail in mails if mail.get("id", 0) > last_max_id]
                
                # 如果当前邮件列表的最大 ID 仍然小于 last_max_id，说明可能没有获取到最新邮件
                # 尝试增加 limit 或使用不同的策略获取更多邮件
                if mails:
                    current_max_id = max(mail.get("id", 0) for mail in mails)
                    if current_max_id <= last_max_id and retry_mode:
                        # 检查是否已经等待超过10秒，如果是，则直接处理当前最新邮件（避免死循环）
                        elapsed = int(time.time() - start_time)
                        if current_max_id == last_max_id and elapsed >= 10:
                            # 已等待10秒，但新邮件还没到达，直接处理当前最新邮件
                            mails.sort(key=lambda x: x.get("id", 0), reverse=True)
                            latest_mail = mails[0]
                            latest_id = latest_mail.get("id", 0)
                            new_mails = [latest_mail]
                            log_print(f"[临时邮箱 API] ⚠ 已等待10秒，当前最大ID ({current_max_id}) 与上次处理的最大ID ({last_max_id}) 相同，直接处理当前最新邮件（ID: {latest_id}）", _level="WARNING")
                        else:
                            # 当前邮件列表的最大 ID 仍然小于等于 last_max_id，尝试获取更多邮件
                            # 限制尝试次数，避免死循环
                            if not hasattr(self, '_retry_fetch_count'):
                                self._retry_fetch_count = 0
                            self._retry_fetch_count += 1
                            
                            # 如果尝试次数超过5次，直接处理当前最新邮件
                            if self._retry_fetch_count > 5:
                                mails.sort(key=lambda x: x.get("id", 0), reverse=True)
                                latest_mail = mails[0]
                                latest_id = latest_mail.get("id", 0)
                                new_mails = [latest_mail]
                                log_print(f"[临时邮箱 API] ⚠ 尝试获取更多邮件超过5次，直接处理当前最新邮件（ID: {latest_id}）", _level="WARNING")
                                self._retry_fetch_count = 0  # 重置计数器
                            else:
                                log_print(f"[临时邮箱 API] 当前邮件列表最大 ID ({current_max_id}) 未超过上次处理的最大 ID ({last_max_id})，尝试获取更多邮件... (尝试 {self._retry_fetch_count}/5)", _level="INFO")
                                # 尝试使用更大的 limit 或移除过滤条件
                                more_mails = self.get_mails(limit=50)  # 增加 limit 到 50
                                if more_mails:
                                    more_mails.sort(key=lambda x: x.get("id", 0), reverse=True)
                                    more_max_id = max(mail.get("id", 0) for mail in more_mails)
                                    if more_max_id > current_max_id:
                                        log_print(f"[临时邮箱 API] 获取到更多邮件，新的最大 ID: {more_max_id}", _level="INFO")
                                        mails = more_mails
                                        new_mails = [mail for mail in mails if mail.get("id", 0) > last_max_id]
                                        self._retry_fetch_count = 0  # 重置计数器
                                else:
                                    # 如果获取不到更多邮件，等待一下再继续
                                    time.sleep(2)
                
                # 如果没有新邮件，检查是否有同一封邮件可以重试（之前可能提取失败）
                # 注意：只在第一次处理失败时允许重试一次，之后应该等待新邮件
                if not new_mails and mails:
                    latest_mail = mails[0]
                    latest_id = latest_mail.get("id", 0)
                    # 检查是否已经等待超过10秒，如果是，则直接处理当前最新邮件
                    elapsed = int(time.time() - start_time)
                    if current_max_id == last_max_id and elapsed >= 10:
                        # 已等待10秒，但新邮件还没到达，直接处理当前最新邮件
                        new_mails = [latest_mail]
                        log_print(f"[临时邮箱 API] ⚠ 已等待10秒，当前最大ID ({current_max_id}) 与上次处理的最大ID ({last_max_id}) 相同，直接处理当前最新邮件（ID: {latest_id}）", _level="WARNING")
                    elif latest_id == last_max_id and last_max_id == 0:
                        # 第一次处理失败，允许重试一次
                        new_mails = [latest_mail]
                        log_print(f"[临时邮箱 API] 重试处理邮件（ID: {latest_id}）", _level="INFO")
                    elif latest_id > last_max_id:
                        # 有新邮件，应该处理新邮件
                        new_mails = [latest_mail]
                        log_print(f"[临时邮箱 API] 发现新邮件（ID: {latest_id}），开始处理", _level="INFO")
                
                if not new_mails:
                    # 没有新邮件，继续等待
                    if not retry_mode:
                        time.sleep(5)
                    else:
                        # 在重试模式下，如果已等待超过10秒，直接处理当前最新邮件
                        elapsed = int(time.time() - start_time)
                        if mails and elapsed >= 10:
                            latest_mail = mails[0]
                            latest_id = latest_mail.get("id", 0)
                            new_mails = [latest_mail]
                            log_print(f"[临时邮箱 API] ⚠ 重试模式下已等待10秒，直接处理当前最新邮件（ID: {latest_id}）", _level="WARNING")
                        else:
                            time.sleep(2)  # 重试模式下等待时间缩短为2秒
                    if not new_mails:
                        continue
            
            # 处理新邮件
            # 初始化 processed_max_id（如果还没有初始化）
            if 'processed_max_id' not in locals():
                processed_max_id = last_max_id
            # 记录处理前的 last_max_id，用于判断是否是第一次处理
            before_process_max_id = last_max_id
            for mail in new_mails:
                mail_id = mail.get("id", 0)
                mail_source = mail.get("source", "未知发件人")
                mail_subject = mail.get("subject", "无主题")
                
                # 更新已处理的最大 ID（即使没有找到验证码，也记录已处理过）
                if mail_id > processed_max_id:
                    processed_max_id = mail_id
                
                    # 调试信息（已关闭）
                    # if not hasattr(self, '_mail_fields_logged'):
                    #     log_print(f"[临时邮箱 API] 邮件对象字段: {list(mail.keys())}")
                    #     self._mail_fields_logged = True
                
                # 获取邮件文本内容（尝试多种字段）
                # 优先顺序：text -> 详情API -> raw -> html -> content -> body
                mail_text = mail.get("text", "")
                
                # 如果 text 字段为空，优先尝试调用详情 API 获取干净的邮件内容
                if not mail_text:
                    try:
                        detail_url = f"{self.worker_url}/api/mails/{mail_id}"
                        headers = {
                            "Authorization": f"Bearer {self.jwt_token}",
                            "Content-Type": "application/json"
                        }
                        detail_response = requests.get(detail_url, headers=headers, timeout=30)
                        if detail_response.status_code == 200:
                            detail_data = detail_response.json()
                            # 优先使用 text 字段（最干净）
                            mail_text = detail_data.get("text", "")
                            if not mail_text:
                                html_content = detail_data.get("html", "") or detail_data.get("content", "") or detail_data.get("body", "")
                                if html_content:
                                    import re
                                    mail_text = re.sub(r'<[^>]+>', '', html_content)
                                    mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                    except Exception as e:
                        log_print(f"[临时邮箱 API] ⚠ 获取邮件详情失败 (ID {mail_id}): {e}", _level="WARNING")
                
                # 如果仍然没有内容，尝试 raw 字段（需要解析邮件格式）
                if not mail_text:
                    raw_content = mail.get("raw", "")
                    if raw_content:
                        # raw 字段包含完整的邮件原始内容（包括邮件头部）
                        # 需要解析邮件格式，提取正文部分，跳过邮件头部
                        import re
                        
                        # 方法1：查找 HTML 正文部分（最可靠）
                        html_match = re.search(r'<html[^>]*>.*?</html>', raw_content, re.DOTALL | re.IGNORECASE)
                        if html_match:
                            raw_content = html_match.group(0)
                            # 移除 HTML 标签
                            mail_text = re.sub(r'<[^>]+>', '', raw_content)
                            mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                        else:
                            # 方法2：查找邮件正文的开始位置（邮件头部和正文之间通常有一个空行）
                            # 查找第一个连续的空行（\r\n\r\n 或 \n\n），之后就是正文
                            body_start = -1
                            # 尝试查找 \r\n\r\n
                            body_start = raw_content.find('\r\n\r\n')
                            if body_start == -1:
                                # 尝试查找 \n\n
                                body_start = raw_content.find('\n\n')
                            
                            if body_start > 0:
                                # 找到正文开始位置，提取正文部分
                                raw_content = raw_content[body_start:].strip()
                                
                                # 如果正文包含 HTML，提取 HTML 部分
                                html_match = re.search(r'<html[^>]*>.*?</html>', raw_content, re.DOTALL | re.IGNORECASE)
                                if html_match:
                                    raw_content = html_match.group(0)
                                
                                # 移除 HTML 标签
                                mail_text = re.sub(r'<[^>]+>', '', raw_content)
                                mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                            else:
                                # 如果找不到空行分隔，尝试移除所有邮件头部行
                                # 移除邮件头部常见的模式（避免误匹配）
                                lines = raw_content.split('\n')
                                body_lines = []
                                in_body = False
                                for line in lines:
                                    # 如果遇到空行，之后就是正文
                                    if not line.strip():
                                        in_body = True
                                        continue
                                    # 如果遇到 Content-Type，之后可能是正文
                                    if 'Content-Type:' in line and 'text/' in line:
                                        in_body = True
                                        continue
                                    # 跳过邮件头部行
                                    if not in_body and (line.startswith('Received:') or 
                                                       line.startswith('ARC-') or
                                                       line.startswith('Return-Path:') or
                                                       line.startswith('Delivered-To:') or
                                                       line.startswith('X-') or
                                                       line.startswith('MIME-Version:') or
                                                       line.startswith('Content-Type:') or
                                                       line.startswith('Content-Transfer-Encoding:') or
                                                       line.startswith('Date:') or
                                                       line.startswith('From:') or
                                                       line.startswith('To:') or
                                                       line.startswith('Subject:') or
                                                       line.startswith('Message-ID:')):
                                        continue
                                    body_lines.append(line)
                                
                                raw_content = '\n'.join(body_lines)
                                # 移除 HTML 标签
                                mail_text = re.sub(r'<[^>]+>', '', raw_content)
                                mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                
                # 解码 Quoted-Printable 编码（如果存在）
                if mail_text and '=' in mail_text:
                    try:
                        # 先移除 Quoted-Printable 的换行标记（`=\r\n`, `=\n`, `=\r`）
                        mail_text_cleaned = mail_text.replace('=\r\n', '').replace('=\n', '').replace('=\r', '')
                        # 如果包含 Quoted-Printable 编码模式（如 `=E9=AA=8C`），尝试解码
                        if re.search(r'=[0-9A-F]{2}', mail_text_cleaned):
                            mail_text = quopri.decodestring(mail_text_cleaned.encode('latin-1')).decode('utf-8', errors='ignore')
                        else:
                            # 即使不是标准 Quoted-Printable，也移除 `=` 符号（可能是解码后的残留）
                            mail_text = mail_text_cleaned
                    except Exception as e:
                        log_print(f"[临时邮箱 API] ⚠ Quoted-Printable 解码失败: {e}", _level="WARNING")
                        # 解码失败时，至少移除 `=` 符号
                        mail_text = mail_text.replace('=', ' ')
                
                # 再次移除所有残留的 `=` 符号（确保完全清理）
                if mail_text and '=' in mail_text:
                    import re
                    # 移除所有单独的 `=` 符号及其后的空格
                    mail_text = re.sub(r'=\s*', '', mail_text)  # `=` 及其后的空格/换行全部移除
                
                # 解码 URL 编码（如果存在）
                if mail_text and '%' in mail_text:
                    try:
                        mail_text = unquote(mail_text)
                    except Exception as e:
                        log_print(f"[临时邮箱 API] ⚠ URL 解码失败: {e}", _level="WARNING")
                
                    # 规范化文本：合并多个空格，处理换行，移除残留的 `=` 符号
                    # 注意：在移除 `=` 符号时，要保护关键短语，避免"验证码"被截断
                    if mail_text:
                        import re
                        # 先保护关键短语，避免在处理 `=` 时被截断
                        # 将"验证码"相关的短语临时替换为占位符
                        protected_phrases = {
                            'VERIFICATION_CODE_PLACEHOLDER_1': '一次性验证码为',
                            'VERIFICATION_CODE_PLACEHOLDER_2': '验证码为',
                            'VERIFICATION_CODE_PLACEHOLDER_3': '验证码是',
                            'VERIFICATION_CODE_PLACEHOLDER_4': 'verification code is',
                            'VERIFICATION_CODE_PLACEHOLDER_5': 'one-time verification code is',
                        }
                        
                        # 保护关键短语
                        for placeholder, phrase in protected_phrases.items():
                            if phrase in mail_text:
                                mail_text = mail_text.replace(phrase, placeholder)
                        
                        # 移除所有单独的 `=` 符号（Quoted-Printable 的换行标记残留）
                        # 处理各种 `=` 符号的情况：
                        # - `一次性验证码= 为：` -> `一次性验证码为：`
                        # - `= ` -> 直接移除（不替换为空格）
                        # - ` = ` -> 空格
                        mail_text = re.sub(r'=\s+', '', mail_text)  # `= ` 或 `=\n` 等直接移除（不保留空格）
                        mail_text = re.sub(r'\s+=\s+', ' ', mail_text)  # ` = ` 替换为空格
                        mail_text = re.sub(r'=\s*$', '', mail_text, flags=re.MULTILINE)  # 行尾的 `=`
                        mail_text = re.sub(r'^\s*=\s*', '', mail_text, flags=re.MULTILINE)  # 行首的 `=`
                        
                        # 恢复关键短语
                        for placeholder, phrase in protected_phrases.items():
                            mail_text = mail_text.replace(placeholder, phrase)
                        
                        # 合并多个空格（但保留换行，因为验证码可能在单独一行）
                        # 先保留换行，只合并连续的空格
                        mail_text = re.sub(r'[ \t]+', ' ', mail_text)  # 只合并空格和制表符，保留换行
                        # 然后合并多个连续的空行（超过2个换行符的合并为2个）
                        mail_text = re.sub(r'\n{3,}', '\n\n', mail_text)
                        mail_text = mail_text.strip()
                    
                    # 调试信息（已关闭）
                    # if not hasattr(self, '_text_preview_logged') and ('验证码' in mail_text or 'verification' in mail_text.lower()):
                    #     preview_after = mail_text[:200].replace('\n', ' ').replace('\r', ' ')
                    #     log_print(f"[临时邮箱 API] 文本规范化后预览: {preview_after}...")
                    #     self._text_preview_logged = True
                
                if not mail_text:
                    html_content = mail.get("html", "") or mail.get("content", "") or mail.get("body", "")
                    if html_content:
                        # 简单的 HTML 标签移除（如果需要更精确，可以使用 BeautifulSoup）
                        import re
                        mail_text = re.sub(r'<[^>]+>', '', html_content)
                        mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                
                # 如果仍然没有内容，尝试调用详情 API 获取邮件内容（作为最后的兜底）
                if not mail_text:
                    try:
                        detail_url = f"{self.worker_url}/api/mails/{mail_id}"
                        headers = {
                            "Authorization": f"Bearer {self.jwt_token}",
                            "Content-Type": "application/json"
                        }
                        detail_response = requests.get(detail_url, headers=headers, timeout=30)
                        if detail_response.status_code == 200:
                            detail_data = detail_response.json()
                            # 尝试从详情中获取内容
                            mail_text = detail_data.get("text", "")
                            if not mail_text:
                                raw_content = detail_data.get("raw", "")
                                if raw_content:
                                    import re
                                    mail_text = re.sub(r'<[^>]+>', '', raw_content)
                                    mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                            if not mail_text:
                                html_content = detail_data.get("html", "") or detail_data.get("content", "") or detail_data.get("body", "")
                                if html_content:
                                    import re
                                    mail_text = re.sub(r'<[^>]+>', '', html_content)
                                    mail_text = re.sub(r'\s+', ' ', mail_text).strip()
                    except Exception as e:
                        log_print(f"[临时邮箱 API] ⚠ 获取邮件详情失败 (ID {mail_id}): {e}", _level="WARNING")
                
                if not mail_text:
                    log_print(f"[临时邮箱 API] ⚠ 邮件 ID {mail_id} (来源: {mail_source}) 无文本内容，可用字段: {list(mail.keys())}", _level="WARNING")
                    # 在重试模式下，如果邮件没有文本内容，尝试打印更多调试信息
                    if retry_mode:
                        log_print(f"[临时邮箱 API] 调试信息 - 邮件对象: {str(mail)[:500]}", _level="INFO")
                    continue
                
                # 在重试模式下，如果邮件文本很短或看起来不完整，打印预览
                if retry_mode and len(mail_text) < 100:
                    log_print(f"[临时邮箱 API] 邮件 ID {mail_id} 文本内容预览（前200字符）: {mail_text[:200]}", _level="INFO")
                
                # 在重试模式下，记录邮件内容用于对比（仅第一次）
                if retry_mode and not hasattr(self, '_content_comparison_logged'):
                    log_print(f"[临时邮箱 API] 邮件内容对比 - API方式获取的邮件 ID {mail_id} 内容（前500字符）:\n{mail_text[:500]}", _level="INFO")
                    self._content_comparison_logged = True
                
                # 提取验证码
                code = extract_code_func(mail_text)
                
                if code:
                    # 计算实际等待时间
                    actual_wait_time = int(time.time() - start_time)
                    log_print(f"[临时邮箱 API] ✓ 从邮件 ID {mail_id} 中提取到验证码: {code} (等待时间: {actual_wait_time} 秒)")
                    # 只有成功提取验证码后，才更新 last_max_id，避免重复处理
                    if mail_id > last_max_id:
                        last_max_id = mail_id
                        self.last_max_id = mail_id  # 同时更新实例变量
                    return code
                # else:
                #     # 只在失败时显示（已关闭，减少日志）
                #     log_print(f"[临时邮箱 API] ⚠ 邮件 ID {mail_id} (来源: {mail_source}) 未找到验证码")
                    # 如果提取失败，不更新 last_max_id，允许下次重试同一封邮件
            
            # 处理完所有新邮件后，更新 last_max_id 到已处理的最大 ID
            # 即使没有找到验证码，也更新 last_max_id，避免重复处理已检查过的邮件
            if 'processed_max_id' in locals() and processed_max_id > last_max_id:
                last_max_id = processed_max_id
                # 如果是第一次处理（before_process_max_id == 0），且处理失败，更新 last_max_id 避免重复处理
                if before_process_max_id == 0 and last_max_id > 0:
                    log_print(f"[临时邮箱 API] 第一次处理邮件（ID: {last_max_id}）失败，更新 last_max_id 为 {last_max_id}，下次将等待新邮件（ID > {last_max_id}）", _level="INFO")
                if retry_mode:
                    self.last_max_id = last_max_id  # 重试模式下立即更新实例变量
            
            # 处理完所有新邮件后，继续等待
            if not retry_mode:
                time.sleep(5)  # 改为 5 秒检查一次
            else:
                break
        
        # 如果超时仍未找到，打印总结信息
        # 更新实例变量，以便下次重试时记住已处理的最大 ID
        if last_max_id > self.last_max_id:
            self.last_max_id = last_max_id
        
        # 计算实际检查的 ID 范围（包括已处理但未找到验证码的邮件）
        actual_checked_max = last_max_id
        if 'processed_max_id' in locals() and processed_max_id > last_max_id:
            actual_checked_max = processed_max_id
        
        log_print(f"[临时邮箱 API] ✗ 未找到验证码（尝试 {attempts} 次，已检查邮件 ID 范围: 0-{actual_checked_max}）", _level="WARNING")
        if last_mail_count > 0:
            log_print(f"[临时邮箱 API] ⚠ 已获取到 {last_mail_count} 封邮件，但均未包含有效验证码", _level="WARNING")
            log_print(f"[临时邮箱 API] 提示: 请检查验证码邮件是否已发送，或验证码提取规则是否需要调整", _level="WARNING")
        return None


def get_verification_code_from_api(
    tempmail_url: str,
    timeout: int = 120,
    retry_mode: bool = False,
    extract_code_func=None,
    worker_url: Optional[str] = None
) -> Optional[str]:
    """通过 API 获取验证码（便捷函数）
    
    Args:
        tempmail_url: 临时邮箱 URL（包含 JWT token）
        timeout: 超时时间（秒）
        retry_mode: 是否为重试模式
        extract_code_func: 验证码提取函数
        worker_url: Worker API 地址（可选，如果不提供则从 tempmail_url 提取）
    
    Returns:
        验证码字符串，如果未找到则返回 None
    """
    try:
        client = TempMailAPIClient(tempmail_url, worker_url)
        return client.get_verification_code(timeout, retry_mode, extract_code_func)
    except Exception as e:
        log_print(f"[临时邮箱 API] 初始化客户端失败: {e}", _level="ERROR")
        return None
