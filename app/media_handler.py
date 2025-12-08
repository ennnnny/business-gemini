"""媒体处理模块 - 图片/视频缓存、下载、清理"""

import os
import re
import uuid
import mimetypes
import shutil
import base64
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

from .config import IMAGE_CACHE_DIR, VIDEO_CACHE_DIR, IMAGE_CACHE_HOURS, VIDEO_CACHE_HOURS, MEDIA_STREAM_CHUNK_SIZE

# MIME 类型到扩展名映射
MIME_EXTENSION_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def get_extension_for_mime(mime_type: Optional[str], default: str = ".bin") -> str:
    """根据 MIME 类型获取文件扩展名"""
    base = (mime_type or "").split(";")[0].strip().lower()
    if base in MIME_EXTENSION_MAP:
        return MIME_EXTENSION_MAP[base]
    guessed = mimetypes.guess_extension(base)
    return guessed or default


def sanitize_filename(name: Optional[str], ext: str) -> str:
    """清理文件名，只保留安全字符"""
    raw = name or f"media_{uuid.uuid4().hex[:8]}"
    safe = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in raw)
    if not safe.lower().endswith(ext.lower()):
        safe += ext
    return safe


def ensure_unique_filename(directory: Path, filename: str) -> str:
    """确保文件名唯一，如果存在则添加数字后缀"""
    candidate = filename
    stem, ext = os.path.splitext(candidate)
    counter = 1
    while (directory / candidate).exists():
        candidate = f"{stem}_{counter}{ext}"
        counter += 1
    return candidate


def save_image_to_cache(image_data: bytes, mime_type: str = "image/png", filename: Optional[str] = None) -> str:
    """保存图片到缓存目录，返回文件名"""
    IMAGE_CACHE_DIR.mkdir(exist_ok=True)
    
    # 确定文件扩展名
    ext = get_extension_for_mime(mime_type or "image/png", ".png")
    
    if filename:
        # 确保有正确的扩展名
        if not filename.lower().endswith(ext.lower()):
            filename = f"{filename}{ext}"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"gemini_{timestamp}_{uuid.uuid4().hex[:8]}{ext}"
    
    filepath = IMAGE_CACHE_DIR / filename
    with open(filepath, "wb") as f:
        f.write(image_data)
    
    return filename


def save_video_to_cache(video_data: bytes, mime_type: str = "video/mp4", filename: Optional[str] = None) -> str:
    """保存视频到缓存目录"""
    VIDEO_CACHE_DIR.mkdir(exist_ok=True)
    ext = get_extension_for_mime(mime_type or "video/mp4", ".mp4")
    if filename:
        if not filename.lower().endswith(ext.lower()):
            filename = f"{filename}{ext}"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"gemini_video_{timestamp}_{uuid.uuid4().hex[:8]}{ext}"
    filename = ensure_unique_filename(VIDEO_CACHE_DIR, filename)
    filepath = VIDEO_CACHE_DIR / filename
    with open(filepath, "wb") as f:
        f.write(video_data)
    return filename


def _cleanup_expired_cache(directory: Path, max_age_hours: int, label: str):
    """清理过期的缓存文件"""
    if not directory.exists():
        return
    
    now = datetime.now().timestamp()
    max_age_seconds = max_age_hours * 3600
    removed = 0
    
    for filepath in directory.iterdir():
        if filepath.is_file():
            try:
                file_age = now - filepath.stat().st_mtime
                if file_age > max_age_seconds:
                    filepath.unlink()
                    removed += 1
            except Exception:
                pass
    
    if removed > 0:
        print(f"[清理] 已删除 {removed} 个过期{label}缓存文件")


def cleanup_expired_images():
    """清理过期的图片缓存"""
    _cleanup_expired_cache(IMAGE_CACHE_DIR, IMAGE_CACHE_HOURS, "图片")


def cleanup_expired_videos():
    """清理过期的视频缓存"""
    _cleanup_expired_cache(VIDEO_CACHE_DIR, VIDEO_CACHE_HOURS, "视频")


def download_file_streaming(jwt: str, session_name: str, file_id: str, mime_type: str,
                            suggested_name: Optional[str] = None, proxy: Optional[str] = None) -> str:
    """以流式方式下载文件并保存到对应缓存目录，返回文件名"""
    import requests
    from .session_manager import get_headers
    
    target_dir = VIDEO_CACHE_DIR if (mime_type or "").startswith("video/") else IMAGE_CACHE_DIR
    target_dir.mkdir(exist_ok=True)
    
    ext = get_extension_for_mime(mime_type, ".bin")
    filename = sanitize_filename(suggested_name, ext)
    filename = ensure_unique_filename(target_dir, filename)
    filepath = target_dir / filename
    
    url = build_download_url(session_name, file_id)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    with requests.get(
        url,
        headers=get_headers(jwt),
        proxies=proxies,
        verify=False,
        timeout=600,
        stream=True,
        allow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(MEDIA_STREAM_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
    
    return filename


def build_download_url(session_name: str, file_id: str) -> str:
    """构造正确的下载URL"""
    return f"https://biz-discoveryengine.googleapis.com/v1alpha/{session_name}:downloadFile?fileId={file_id}&alt=media"


def download_file_with_jwt(jwt: str, session_name: str, file_id: str, proxy: Optional[str] = None) -> bytes:
    """使用JWT认证下载文件"""
    import requests
    import base64
    from .session_manager import get_headers
    
    url = build_download_url(session_name, file_id)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    resp = requests.get(
        url,
        headers=get_headers(jwt),
        proxies=proxies,
        verify=False,
        timeout=120,
        allow_redirects=True
    )
    
    resp.raise_for_status()
    content = resp.content

    print(f"[下载] {url} - {len(content)} bytes - status: {resp.status_code}")
    
    # 检测是否为base64编码的内容
    try:
        text_content = content.decode("utf-8", errors="ignore").strip()
        if text_content.startswith("iVBORw0KGgo") or text_content.startswith("/9j/"):
            # 是base64编码，需要解码
            return base64.b64decode(text_content)
    except Exception:
        pass
    
    return content


def parse_base64_data_url(data_url: str) -> Optional[Dict]:
    """解析 base64 data URL，返回 {type, mime_type, data} 或 None"""
    if not data_url or not data_url.startswith("data:"):
        return None
    
    # base64格式: data:image/png;base64,xxxxx
    match = re.match(r"data:([^;]+);base64,(.+)", data_url)
    if match:
        return {
            "type": "base64",
            "mime_type": match.group(1),
            "data": match.group(2)
        }
    return None


def extract_images_from_files_array(files: List[Dict]) -> List[Dict]:
    """从 files 数组中提取图片（支持内联 base64 格式）
    
    支持格式:
    {
        "data": "data:image/png;base64,xxxxx",
        "type": "image",
        "detail": "high"  # 可选
    }
    
    返回: 图片列表 [{type: 'base64', mime_type: ..., data: ...}]
    """
    images = []
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        
        file_type = file_item.get("type", "")
        
        # 只处理图片类型
        if file_type != "image":
            continue
        
        data = file_item.get("data", "")
        if data:
            parsed = parse_base64_data_url(data)
            if parsed:
                images.append(parsed)
    
    return images


def extract_images_from_openai_content(content: Any) -> Tuple[str, List[Dict]]:
    """从OpenAI格式的content中提取文本和图片
    
    返回: (文本内容, 图片列表[{type: 'base64'|'url', data: ...}])
    """
    if isinstance(content, str):
        return content, []
    
    if not isinstance(content, list):
        return str(content), []
    
    text_parts = []
    images = []
    
    for item in content:
        if not isinstance(item, dict):
            continue
        
        item_type = item.get("type", "")
        
        if item_type == "text":
            text_parts.append(item.get("text", ""))
        
        elif item_type == "image_url":
            image_url_obj = item.get("image_url", {})
            if isinstance(image_url_obj, str):
                url = image_url_obj
            else:
                url = image_url_obj.get("url", "")
            
            parsed = parse_base64_data_url(url)
            if parsed:
                images.append(parsed)
            elif url:
                # 普通URL
                images.append({
                    "type": "url",
                    "url": url
                })
        
        # 支持直接的 image 类型（带 data 字段）
        elif item_type == "image" and item.get("data"):
            parsed = parse_base64_data_url(item.get("data"))
            if parsed:
                images.append(parsed)
    
    return "\n".join(text_parts), images


def download_image_from_url(url: str, proxy: Optional[str] = None) -> Tuple[bytes, str]:
    """从URL下载图片，返回(图片数据, mime_type)"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(url, proxies=proxies, verify=False, timeout=60)
    resp.raise_for_status()
    
    content_type = resp.headers.get("Content-Type", "image/png")
    # 提取主mime类型
    mime_type = content_type.split(";")[0].strip()
    
    return resp.content, mime_type


def get_session_file_metadata(jwt: str, session_name: str, team_id: str, proxy: Optional[str] = None) -> Dict:
    """获取会话中的文件元数据（AI生成的图片）"""
    from .config import LIST_FILE_METADATA_URL
    from .session_manager import get_headers
    
    body = {
        "configId": team_id,
        "additionalParams": {"token": "-"},
        "listSessionFileMetadataRequest": {
            "name": session_name,
            "filter": "file_origin_type = AI_GENERATED"
        }
    }
    
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.post(
        LIST_FILE_METADATA_URL,
        headers=get_headers(jwt),
        json=body,
        proxies=proxies,
        verify=False,
        timeout=30
    )
    
    if resp.status_code != 200:
        from .logger import print
        print(f"[图片] 获取文件元数据失败: {resp.status_code}")
        return {}
    
    data = resp.json()
    # 返回 fileId -> metadata 的映射
    result = {}
    file_metadata_list = data.get("listSessionFileMetadataResponse", {}).get("fileMetadata", [])
    for meta in file_metadata_list:
        file_id = meta.get("fileId")
        if file_id:
            result[file_id] = meta
    return result

