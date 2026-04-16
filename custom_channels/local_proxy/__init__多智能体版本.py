"""
CoPaw 多 Agent 路由网关 (一体化版)
全局共享 8089 端口和 WeFlow 监听，支持按规则将消息分发给不同 Agent。
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

# ==================== 全局状态 ====================
class _Store:
    def __init__(self): self.msgs = defaultdict(list); self.meta = {}
    def add(self, sid, m): self.msgs[sid].append(m)
    def get(self, sid): return list(self.msgs.get(sid, []))
    def clear(self, sid):
        c = len(self.msgs.get(sid, [])); self.msgs[sid] = []; return c

S = _Store()
processed_keys = set()

# ==================== 多 Agent 路由核心 ====================
class LocalProxyChannel(BaseChannel):
    channel = "local_proxy"
    
    # 类级别全局控制
    _instances: Dict[str, "LocalProxyChannel"] = {}
    _main_instance: Optional["LocalProxyChannel"] = None
    _http_started = False
    _weflow_started = False

    def __init__(self, process=None, enabled=True, weflow_token="", weflow_api="http://127.0.0.1:5031", routing_rules=None, **kw):
        super().__init__(process, on_reply_sent=kw.get("on_reply_sent"))
        self.enabled = enabled
        self.weflow_token = weflow_token
        self.weflow_api = weflow_api
        self.routing_rules = routing_rules or {}
        
        # 提取当前 Agent 的唯一 ID
        self.agent_id = getattr(process, 'name', getattr(process, 'agent_id', 'default')) or 'default'
        
        # 将自己注册到全局实例池
        LocalProxyChannel._instances[self.agent_id] = self

    @classmethod
    def from_config(cls, process, config, on_reply_sent=None, show_tool_details=True):
        def get_val(c, key, default=""):
            if isinstance(c, dict): return c.get(key, default)
            return getattr(c, key, default)
            
        return cls(
            process=process,
            enabled=get_val(config, "enabled", True),
            weflow_token=get_val(config, "weflow_token", ""),
            weflow_api=get_val(config, "weflow_api", "http://127.0.0.1:5031"),
            routing_rules=get_val(config, "routing_rules", {}),
            on_reply_sent=on_reply_sent
        )

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return cls(process=process, on_reply_sent=on_reply_sent)

    def _route_enqueue(self, agent_id: str, payload: dict):
        """核心路由：将消息投递给指定 Agent 的通道实例"""
        target = LocalProxyChannel._instances.get(agent_id, LocalProxyChannel._main_instance)
        if target and target.enabled:
            target._enqueue(payload)
            return True
        return False

    # ==================== 生命周期 ====================
    async def start(self):
        # 1. 全局单例：只启动一次 HTTP 服务
        if not LocalProxyChannel._http_started:
            try:
                self._server = await asyncio.start_server(self._handle_conn, '0.0.0.0', 8089)
                LocalProxyChannel._http_started = True
                LocalProxyChannel._main_instance = self
                print(f"===== [local_proxy] 主实例 ({self.agent_id}) 启动 HTTP: http://localhost:8089 =====")
            except OSError:
                print("===== [local_proxy] 8089 端口被占用，跳过 HTTP =====")
                
        # 2. 全局单例：只启动一次 WeFlow 监听
        if not LocalProxyChannel._weflow_started and HAS_HTTPX and self.weflow_token:
            LocalProxyChannel._weflow_started = True
            LocalProxyChannel._main_instance = self # 确保主实例是能处理微信的
            asyncio.create_task(self._listen_weflow())
            print(f"===== [local_proxy] 主实例启动 WeFlow 监听 (Token: {self.weflow_token[:4]}...) =====")
            
        print(f"===== [local_proxy] Agent [{self.agent_id}] 已注册到路由池 =====")

    async def stop(self):
        if hasattr(self, '_server') and self._server: self._server.close()

    # ==================== HTTP 服务器 ====================
    async def _handle_conn(self, reader, writer):
        try:
            req_line = await reader.readline()
            if not req_line: return
            parts = req_line.decode().strip().split(' ', 2)
            if len(parts) < 2: return
            method, path = parts[0], parts[1]
            content_length = 0
            while True:
                line = await reader.readline()
                if line == b'\r\n': break
                if line.lower().startswith(b'content-length:'): content_length = int(line.split(b':')[1].strip())
            body = await reader.read(content_length) if content_length > 0 else b'{}'
            res = await self._route(method, path, body)
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n\r\n{json.dumps(res, ensure_ascii=False)}".encode())
            await writer.drain()
        except: pass
        finally: writer.close()

    async def _route(self, method, path, body):
        try: data = json.loads(body) if body else {}
        except: data = {}

        if path == "/api/local-proxy/" and method == "GET":
            return {"status": "running", "agents": list(LocalProxyChannel._instances.keys())}

        # 同步发送 (支持指定 agent_id)
        if path == "/api/local-proxy/send" and method == "POST":
            aid = data.pop("agent_id", None) # 提取 agent_id，不传给 CoPaw
            if not self._route_enqueue(aid, data): return {"error": "disabled_or_not_found"}
            sid = data.get("session_id") or "default"
            bc = len(S.get(sid)); e = 0.0
            while e < (data.get("timeout") or 120):
                await asyncio.sleep(0.15); e += 0.15
                cm = S.get(sid)
                if len(cm) > bc: return {"status": "ok", "messages": cm[bc:]}
            return {"status": "timeout", "messages": []}

        # 异步入队 (支持指定 agent_id)
        if path == "/api/local-proxy/callback" and method == "POST":
            aid = data.pop("agent_id", None)
            if not self._route_enqueue(aid, data): return {"error": "disabled_or_not_found"}
            return {"status": "queued", "session_id": data.get("session_id")}

        if path.startswith("/api/local-proxy/messages/") and method == "GET":
            return {"messages": S.get(path.split("/")[-1])}
        if path.startswith("/api/local-proxy/messages/") and method == "DELETE":
            return {"cleared": S.clear(path.split("/")[-1])}
            
        return {"error": "not_found"}

    # ==================== CoPaw 核心对接 ====================
    def get_to_handle_from_request(self, req): return req.channel_meta.get("session_id", req.user_id)

    def build_agent_request_from_native(self, p):
        p = p if isinstance(p, dict) else {}
        uid = p.get("user_id") or "anonymous"
        sid = p.get("session_id") or uid
        m = p.get("metadata") or {}
        m["_session_info"] = {"user_id": uid}; m["session_id"] = sid
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

    # ==================== WeFlow 监听 ====================
    async def _listen_weflow(self):
        print(f"[WeFlow] 正在连接 {self.weflow_api} ...")
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", f"{self.weflow_api}/api/v1/push/messages", params={"access_token": self.weflow_token}) as resp:
                        if resp.status_code != 200:
                            print(f"[WeFlow] 认证失败(状态码:{resp.status_code})，10秒后重试..."); await asyncio.sleep(10); continue
                        print("[WeFlow] ✅ 连接成功，实时监听中...")
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "): continue
                            try:
                                data = json.loads(line[6:])
                                if data.get("event") == "message.new": await self._handle_wx_msg(data)
                            except: pass
            except Exception as e:
                print(f"[WeFlow] 断开: {e}，5秒后重连..."); await asyncio.sleep(5)

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
            if media_info: attachments.append(media_info); text_send = f"收到一个{content}，请查看附件"
            else: text_send = f"收到一个{content}，无法获取"

        # 【核心】路由决策：通过 sessionId(群号/好友号) 匹配路由规则
        target_agent_id = None
        if grp and grp in self.routing_rules:
            target_agent_id = self.routing_rules[grp]
        elif sid in self.routing_rules:
            target_agent_id = self.routing_rules[sid]
        # 如果没匹配到规则，默认交给主实例处理

        payload = {
            "session_id": f"weflow_{sid}",
            "text": f"[{src}]: {text_send}",
            "attachments": attachments if attachments else None
        }
        
        if self._route_enqueue(target_agent_id, payload):
            asyncio.create_task(self._poll_reply(payload["session_id"], 120, src))
        else:
            print(f"[⚠️ 路由失败] 找不到可用的 Agent 实例处理此消息")

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
        e = 0.0
        while e < timeout:
            await asyncio.sleep(1); e += 1
            msgs = S.get(sid)
            if msgs:
                print(f"\n{'='*50}\n[✅ CoPaw 回复 {src}]:\n{'='*50}")
                for msg in msgs: print(f"🤖: {msg['text']}")
                print(f"{'='*50}\n")
                S.clear(sid)
                return