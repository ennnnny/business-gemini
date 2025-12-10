"""聊天处理模块
包含流式聊天、响应解析、OpenAI格式转换等功能
"""

import json
import base64
import uuid
import requests
from typing import List, Optional, Dict, Any, Generator

from app.models import ChatResponse, ChatImage
from app.config import STREAM_ASSIST_URL, IMAGE_CACHE_DIR, VIDEO_CACHE_DIR
from app.session_manager import get_headers
from app.utils import raise_for_account_response
from app.exceptions import AccountRequestError, AccountResourceExhaustedError
from app.media_handler import (
    get_extension_for_mime,
    save_image_to_cache,
    save_video_to_cache,
    get_session_file_metadata,
    build_download_url,
    download_file_with_jwt,
    download_file_streaming
)
from app.cfbed_upload import upload_base64_to_cfbed, upload_file_streaming_to_cfbed
from .logger import print

# account_manager 需要通过参数传递或导入
# 为了避免循环引用，这里先不导入，通过参数传递


# ---------- JSON 流式解析器 (参考 j.py) ----------
class JSONStreamParser:
    """
    处理 Google 返回的非标准/分块 JSON 流。
    能够处理被截断的 JSON 对象，实现真正的流式解析。
    """
    def __init__(self):
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def decode(self, chunk: str) -> List[dict]:
        """解析分块 JSON 数据，返回完整的 JSON 对象列表"""
        self.buffer += chunk
        results = []
        while True:
            self.buffer = self.buffer.lstrip()  # 去除头部空白
            # 尝试跳过数组开始的 [ 或分隔符 ,
            if self.buffer.startswith("[") or self.buffer.startswith(","):
                self.buffer = self.buffer[1:]
                continue
            
            if not self.buffer:
                break

            try:
                # 尝试解析一个完整的 JSON 对象
                obj, idx = self.decoder.raw_decode(self.buffer)
                results.append(obj)
                self.buffer = self.buffer[idx:]
            except json.JSONDecodeError:
                # 缓冲区数据不完整，等待下一个 chunk
                break
        return results


def get_tools_spec_for_model(model_id: Optional[str]) -> Dict[str, Any]:
    """根据模型ID返回相应的工具配置
    
    Args:
        model_id: 模型ID（如 gemini-video, gemini-image 等）
    
    Returns:
        工具配置字典
    """
    # gemini-image: 只启用图片生成
    if model_id == "gemini-image" or model_id == "gemini-3-pro-preview":
        return {
            "imageGenerationSpec": {}
        }
    
    # gemini-video: 只启用视频生成
    if model_id == "gemini-video":
        return {
            "videoGenerationSpec": {}
        }
    
    # 默认: 完整工具集（普通对话）
    return {
        "webGroundingSpec": {},
        "toolRegistry": "default_tool_registry",
        "imageGenerationSpec": {},
        "videoGenerationSpec": {}
    }


def stream_chat_realtime_generator(jwt: str, sess_name: str, message: str, 
                                   proxy: str, team_id: str, file_ids: List[str] = None, 
                                   model_id: Optional[str] = None, account_manager=None, 
                                   account_idx: Optional[int] = None, quota_type: Optional[str] = None,
                                   chat_id: str = None, created: int = None, model_name: str = None,
                                   host_url: str = None) -> Generator[str, None, None]:
    """真正的流式处理：边接收边解析边转发
    
    这是一个生成器函数，实时解析 Gemini API 的流式响应并立即转发给客户端。
    同时收集图片/视频信息（因为需要下载，不能实时转发）。
    
    Args:
        jwt: JWT token
        sess_name: Session名称
        message: 消息内容
        proxy: 代理设置
        team_id: Team ID
        file_ids: 文件ID列表
        model_id: 模型ID（可选）
        account_manager: AccountManager实例
        account_idx: 账号索引
        quota_type: 配额类型
        chat_id: OpenAI 格式的聊天ID
        created: 创建时间戳
        model_name: 模型名称
    
    Yields:
        str: OpenAI 格式的 SSE 数据块（"data: {...}\n\n"）
    
    Returns:
        ChatResponse: 包含图片/视频等媒体信息的响应对象
    """
    query_parts = [{"text": message}]
    request_file_ids = file_ids if file_ids else []
    
    # 根据模型ID获取工具配置
    tools_spec = get_tools_spec_for_model(model_id)
    
    body = {
        "configId": team_id,
        "additionalParams": {"token": "-"},
        "streamAssistRequest": {
            "session": sess_name,
            "query": {"parts": query_parts},
            "filter": "",
            "fileIds": request_file_ids,
            "answerGenerationMode": "NORMAL",
            "toolsSpec": tools_spec,
            "languageCode": "zh-CN",
            "userMetadata": {"timeZone": "Etc/GMT-8"},
            "assistSkippingMode": "REQUEST_ASSIST"
        }
    }
    
    # 如果指定了模型ID，在 streamAssistRequest 中添加 assistGenerationConfig
    if model_id and model_id not in ["gemini-video", "gemini-image", "gemini-3-pro-preview"]:
        body["streamAssistRequest"]["assistGenerationConfig"] = {
            "modelId": model_id
        }
    
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    # 初始化响应对象（用于收集图片/视频）
    result = ChatResponse()
    file_ids_list = []
    current_session = None
    parser = JSONStreamParser()
    
    # 先发送 role 标记（降低首字延迟）
    if chat_id and created is not None and model_name:
        role_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
    
    try:
        resp = requests.post(
            STREAM_ASSIST_URL,
            headers=get_headers(jwt),
            json=body,
            proxies=proxies,
            verify=False,
            timeout=300,
            stream=True
        )
    except requests.RequestException as e:
        raise AccountRequestError(f"聊天请求失败: {e}") from e
    
    if resp.status_code != 200:
        raise_for_account_response(resp, "聊天请求", account_idx, quota_type)
    
    # ✅ 真正的流式处理：逐块读取并实时解析
    buffer = ""
    for line in resp.iter_lines():
        if not line:
            continue
        
        chunk_text = line.decode('utf-8')
        buffer += chunk_text + "\n"
        
        # 使用 JSONStreamParser 解析分块 JSON
        json_objects = parser.decode(chunk_text)
        
        for data in json_objects:
            sar = data.get("streamAssistResponse")
            if not sar:
                continue
            
            # 获取session信息
            session_info = sar.get("sessionInfo", {})
            if session_info.get("session"):
                current_session = session_info["session"]
            
            # 检查顶层的generatedImages（图片需要下载，不能实时转发）
            for gen_img in sar.get("generatedImages", []):
                parse_generated_media(gen_img, result, proxy, account_manager)
            
            answer = sar.get("answer") or {}
            
            # 检查answer级别的generatedImages
            for gen_img in answer.get("generatedImages", []):
                parse_generated_media(gen_img, result, proxy, account_manager)
            
            # ✅ 实时处理文本回复（过滤思考输出）
            for reply in answer.get("replies", []):
                # 检查reply级别的generatedImages
                for gen_img in reply.get("generatedImages", []):
                    parse_generated_media(gen_img, result, proxy, account_manager)
                
                gc = reply.get("groundedContent", {})
                content = gc.get("content", {})
                text = content.get("text", "")
                # ✅ 过滤思考输出
                thought = content.get("thought", False)
                
                # 检查file字段（图片生成的关键）
                file_info = content.get("file")
                if file_info and file_info.get("fileId"):
                    file_ids_list.append({
                        "fileId": file_info["fileId"],
                        "mimeType": file_info.get("mimeType", "image/png"),
                        "fileName": file_info.get("name")
                    })
                
                # 解析图片数据（需要下载，不能实时转发）
                parse_image_from_content(content, result, proxy, account_manager)
                parse_image_from_content(gc, result, proxy, account_manager)
                
                # 检查attachments
                for att in reply.get("attachments", []) + gc.get("attachments", []) + content.get("attachments", []):
                    parse_attachment(att, result, proxy, account_manager)
                
                # ✅ 只处理非思考输出，实时转发文本
                if text and not thought:
                    # 过滤掉 "Image generated by Nano Banana Pro." 文本
                    filtered_text = text
                    if "Image generated by Nano Banana Pro" in text:
                        lines = text.split('\n')
                        filtered_lines = [line for line in lines if "Image generated by Nano Banana Pro" not in line.strip()]
                        filtered_text = '\n'.join(filtered_lines).strip()
                    
                    if filtered_text and chat_id and created is not None and model_name:
                        # 实时转发文本内容
                        text_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": filtered_text}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"
    
    print(f"[DEBUG][stream_chat_with_images] file_ids_list: {json.dumps(file_ids_list, ensure_ascii=False, indent=2)}")
    # 处理通过fileId引用的图片/视频（需要下载，在流式结束后处理）
    if file_ids_list and current_session:
        try:
            upload_endpoint = account_manager.config.get("upload_endpoint", "").strip() if account_manager else ""
            upload_api_token = account_manager.config.get("upload_api_token", "").strip() if account_manager else ""
            use_cfbed = bool(upload_endpoint and upload_api_token)
            
            file_metadata = get_session_file_metadata(jwt, current_session, team_id, proxy)
            for finfo in file_ids_list:
                fid = finfo["fileId"]
                mime = finfo["mimeType"]
                fname = finfo.get("fileName")
                meta = file_metadata.get(fid)
                
                if meta:
                    fname = fname or meta.get("name")
                    mime = meta.get("mimeType", mime)
                    session_path = meta.get("session") or current_session
                else:
                    session_path = current_session
                
                try:
                    is_video = mime.startswith("video/")
                    
                    if use_cfbed:
                        url = build_download_url(session_path, fid)
                        download_resp = requests.get(
                            url,
                            headers=get_headers(jwt),
                            proxies={"http": proxy, "https": proxy} if proxy else None,
                            verify=False,
                            timeout=600,
                            stream=True,
                            allow_redirects=True
                        )
                        download_resp.raise_for_status()
                        
                        upload_result = upload_file_streaming_to_cfbed(
                            file_stream=download_resp,
                            filename=fname or (f"media_{uuid.uuid4().hex[:8]}{get_extension_for_mime(mime)}"),
                            mime_type=mime,
                            endpoint=upload_endpoint,
                            api_token=upload_api_token,
                            proxy=proxy
                        )
                        
                        image_base_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
                        if not image_base_url:
                            image_base_url = upload_endpoint.rstrip("/").replace("/upload", "")
                        
                        if not image_base_url.endswith("/"):
                            image_base_url += "/"
                        
                        full_url = f"{image_base_url.rstrip('/')}{upload_result['src']}"
                        
                        img = ChatImage(
                            file_id=fid,
                            file_name=upload_result["src"].split("/")[-1],
                            mime_type=mime,
                            url=full_url,
                            media_type="video" if is_video else "image"
                        )
                        result.images.append(img)
                        
                        # ✅ 实时发送图片 URL（作为字符串，兼容流式 API）
                        if chat_id and created is not None and model_name:
                            image_url_text = f"\n{full_url}\n"
                            image_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": image_url_text},
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(image_chunk, ensure_ascii=False)}\n\n"
                    else:
                        # 使用本地缓存
                        if is_video:
                            filename = download_file_streaming(jwt, session_path, fid, mime, fname, proxy, account_manager)
                            if filename:
                                video = ChatImage(
                                    file_id=fid,
                                    file_name=filename,
                                    mime_type=mime,
                                    media_type="video"
                                )
                                result.images.append(video)
                                
                                # ✅ 实时发送视频 URL
                                if chat_id and created is not None and model_name:
                                    # 构建视频 URL
                                    base_url = get_image_base_url(host_url, account_manager, None)
                                    video_url = f"{base_url}video/{filename}"
                                    video_url_text = f"\n{video_url}\n"
                                    video_chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": video_url_text},
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(video_chunk, ensure_ascii=False)}\n\n"
                        else:
                            file_data = download_file_with_jwt(jwt, session_path, fid, proxy)
                            if file_data:
                                filename = save_image_to_cache(file_data, mime, fname)
                                if filename:
                                    img = ChatImage(
                                        file_id=fid,
                                        file_name=filename,
                                        mime_type=mime,
                                        media_type="image"
                                    )
                                    result.images.append(img)
                                    
                                    # ✅ 实时发送图片 URL
                                    if chat_id and created is not None and model_name:
                                        # 构建图片 URL
                                        base_url = get_image_base_url(host_url, account_manager, None)
                                        image_url = f"{base_url}image/{filename}"
                                        image_url_text = f"\n{image_url}\n"
                                        image_chunk = {
                                            "id": chat_id,
                                            "object": "chat.completion.chunk",
                                            "created": created,
                                            "model": model_name,
                                            "choices": [{
                                                "index": 0,
                                                "delta": {"content": image_url_text},
                                                "finish_reason": None
                                            }]
                                        }
                                        yield f"data: {json.dumps(image_chunk, ensure_ascii=False)}\n\n"
                except Exception as e:
                    print(f"[WARNING] 下载文件失败 {fid}: {e}")
        except Exception as e:
            print(f"[WARNING] 处理文件列表失败: {e}")
    
    # 流式模式下文本已实时转发，不需要返回 ChatResponse
    # 图片/视频 URL 也已实时发送
    pass


def stream_chat_with_images(jwt: str, sess_name: str, message: str, 
                            proxy: str, team_id: str, file_ids: List[str] = None, 
                            model_id: Optional[str] = None, account_manager=None, account_idx: Optional[int] = None, quota_type: Optional[str] = None) -> ChatResponse:
    """发送消息并流式接收响应
    
    Args:
        jwt: JWT token
        sess_name: Session名称
        message: 消息内容
        proxy: 代理设置
        team_id: Team ID
        file_ids: 文件ID列表
        model_id: 模型ID（可选）
        account_manager: AccountManager实例（用于访问配置）
    """
    query_parts = [{"text": message}]
    request_file_ids = file_ids if file_ids else []
    
    # 根据模型ID获取工具配置
    tools_spec = get_tools_spec_for_model(model_id)
    
    body = {
        "configId": team_id,
        "additionalParams": {"token": "-"},
        "streamAssistRequest": {
            "session": sess_name,
            "query": {"parts": query_parts},
            "filter": "",
            "fileIds": request_file_ids,
            "answerGenerationMode": "NORMAL",
            "toolsSpec": tools_spec,
            "languageCode": "zh-CN",
            "userMetadata": {"timeZone": "Etc/GMT-8"},
            "assistSkippingMode": "REQUEST_ASSIST"
        }
    }
    
    # 如果指定了模型ID，在 streamAssistRequest 中添加 assistGenerationConfig
    # 根据实际 API 请求，模型ID应该通过 assistGenerationConfig.modelId 传递
    # 注意：对于 gemini-video 和 gemini-image，这些是虚拟模型ID，不需要传递给 API
    if model_id and model_id not in ["gemini-video", "gemini-image", "gemini-3-pro-preview"]:
        body["streamAssistRequest"]["assistGenerationConfig"] = {
            "modelId": model_id
        }
        # 调试日志已关闭
        # print(f"[DEBUG][stream_chat_with_images] 使用模型ID: {model_id}")
    elif model_id in ["gemini-video", "gemini-image"]:
        # 调试日志已关闭
        # print(f"[DEBUG][stream_chat_with_images] 使用专用模型: {model_id}（仅启用对应工具）")
        pass

    # 调试日志已关闭
    # body_str = json.dumps(body, ensure_ascii=False, indent=2)
    # print(f"[DEBUG][stream_chat_with_images] 请求体大小: {len(body_str)} 字符")
    
    # 调试日志已关闭
    # debug_body = {
    #     "configId": body.get("configId"),
    #     "additionalParams": body.get("additionalParams"),
    #     "streamAssistRequest": {
    #         "session": body.get("streamAssistRequest", {}).get("session"),
    #         "query": {
    #             "parts": [{"text": f"<消息长度: {len(message)} 字符>"}]
    #         },
    #         "filter": body.get("streamAssistRequest", {}).get("filter"),
    #         "fileIds": body.get("streamAssistRequest", {}).get("fileIds", []),
    #         "answerGenerationMode": body.get("streamAssistRequest", {}).get("answerGenerationMode"),
    #         "toolsSpec": body.get("streamAssistRequest", {}).get("toolsSpec"),
    #         "languageCode": body.get("streamAssistRequest", {}).get("languageCode"),
    #         "userMetadata": body.get("streamAssistRequest", {}).get("userMetadata"),
    #         "assistSkippingMode": body.get("streamAssistRequest", {}).get("assistSkippingMode"),
    #         "assistGenerationConfig": body.get("streamAssistRequest", {}).get("assistGenerationConfig")
    #     }
    # }
    # print(f"[DEBUG][stream_chat_with_images] 请求体结构: {json.dumps(debug_body, ensure_ascii=False, indent=2)}")
    
    # 调试日志已关闭
    # if len(message) <= 200:
    #     print(f"[DEBUG][stream_chat_with_images] 消息内容: {message}")
    # else:
    #     print(f"[DEBUG][stream_chat_with_images] 消息内容(前100字符): {message[:100]}...")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        # 增加超时时间，避免长时间请求导致 504 错误
        # 对于流式响应，需要更长的超时时间
        resp = requests.post(
            STREAM_ASSIST_URL,
            headers=get_headers(jwt),
            json=body,
            proxies=proxies,
            verify=False,
            timeout=300,  # 增加到 5 分钟，避免超时
            stream=True
        )
    except requests.RequestException as e:
        raise AccountRequestError(f"聊天请求失败: {e}") from e

    if resp.status_code != 200:
        # 对于 500 错误，打印更详细的调试信息
        if resp.status_code == 500:
            print(f"[ERROR][stream_chat_with_images] 500 内部错误 - 响应内容: {resp.text[:1000]}")
            print(f"[ERROR][stream_chat_with_images] 请求URL: {STREAM_ASSIST_URL}")
            print(f"[ERROR][stream_chat_with_images] Session: {sess_name}")
            print(f"[ERROR][stream_chat_with_images] Team ID: {team_id}")
            print(f"[ERROR][stream_chat_with_images] Model ID: {model_id}")
        raise_for_account_response(resp, "聊天请求", account_idx, quota_type)

    # ⚠️ 注意：当前实现不是真正的流式
    # 这里是先收集完整响应，然后才解析，最后在 routes.py 中再分块发送
    # 要实现真正的流式（边接收边解析边转发），需要参考 j.py 的实现方式
    # 使用 JSONStreamParser 实时解析分块 JSON，并立即转发给客户端
    # 收集完整响应
    full_response = ""
    for line in resp.iter_lines():
        if line:
            full_response += line.decode('utf-8') + "\n"

    # 解析响应
    result = ChatResponse()
    texts = []
    file_ids_list = []  # 收集需要下载的文件 {fileId, mimeType}
    current_session = None
    
    try:
        data_list = json.loads(full_response)
        for data in data_list:
            sar = data.get("streamAssistResponse")
            if not sar:
                continue
            
            # 获取session信息
            session_info = sar.get("sessionInfo", {})
            if session_info.get("session"):
                current_session = session_info["session"]
            
            # 检查顶层的generatedImages
            for gen_img in sar.get("generatedImages", []):
                parse_generated_media(gen_img, result, proxy, account_manager)
            
            answer = sar.get("answer") or {}
            
            # 检查answer级别的generatedImages
            for gen_img in answer.get("generatedImages", []):
                parse_generated_media(gen_img, result, proxy, account_manager)
            
            for reply in answer.get("replies", []):
                # 检查reply级别的generatedImages
                for gen_img in reply.get("generatedImages", []):
                    parse_generated_media(gen_img, result, proxy, account_manager)
                
                gc = reply.get("groundedContent", {})
                content = gc.get("content", {})
                text = content.get("text", "")
                # ✅ 思考输出过滤：thought 字段为 True 时表示这是思考过程，应该被过滤
                thought = content.get("thought", False)
                
                # 检查file字段（图片生成的关键）
                file_info = content.get("file")
                if file_info and file_info.get("fileId"):
                    file_ids_list.append({
                        "fileId": file_info["fileId"],
                        "mimeType": file_info.get("mimeType", "image/png"),
                        "fileName": file_info.get("name")
                    })
                
                # 解析图片数据
                parse_image_from_content(content, result, proxy, account_manager)
                parse_image_from_content(gc, result, proxy, account_manager)
                
                # 检查attachments
                for att in reply.get("attachments", []) + gc.get("attachments", []) + content.get("attachments", []):
                    parse_attachment(att, result, proxy, account_manager)
                
                # ✅ 只处理非思考输出：text 存在且 thought 为 False
                if text and not thought:
                    # 过滤掉 "Image generated by Nano Banana Pro." 文本
                    filtered_text = text
                    if "Image generated by Nano Banana Pro" in text:
                        # 如果整行只包含这个文本，则跳过
                        lines = text.split('\n')
                        filtered_lines = [line for line in lines if "Image generated by Nano Banana Pro" not in line.strip()]
                        filtered_text = '\n'.join(filtered_lines).strip()
                    
                    # 只有当过滤后的文本不为空时才添加
                    if filtered_text:
                        texts.append(filtered_text)
        
        print(f"[DEBUG][stream_chat_with_images] file_ids_list: {json.dumps(file_ids_list, ensure_ascii=False, indent=2)}")

        # 处理通过fileId引用的图片/视频
        if file_ids_list and current_session:
            try:
                # 检查是否配置了 cfbed
                upload_endpoint = account_manager.config.get("upload_endpoint", "").strip() if account_manager else ""
                upload_api_token = account_manager.config.get("upload_api_token", "").strip() if account_manager else ""
                use_cfbed = bool(upload_endpoint and upload_api_token)
                
                file_metadata = get_session_file_metadata(jwt, current_session, team_id, proxy)
                # print(f"[DEBUG][get_session_file_metadata] 文件元数据: {json.dumps(file_metadata, ensure_ascii=False, indent=2)}")
                for finfo in file_ids_list:
                    fid = finfo["fileId"]
                    mime = finfo["mimeType"]
                    fname = finfo.get("fileName")
                    meta = file_metadata.get(fid)
                    
                    if meta:
                        fname = fname or meta.get("name")
                        mime = meta.get("mimeType", mime)
                        session_path = meta.get("session") or current_session
                    else:
                        session_path = current_session
                    
                    try:
                        is_video = mime.startswith("video/")
                        
                        if use_cfbed:
                            # 使用 cfbed 上传
                            print(f"[cfbed] 开始上传 {'视频' if is_video else '图片'}: {fname or fid}")
                            
                            # 流式下载文件
                            url = build_download_url(session_path, fid)
                            download_resp = requests.get(
                                url,
                                headers=get_headers(jwt),
                                proxies={"http": proxy, "https": proxy} if proxy else None,
                                verify=False,
                                timeout=600,
                                stream=True,
                                allow_redirects=True
                            )
                            download_resp.raise_for_status()
                            
                            # 上传到 cfbed
                            upload_result = upload_file_streaming_to_cfbed(
                                file_stream=download_resp,
                                filename=fname or (f"media_{uuid.uuid4().hex[:8]}{get_extension_for_mime(mime)}"),
                                mime_type=mime,
                                endpoint=upload_endpoint,
                                api_token=upload_api_token,
                                proxy=proxy
                            )
                            
                            # 构建完整 URL
                            image_base_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
                            if not image_base_url:
                                # 从 upload_endpoint 推断（去掉 /upload）
                                image_base_url = upload_endpoint.rstrip("/").replace("/upload", "")
                            
                            if not image_base_url.endswith("/"):
                                image_base_url += "/"
                            
                            # upload_result["src"] 格式: "/file/abc123_image.jpg"
                            full_url = f"{image_base_url.rstrip('/')}{upload_result['src']}"
                            
                            img = ChatImage(
                                file_id=fid,
                                file_name=upload_result["src"].split("/")[-1],  # 只保留文件名
                                mime_type=mime,
                                url=full_url,  # 公网 URL
                                media_type="video" if is_video else "image"
                            )
                            result.images.append(img)
                            print(f"[cfbed] 上传成功: {full_url}")
                        else:
                            # 本地缓存
                            if is_video:
                                filename = download_file_streaming(jwt, session_path, fid, mime, fname, proxy)
                                local_path = VIDEO_CACHE_DIR / filename
                                media_type = "video"
                            else:
                                image_data = download_file_with_jwt(jwt, session_path, fid, proxy)
                                filename = save_image_to_cache(image_data, mime, fname)
                                local_path = IMAGE_CACHE_DIR / filename
                                media_type = "image"
                            img = ChatImage(
                                file_id=fid,
                                file_name=filename,
                                mime_type=mime,
                                local_path=str(local_path),
                                media_type=media_type
                            )
                            result.images.append(img)
                            print(f"[{'视频' if is_video else '图片'}] 已保存: {filename}")
                    except Exception as e:
                        print(f"[{'视频' if mime.startswith('video/') else '图片'}] 处理失败 (fileId={fid}): {e}")
                        import traceback
                        traceback.print_exc()
            except Exception as e:
                print(f"[文件处理] 获取文件元数据失败: {e}")
                import traceback
                traceback.print_exc()
                
    except json.JSONDecodeError:
        print(f"[Debug] 响应体不是有效的 JSON: {full_response[:2000]}")
        pass

    if not result.text and not result.images:
        # 打印最原始的响应，便于排查图片丢失
        print(
            f"[图片调试][raw] http_status={resp.status_code}, "
            f"headers={{'Content-Type': {resp.headers.get('Content-Type')}, 'Content-Length': {resp.headers.get('Content-Length')}}}, "
            f"body_len={len(full_response)}, body_preview={full_response}"
        )
        # 判断full_response是否包含`Resource has been exhausted`或者`RESOURCE_EXHAUSTED`或者`"code": 429`
        if "Resource has been exhausted" in full_response or "RESOURCE_EXHAUSTED" in full_response or "\"code\": 429" in full_response:
            print(f"❌[429]响应体包含配额错误信息")
            # raise AccountResourceExhaustedError(f"资源耗尽")


    result.text = "".join(texts)
    return result


def parse_generated_media(gen_img: Dict, result: ChatResponse, proxy: Optional[str] = None, account_manager=None):
    """解析generatedImages中的多媒体内容"""
    image_data = gen_img.get("image")
    if not image_data:
        return
    
    # 检查base64数据
    b64_data = image_data.get("bytesBase64Encoded")
    if b64_data:
        try:
            decoded = base64.b64decode(b64_data)
            mime_type = image_data.get("mimeType", "image/png")
            is_video = mime_type.startswith("video/")
            
            # 检查是否配置了 cfbed
            upload_endpoint = account_manager.config.get("upload_endpoint", "").strip() if account_manager else ""
            upload_api_token = account_manager.config.get("upload_api_token", "").strip() if account_manager else ""
            use_cfbed = bool(upload_endpoint and upload_api_token)
            
            if use_cfbed:
                # 上传到 cfbed
                print(f"[cfbed] 开始上传 {'视频' if is_video else '图片'} (base64)")
                filename = f"media_{uuid.uuid4().hex[:8]}{get_extension_for_mime(mime_type)}"
                
                upload_result = upload_base64_to_cfbed(
                    base64_data=b64_data,
                    filename=filename,
                    mime_type=mime_type,
                    endpoint=upload_endpoint,
                    api_token=upload_api_token,
                    proxy=proxy
                )
                
                # 构建完整 URL
                image_base_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
                if not image_base_url:
                    image_base_url = upload_endpoint.rstrip("/").replace("/upload", "")
                if not image_base_url.endswith("/"):
                    image_base_url += "/"
                
                full_url = f"{image_base_url.rstrip('/')}{upload_result['src']}"
                
                img = ChatImage(
                    base64_data=b64_data,
                    mime_type=mime_type,
                    file_name=upload_result["src"].split("/")[-1],
                    url=full_url,
                    media_type="video" if is_video else "image"
                )
                result.images.append(img)
                print(f"[cfbed] 上传成功: {full_url}")
            else:
                # 本地缓存
                if is_video:
                    filename = save_video_to_cache(decoded, mime_type)
                    media_type = "video"
                    local_path = VIDEO_CACHE_DIR / filename
                else:
                    filename = save_image_to_cache(decoded, mime_type)
                    media_type = "image"
                    local_path = IMAGE_CACHE_DIR / filename
                img = ChatImage(
                    base64_data=b64_data,
                    mime_type=mime_type,
                    file_name=filename,
                    local_path=str(local_path),
                    media_type=media_type
                )
                result.images.append(img)
                print(f"[{'视频' if is_video else '图片'}] 已保存: {filename}")
        except Exception as e:
            print(f"[{'视频' if image_data.get('mimeType', '').startswith('video/') else '图片'}] 解析base64失败: {e}")
            import traceback
            traceback.print_exc()


def parse_image_from_content(content: Dict, result: ChatResponse, proxy: Optional[str] = None, account_manager=None):
    """从content中解析图片"""
    # 检查inlineData
    inline_data = content.get("inlineData")
    if inline_data:
        b64_data = inline_data.get("data")
        if b64_data:
            try:
                decoded = base64.b64decode(b64_data)
                mime_type = inline_data.get("mimeType", "image/png")
                is_video = mime_type.startswith("video/")
                
                # 检查是否配置了 cfbed
                upload_endpoint = account_manager.config.get("upload_endpoint", "").strip() if account_manager else ""
                upload_api_token = account_manager.config.get("upload_api_token", "").strip() if account_manager else ""
                use_cfbed = bool(upload_endpoint and upload_api_token)
                
                if use_cfbed:
                    # 上传到 cfbed
                    print(f"[cfbed] 开始上传 {'视频' if is_video else '图片'} (inlineData)")
                    filename = f"media_{uuid.uuid4().hex[:8]}{get_extension_for_mime(mime_type)}"
                    
                    upload_result = upload_base64_to_cfbed(
                        base64_data=b64_data,
                        filename=filename,
                        mime_type=mime_type,
                        endpoint=upload_endpoint,
                        api_token=upload_api_token,
                        proxy=proxy
                    )
                    
                    # 构建完整 URL
                    image_base_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
                    if not image_base_url:
                        image_base_url = upload_endpoint.rstrip("/").replace("/upload", "")
                    if not image_base_url.endswith("/"):
                        image_base_url += "/"
                    
                    full_url = f"{image_base_url.rstrip('/')}{upload_result['src']}"
                    
                    img = ChatImage(
                        base64_data=b64_data,
                        mime_type=mime_type,
                        file_name=upload_result["src"].split("/")[-1],
                        url=full_url,
                        media_type="video" if is_video else "image"
                    )
                    result.images.append(img)
                    print(f"[cfbed] 上传成功: {full_url}")
                else:
                    # 本地缓存
                    if is_video:
                        filename = save_video_to_cache(decoded, mime_type)
                        media_type = "video"
                        local_path = VIDEO_CACHE_DIR / filename
                    else:
                        filename = save_image_to_cache(decoded, mime_type)
                        media_type = "image"
                        local_path = IMAGE_CACHE_DIR / filename
                    img = ChatImage(
                        base64_data=b64_data,
                        mime_type=mime_type,
                        file_name=filename,
                        local_path=str(local_path),
                        media_type=media_type
                    )
                    result.images.append(img)
                    print(f"[{'视频' if is_video else '图片'}] 已保存: {filename}")
            except Exception as e:
                print(f"[{'视频' if inline_data.get('mimeType', '').startswith('video/') else '图片'}] 解析inlineData失败: {e}")
                import traceback
                traceback.print_exc()


def parse_attachment(att: Dict, result: ChatResponse, proxy: Optional[str] = None, account_manager=None):
    """解析attachment中的图片/视频"""
    # 检查是否是图片或视频类型
    mime_type = att.get("mimeType", "")
    if not (mime_type.startswith("image/") or mime_type.startswith("video/")):
        return
    
    # 检查base64数据
    b64_data = att.get("data") or att.get("bytesBase64Encoded")
    if b64_data:
        try:
            decoded = base64.b64decode(b64_data)
            is_video = mime_type.startswith("video/")
            
            # 检查是否配置了 cfbed
            upload_endpoint = account_manager.config.get("upload_endpoint", "").strip() if account_manager else ""
            upload_api_token = account_manager.config.get("upload_api_token", "").strip() if account_manager else ""
            use_cfbed = bool(upload_endpoint and upload_api_token)
            
            if use_cfbed:
                # 上传到 cfbed
                print(f"[cfbed] 开始上传 {'视频' if is_video else '图片'} (attachment)")
                suggested_name = att.get("name")
                filename = suggested_name or f"media_{uuid.uuid4().hex[:8]}{get_extension_for_mime(mime_type)}"
                
                upload_result = upload_base64_to_cfbed(
                    base64_data=b64_data,
                    filename=filename,
                    mime_type=mime_type,
                    endpoint=upload_endpoint,
                    api_token=upload_api_token,
                    proxy=proxy
                )
                
                # 构建完整 URL
                image_base_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
                if not image_base_url:
                    image_base_url = upload_endpoint.rstrip("/").replace("/upload", "")
                if not image_base_url.endswith("/"):
                    image_base_url += "/"
                
                full_url = f"{image_base_url.rstrip('/')}{upload_result['src']}"
                
                img = ChatImage(
                    base64_data=b64_data,
                    mime_type=mime_type,
                    file_name=upload_result["src"].split("/")[-1],
                    url=full_url,
                    media_type="video" if is_video else "image"
                )
                result.images.append(img)
                print(f"[cfbed] 上传成功: {full_url}")
            else:
                # 本地缓存
                suggested_name = att.get("name")
                if is_video:
                    filename = save_video_to_cache(decoded, mime_type, suggested_name)
                    local_path = VIDEO_CACHE_DIR / filename
                    media_type = "video"
                else:
                    filename = save_image_to_cache(decoded, mime_type, suggested_name)
                    local_path = IMAGE_CACHE_DIR / filename
                    media_type = "image"
                img = ChatImage(
                    base64_data=b64_data,
                    mime_type=mime_type,
                    file_name=filename,
                    local_path=str(local_path),
                    media_type=media_type
                )
                result.images.append(img)
                print(f"[{'视频' if is_video else '图片'}] 已保存: {filename}")
        except Exception as e:
            print(f"[{'视频' if mime_type.startswith('video/') else '图片'}] 解析attachment失败: {e}")
            import traceback
            traceback.print_exc()


def get_image_base_url(fallback_host_url: str, account_manager=None, request=None) -> str:
    """获取图片基础URL
    
    优先使用配置文件中的 image_base_url，否则使用请求的 host_url
    如果配置的是 127.0.0.1、localhost 或 0.0.0.0，自动从请求头中获取真实的 host
    """
    configured_url = account_manager.config.get("image_base_url", "").strip() if account_manager else ""
    
    # 如果配置为空，直接使用请求的 host_url
    if not configured_url:
        return fallback_host_url
    
    # 如果配置的是 127.0.0.1、localhost 或 0.0.0.0，尝试从请求头获取真实的 host
    if request and ("127.0.0.1" in configured_url or "localhost" in configured_url.lower() or "0.0.0.0" in configured_url):
        # 尝试从请求头获取真实的 host（支持反向代理）
        try:
            # 优先使用 X-Forwarded-Host（反向代理场景）
            forwarded_host = request.headers.get("X-Forwarded-Host", "")
            if forwarded_host:
                # 获取协议（优先使用 X-Forwarded-Proto，否则从配置中提取）
                proto = request.headers.get("X-Forwarded-Proto", "http")
                if "://" in configured_url:
                    proto = configured_url.split("://")[0]
                # 从配置中提取端口（如果有）
                port = ""
                if "://" in configured_url:
                    parts = configured_url.split("://")[1].split("/")[0]
                    if ":" in parts:
                        port = ":" + parts.split(":")[1]
                return f"{proto}://{forwarded_host}{port}/"
            
            # 如果没有 X-Forwarded-Host，尝试使用 Host 头
            host_header = request.headers.get("Host", "")
            if host_header and "127.0.0.1" not in host_header and "localhost" not in host_header.lower() and "0.0.0.0" not in host_header:
                proto = "http"
                if "://" in configured_url:
                    proto = configured_url.split("://")[0]
                # 从配置中提取端口（如果有）
                port = ""
                if "://" in configured_url:
                    parts = configured_url.split("://")[1].split("/")[0]
                    if ":" in parts:
                        port = ":" + parts.split(":")[1]
                return f"{proto}://{host_header}{port}/"
            
            # 如果 Host 头也是 127.0.0.1 或 localhost，尝试从 remote_addr 获取
            # 注意：这通常不准确，因为可能是代理后的地址
            remote_addr = request.remote_addr
            if remote_addr and remote_addr != "127.0.0.1":
                proto = "http"
                if "://" in configured_url:
                    proto = configured_url.split("://")[0]
                port = ""
                if "://" in configured_url:
                    parts = configured_url.split("://")[1].split("/")[0]
                    if ":" in parts:
                        port = ":" + parts.split(":")[1]
                return f"{proto}://{remote_addr}{port}/"
        except Exception:
            # 如果获取失败，使用原配置
            pass
    
    # 确保以 / 结尾
    if not configured_url.endswith("/"):
        configured_url += "/"
    return configured_url


def detect_client_image_format(request=None, request_data=None) -> str:
    """检测客户端支持的图片格式
    
    检测优先级：
    1. 请求参数中的 image_format 或 response_format（显式指定）
    2. User-Agent 检测（已知客户端）- 优先于消息格式检测
    3. 检查客户端发送的消息格式（如果发送数组格式，说明支持数组格式）
    4. 默认使用数组格式（OpenAI 标准格式）
    
    Args:
        request: Flask request 对象
        request_data: 请求的 JSON 数据
    
    Returns:
        "array" - 数组格式（OpenAI 标准）
        "markdown" - Markdown 格式
        "url" - 纯 URL 格式
    """
    try:
        # 先把请求数据规范成字典，避免字符串/其它类型直接调用 .get 触发异常
        if isinstance(request_data, str):
            try:
                request_data = json.loads(request_data)
            except Exception:
                request_data = {}
        elif not isinstance(request_data, dict):
            request_data = {}

        # 1. 检查请求参数中的格式偏好（最高优先级）
        if request_data:
            image_format = request_data.get('image_format') or request_data.get('response_format')
            if image_format in ['array', 'markdown', 'url']:
                return image_format
        
        if not request:
            return "array"  # 默认使用数组格式
        
        # 2. User-Agent 检测（已知客户端）- 优先于消息格式检测
        # 这样可以确保已知客户端（如 Cherry Studio）的格式不会被消息格式覆盖
        user_agent = request.headers.get('User-Agent', '').lower()
        
        # 已知需要 Markdown 格式的客户端（优先检查，避免被消息格式覆盖）
        markdown_format_clients = [
            'cherry',  # Cherry Studio
            'studio',  # 某些 Studio 客户端
        ]
        
        # 已知支持数组格式的客户端
        array_format_clients = [
            'cursor',  # Cursor IDE
            'vscode',  # VS Code
            'chatgpt',  # ChatGPT
            'openai',  # OpenAI 官方客户端
            'anthropic',  # Claude
        ]
        
        # 优先检查 Markdown 格式客户端（因为 Cherry Studio 上传图片时也会发送数组格式）
        for client in markdown_format_clients:
            if client in user_agent:
                return "markdown"
        
        # 检查数组格式客户端
        for client in array_format_clients:
            if client in user_agent:
                return "array"
        
        # 3. 检查客户端发送的消息格式（如果发送数组格式，说明支持数组格式）
        # 注意：这个检查在 User-Agent 检测之后，避免覆盖已知客户端
        if request_data:
            messages = request_data.get('messages', [])
            for msg in messages:
                content = msg.get('content', '')
                # 如果消息内容是数组格式，说明客户端支持数组格式
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get('type') in ['text', 'image_url', 'file']:
                            return "array"  # 客户端支持数组格式
        
        # 4. 检查 Accept 头（某些客户端可能通过 Accept 头表明支持的格式）
        accept = request.headers.get('Accept', '').lower()
        if 'application/json' in accept and 'text/markdown' not in accept:
            # 如果只接受 JSON，可能支持数组格式
            return "array"
        
        # 5. 默认使用数组格式（OpenAI 标准格式）
        return "array"
    except Exception:
        return "array"


def build_openai_response_content(chat_response: ChatResponse, host_url: str, account_manager=None, request=None, request_data=None):
    """构建OpenAI格式的响应内容
    
    如果有图片/视频，根据客户端支持的格式返回：
    - 数组格式：[{type: "text", text: "..."}, {type: "image_url", image_url: {url: "..."}}]
    - Markdown 格式：![image](url)
    - URL 格式：直接返回图片 URL
    
    如果没有图片，返回纯文本字符串
    
    返回: str | List[Dict] - 纯文本或内容数组
    """
    result_text = chat_response.text
    
    # 检测客户端支持的图片格式
    image_format = detect_client_image_format(request, request_data)
    
    # 如果有图片或视频
    if chat_response.images:
        base_url = get_image_base_url(host_url, account_manager, request)
        
        # 根据检测到的格式返回不同的内容
        if image_format == "markdown":
            markdown_parts = []
            if result_text and result_text.strip():
                markdown_parts.append(result_text)
            
            # 添加 Markdown 格式的图片链接
            for img in chat_response.images:
                # 优先使用 base64 格式（如果存在），这样客户端可以直接显示图片
                if img.base64_data:
                    # 使用 base64 data URL 格式，让客户端可以直接显示图片
                    mime_type = img.mime_type or "image/png"
                    base64_url = f"data:{mime_type};base64,{img.base64_data}"
                    markdown_parts.append(f"![image]({base64_url})")
                elif img.url:
                    # 使用普通 URL
                    markdown_parts.append(f"![image]({img.url})")
                elif img.file_name:
                    # 本地缓存，构建本地 URL
                    if img.media_type == "video":
                        media_url = f"{base_url}video/{img.file_name}"
                    else:
                        media_url = f"{base_url}image/{img.file_name}"
                    markdown_parts.append(f"![image]({media_url})")
                else:
                    continue  # 跳过没有 URL 或 base64 的图片
            
            # 返回 Markdown 格式的文本
            return "\n\n".join(markdown_parts) if markdown_parts else result_text
        
        elif image_format == "url":
            # URL 格式：直接返回图片 URL（每行一个）
            url_parts = []
            if result_text and result_text.strip():
                url_parts.append(result_text)
            
            # 添加图片 URL（每行一个）
            for img in chat_response.images:
                if img.url:
                    url_parts.append(img.url)
                elif img.file_name:
                    if img.media_type == "video":
                        media_url = f"{base_url}video/{img.file_name}"
                    else:
                        media_url = f"{base_url}image/{img.file_name}"
                    url_parts.append(media_url)
                elif img.base64_data:
                    # base64 格式也作为 URL 返回
                    mime_type = img.mime_type or "image/png"
                    base64_url = f"data:{mime_type};base64,{img.base64_data}"
                    url_parts.append(base64_url)
                else:
                    continue  # 跳过没有 URL 或 base64 的图片
            
            # 返回 URL 格式的文本（每行一个 URL）
            return "\n".join(url_parts) if url_parts else result_text
        
        # 默认使用数组格式（标准 OpenAI 格式）
        content_array = []
        
        # 如果有文本，先添加文本部分
        if result_text and result_text.strip():
            content_array.append({
                "type": "text",
                "text": result_text
            })
        
        # 添加图片/视频部分
        for img in chat_response.images:
            # 优先使用 base64 格式（如果存在），这样客户端可以直接显示图片
            # 如果客户端支持 base64，会直接显示；如果不支持，会回退到 URL
            if img.base64_data:
                # 使用 base64 data URL 格式，让客户端可以直接显示图片
                mime_type = img.mime_type or "image/png"
                base64_url = f"data:{mime_type};base64,{img.base64_data}"
                content_array.append({
                    "type": "image_url",
                    "image_url": {
                        "url": base64_url
                    }
                })
            elif img.url:
                # 使用普通 URL
                content_array.append({
                    "type": "image_url",
                    "image_url": {
                        "url": img.url
                    }
                })
            elif img.file_name:
                # 本地缓存，构建本地 URL
                if img.media_type == "video":
                    media_url = f"{base_url}video/{img.file_name}"
                else:
                    media_url = f"{base_url}image/{img.file_name}"
                content_array.append({
                    "type": "image_url",
                    "image_url": {
                        "url": media_url
                    }
                })
            else:
                continue  # 跳过没有 URL 或 base64 的图片
        
        # 如果只有图片没有文本，返回数组；如果有文本，也返回数组
        return content_array if content_array else result_text
    
    # 没有图片，返回纯文本
    return result_text
