"""
CoPaw 本地网关通道 (纯原生异步版)
不依赖 FastAPI 注入，自带 8089 异步服务器 + WeFlow 监听。
"""
import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Optional, Dict, List

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from agentscope_runtime.engine.schemas.agent_schemas import (
        TextContent, ImageContent, VideoContent,
        AudioContent, FileContent, ContentType,
    )
except ImportError:
    ContentType = type("CT", (), {"TEXT": "text", "IMAGE": "image", "VIDEO": "video", "AUDIO": "audio", "FILE": "file"})
    TextContent = lambda **kw: {"type": "text", **kw}
    ImageContent = lambda **kw: {"type": "image", **kw}
    VideoContent = lambda **kw: {"type": "video", **kw}
    AudioContent = lambda **kw: {"type": "audio", **kw}
    FileContent = lambda **kw: {"type": "file", **kw}

from copaw.app.channels.base import BaseChannel

# ==================== 内存存储 ====================
class _Store:
    def __init__(self):
        self.msgs = defaultdict(list)
        self.meta = {}
    def add(self, sid, m): self.msgs[sid].append(m)
    def get(self, sid): return list(self.msgs.get(sid, []))
    def clear(self, sid):
        c = len(self.msgs.get(sid, []))
        self.msgs[sid] = []
        return c

S = _Store()
processed_keys = set()

# ==================== 通道主体 ====================
class LocalProxyChannel(BaseChannel):
    channel = "local_proxy"
    _inst = None

    def __init__(self, process=None, enabled=True, weflow_token="", weflow_api="http://127.0.0.1:5031", **kw):
        super().__init__(process, on_reply_sent=kw.get("on_reply_sent"))
        self.enabled = enabled
        self.weflow_token = weflow_token
        self.weflow_api = weflow_api
        LocalProxyChannel._inst = self
        self._server = None

    @classmethod
    def from_config(cls, process, config, on_reply_sent=None, show_tool_details=True):
        # 兼容 CoPaw 传入 config 为字典或对象的情况
        def get_val(c, key, default=""):
            if isinstance(c, dict):
                return c.get(key, default)
            return getattr(c, key, default)
            
        return cls(
            process=process,
            enabled=get_val(config, "enabled", True),
            weflow_token=get_val(config, "weflow_token", ""),
            weflow_api=get_val(config, "weflow_api", "http://127.0.0.1:5031"),
            on_reply_sent=on_reply_sent
        )

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return cls(process=process, on_reply_sent=on_reply_sent)

    # ==================== 生命周期 ====================
    async def start(self):
        # 单例保护：防止多个 Agent 重复启动同一个 HTTP 服务和监听
        if getattr(self, '_started', False):
            return
        self._started = True

        # 1. 尝试启动 HTTP 服务器 (8089)
        try:
            self._server = await asyncio.start_server(self._handle_conn, '0.0.0.0', 8089)
            print("===== [local_proxy] 异步 API 服务已启动: http://localhost:8089 =====")
        except OSError as e:
            print(f"===== [local_proxy] 8089 端口被占用，跳过 HTTP 服务 (不影响微信监听) =====")
            self._server = None
            
        # 2. 启动 WeFlow 微信监听 (核心功能)
        if HAS_HTTPX and self.weflow_token:
            asyncio.create_task(self._listen_weflow())
            print(f"===== [local_proxy] WeFlow 微信监听已启动 (Token: {self.weflow_token[:4]}...) =====")
        else:
            if not HAS_HTTPX:
                print("===== [local_proxy] 缺少 httpx，WeFlow 监听未启动。请执行: pip install httpx =====")
            else:
                print("===== [local_proxy] 未配置 weflow_token，WeFlow 监听未启动 =====")

    # ==================== 手写极简异步 HTTP 服务器 ====================
    async def _handle_conn(self, reader, writer):
        try:
            req_line = await reader.readline()
            if not req_line: return
            parts = req_line.decode().strip().split(' ', 2)
            if len(parts) < 2: return
            method, path = parts[0], parts[1]
            
            # 读 Headers 拿 Body 长度
            content_length = 0
            while True:
                line = await reader.readline()
                if line == b'\r\n': break
                if line.lower().startswith(b'content-length:'):
                    content_length = int(line.split(b':')[1].strip())
            
            body = await reader.read(content_length) if content_length > 0 else b'{}'
            
            # 路由并返回
            res = await self._route(method, path, body)
            resp_str = json.dumps(res, ensure_ascii=False)
            http_resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{resp_str}"
            writer.write(http_resp.encode())
            await writer.drain()
        except Exception as e:
            pass # 忽略非法请求
        finally:
            writer.close()

    async def _route(self, method, path, body):
        try:
            data = json.loads(body) if body else {}
        except: 
            data = {}

        # 接口：状态页
        if path == "/api/local-proxy/" and method == "GET":
            return {"status": "running", "weflow": bool(self.weflow_token and HAS_HTTPX)}

        # 接口：同步发送
        if path == "/api/local-proxy/send" and method == "POST":
            if not self.enabled: return {"error": "disabled"}
            sid = data.get("session_id") or "default"
            self._enqueue(data)
            bc = len(S.get(sid)); e = 0.0
            while e < (data.get("timeout") or 120):
                await asyncio.sleep(0.15); e += 0.15
                cm = S.get(sid)
                if len(cm) > bc: return {"status": "ok", "messages": cm[bc:]}
            return {"status": "timeout", "messages": []}

        # 接口：异步入队
        if path == "/api/local-proxy/callback" and method == "POST":
            if not self.enabled: return {"error": "disabled"}
            sid = data.get("session_id") or "default"
            self._enqueue(data)
            return {"status": "queued", "session_id": sid}

        # 接口：获取回复
        if path.startswith("/api/local-proxy/messages/") and method == "GET":
            sid = path.split("/")[-1]
            return {"messages": S.get(sid)}

        # 接口：清空回复
        if path.startswith("/api/local-proxy/messages/") and method == "DELETE":
            sid = path.split("/")[-1]
            return {"cleared": S.clear(sid)}

        return {"error": "not_found"}

    # ==================== CoPaw 核心对接 ====================
    def get_to_handle_from_request(self, req): return req.channel_meta.get("session_id", req.user_id)

    def build_agent_request_from_native(self, p):
        p = p if isinstance(p, dict) else {}
        uid = p.get("user_id") or "anonymous"
        sid = p.get("session_id") or uid
        m = p.get("metadata") or {}
        S.meta[sid] = {"user_id": uid}
        m["_session_info"] = S.meta[sid]; m["session_id"] = sid
        parts = []
        if p.get("text"): parts.append(TextContent(type=ContentType.TEXT, text=p["text"]))
        for a in p.get("attachments") or []:
            t, u = (a.get("type") or "file").lower(), a.get("url") or ""
            if not u: continue
            if t == "image": parts.append(ImageContent(type=ContentType.IMAGE, image_url=u))
            elif t == "video": parts.append(VideoContent(type=ContentType.VIDEO, video_url=u))
            elif t == "audio": parts.append(AudioContent(type=ContentType.AUDIO, data=u))
            else: parts.append(FileContent(type=ContentType.FILE, file_url=u))
        if not parts: parts = [TextContent(type=ContentType.TEXT, text="")]
        req = self.build_agent_request_from_user_content(channel_id=self.channel, sender_id=uid, session_id=sid, content_parts=parts, channel_meta=m)
        req.channel_meta = m
        return req

    async def send(self, to, text, meta=None):
        S.add(to, {"id": str(uuid.uuid4()), "session_id": to, "text": text, "created_at": time.time()})

    @classmethod
    def inst(cls):
        if not cls._inst: raise Exception("not_init")
        return cls._inst

    # ==================== WeFlow 微信监听核心 ====================
    async def _listen_weflow(self):
        print(f"[WeFlow] 正在连接 {self.weflow_api} ...")
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", f"{self.weflow_api}/api/v1/push/messages", params={"access_token": self.weflow_token}) as resp:
                        if resp.status_code != 200:
                            print(f"[WeFlow] 认证失败(状态码:{resp.status_code})，10秒后重试...")
                            await asyncio.sleep(10); continue
                        print("[WeFlow] ✅ 连接成功，实时监听微信消息中...")
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "): continue
                            try:
                                data = json.loads(line[6:])
                                if data.get("event") == "message.new":
                                    await self._handle_wx_msg(data)
                            except: pass
            except Exception as e:
                print(f"[WeFlow] 断开: {e}，5秒后重连...")
                await asyncio.sleep(5)

    async def _handle_wx_msg(self, data):
        mk = data.get("messageKey")
        if not mk or mk in processed_keys: return
        processed_keys.add(mk)
        if len(processed_keys) > 2000: processed_keys.clear()

        sid = data.get("sessionId", "unknown")
        name = data.get("sourceName", "未知")
        grp = data.get("groupName", "")
        content = data.get("content", "").strip()
        if not content: return

        src = f"微信-{'群['+grp+']' if grp else '私聊'}-{name}"
        print(f"\n[📥 {src}]: {content}")

        attachments = []
        text_send = content
        if content in ["[图片]", "[视频]", "[语音]", "[文件]", "[表情]"]:
            media_info = await self._get_wx_media(sid)
            if media_info:
                attachments.append(media_info)
                text_send = f"收到一个{content}，请查看附件"
            else:
                text_send = f"收到一个{content}，无法获取"

        payload = {
            "session_id": f"weflow_{sid}",
            "text": f"[{src}]: {text_send}",
            "attachments": attachments if attachments else None
        }
        self._enqueue(payload)
        asyncio.create_task(self._poll_reply(payload["session_id"], 120, src))

    async def _get_wx_media(self, talker):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.weflow_api}/api/v1/messages", params={"talker": talker, "limit": 1, "media": "1"}, headers={"Authorization": f"Bearer {self.weflow_token}"})
                d = r.json()
                if d.get("success") and d.get("messages"):
                    m = d["messages"][0]; mt = m.get("mediaType", "file"); url = m.get("mediaUrl"); lp = m.get("mediaLocalPath")
                    at = "image" if mt in ["image","emoji"] else ("video" if mt=="video" else ("audio" if mt=="voice" else "file"))
                    fu = None
                    if url and "/api/v1/media/" in url: fu = f"{self.weflow_api}/api/v1/media/{url.split('/api/v1/media/')[-1]}"
                    if not fu and lp: fu = lp.replace("\\", "/"); fu = f"file:///{fu}" if not fu.startswith("/") else fu
                    if fu: return {"type": at, "url": fu}
        except Exception as e: print(f"[WeFlow] 获取媒体失败: {e}")
        return None

    async def _poll_reply(self, sid, timeout, src):
        """直接读内存，不走 HTTP，极速零开销"""
        e = 0.0
        while e < timeout:
            await asyncio.sleep(1); e += 1
            msgs = S.get(sid) # 直接读内存
            if msgs:
                print(f"\n{'='*50}\n[✅ CoPaw 回复 {src}]:\n{'='*50}")
                for msg in msgs: print(f"🤖: {msg['text']}")
                print(f"{'='*50}\n")
                S.clear(sid)
                return