"""
CoPaw 自定义频道 - 企业数字员工网关
通过 WeFlow 监听微信消息，支持多Agent路由、老板审批、@触发模式、黑名单等功能。
所有配置在 agent.json → channels.local_proxy 中管理，无独立配置文件。

安装: copaw channels install local_proxy
配置: copaw channels config local_proxy
管理: http://localhost:8088/api/local-proxy/admin
"""
import asyncio
import json
import time
import uuid
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict, is_dataclass
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
    ContentType = type("CT", (), {"TEXT":"text","IMAGE":"image","VIDEO":"video","AUDIO":"audio","FILE":"file"})
    TextContent = lambda **kw: {"type":"text",**kw}
    ImageContent = lambda **kw: {"type":"image",**kw}
    VideoContent = lambda **kw: {"type":"video",**kw}
    AudioContent = lambda **kw: {"type":"audio",**kw}
    FileContent = lambda **kw: {"type":"file",**kw}

from copaw.app.channels.base import BaseChannel

# ==================== 数据模型 ====================
@dataclass
class BlacklistEntry:
    session_id: str = ""
    display_name: str = ""
    reason: str = ""
    custom_reply: str = "无权限"
    added_at: float = 0.0

@dataclass
class Task:
    id: str = ""
    session_id: str = ""
    session_name: str = ""
    source_type: str = ""
    source_name: str = ""
    content: str = ""
    status: str = "pending"
    agent_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    result: Optional[str] = None
    boss_approved: bool = False
    approval_id: Optional[str] = None
    error: Optional[str] = None
    messages_count: int = 0

@dataclass
class ApprovalRequest:
    id: str = ""
    task_id: str = ""
    question: str = ""
    session_info: dict = field(default_factory=dict)
    status: str = "pending"
    created_at: float = 0.0
    responded_at: Optional[float] = None
    boss_response: Optional[str] = None

@dataclass
class SessionContext:
    session_id: str = ""
    display_name: str = ""
    session_type: str = "private"
    message_buffer: list = field(default_factory=list)
    active_task_id: Optional[str] = None
    last_trigger_time: Optional[float] = None
    last_message_time: float = 0.0
    message_count: int = 0
    total_processed: int = 0

@dataclass
class AuditEntry:
    id: str = ""
    timestamp: float = 0.0
    action: str = ""
    category: str = ""
    detail: str = ""
    session_id: Optional[str] = None

# ==================== 全局状态 ====================
G_TASKS: Dict[str, Task] = {}
G_APPROVALS: Dict[str, ApprovalRequest] = {}
G_SESSIONS: Dict[str, SessionContext] = {}
G_AUDIT: List[AuditEntry] = []
processed_keys: set = set()

class _Store:
    """兼容旧版消息存储"""
    def __init__(self): self.msgs = defaultdict(list)
    def add(self, sid, m): self.msgs[sid].append(m)
    def get(self, sid): return list(self.msgs.get(sid, []))
    def clear(self, sid): c = len(self.msgs.get(sid, [])); self.msgs[sid] = []; return c
S = _Store()

def _now(): return time.time()

def _dc(cls, data):
    """字典转 dataclass"""
    if not isinstance(data, dict): return data
    ftypes = {f.name: f.type for f in cls.__dataclass_fields__.values()}
    r = {}
    for k, v in data.items():
        if k not in ftypes: continue
        ft = ftypes[k]
        if hasattr(ft, '__dataclass_fields__') and isinstance(v, dict):
            r[k] = _dc(ft, v)
        elif (hasattr(ft, '__origin__') and hasattr(ft, '__args__') and ft.__args__
              and is_dataclass(ft.__args__[0])) and isinstance(v, list):
            r[k] = [_dc(ft.__args__[0], i) if isinstance(i, dict) else i for i in v]
        else:
            r[k] = v
    return cls(**r)

async def audit(action, category, detail, session_id=None):
    G_AUDIT.append(AuditEntry(id=str(uuid.uuid4())[:12], timestamp=_now(),
                              action=action, category=category, detail=detail, session_id=session_id))
    if len(G_AUDIT) > 5000: G_AUDIT[:] = G_AUDIT[-3000:]


# ==================== 频道实现 ====================
class LocalProxyChannel(BaseChannel):
    """
    企业数字员工网关频道。

    agent.json 配置示例:
    {
      "channels": {
        "local_proxy": {
          "enabled": true,

          // CoPaw 通用字段
          "dm_policy": "open",
          "group_policy": "open",
          "allow_from": [],
          "deny_message": "无权限",
          "require_mention": false,

          // WeFlow 连接
          "weflow_token": "your_token",
          "weflow_api": "http://127.0.0.1:5031",

          // 老板配置
          "boss_wxid": "wxid_xxx",
          "boss_name": "王总",
          "boss_approval_enabled": true,
          "boss_approval_timeout": 3600,
          "boss_report_enabled": true,
          "boss_report_on_complete": true,
          "boss_report_on_error": true,
          "boss_auto_approve_keywords": ["同意","可以","批","ok"],

          // 触发模式 (require_mention=true 时自动为 mention)
          "trigger_mode": "mention",
          "mention_keywords": ["@"],
          "max_buffer_messages": 50,
          "task_idle_timeout": 300,

          // 黑名单 (扩展字段)
          "blacklist": [
            {"session_id":"wxid_bad","display_name":"广告号","reason":"发广告","custom_reply":"无权限"}
          ],

          // 路由规则
          "routing_rules": {"客户群A": "agent_sales"}
        }
      }
    }
    """
    channel = "local_proxy"

    _instances: Dict[str, "LocalProxyChannel"] = {}
    _main_instance: Optional["LocalProxyChannel"] = None
    _weflow_started = False
    _bg_tasks: list = []

    def __init__(self, process=None, enabled=True, on_reply_sent=None, **kw):
        super().__init__(process, on_reply_sent=on_reply_sent)
        self.enabled = enabled

        # WeFlow
        self.weflow_token = kw.get("weflow_token", "")
        self.weflow_api = kw.get("weflow_api", "http://127.0.0.1:5031")

        # CoPaw 通用字段
        self.dm_policy = kw.get("dm_policy", "open")
        self.group_policy = kw.get("group_policy", "open")
        self.allow_from = kw.get("allow_from", []) or []
        self.deny_message = kw.get("deny_message", "无权限")
        self.require_mention = kw.get("require_mention", False)

        # 老板
        self.boss_wxid = kw.get("boss_wxid", "")
        self.boss_name = kw.get("boss_name", "老板")
        self.boss_approval_enabled = kw.get("boss_approval_enabled", True)
        self.boss_approval_timeout = int(kw.get("boss_approval_timeout", 3600))
        self.boss_report_enabled = kw.get("boss_report_enabled", True)
        self.boss_report_on_complete = kw.get("boss_report_on_complete", True)
        self.boss_report_on_error = kw.get("boss_report_on_error", True)
        self.boss_auto_approve_keywords = kw.get("boss_auto_approve_keywords", []) or []

        # 触发模式
        self.trigger_mode = "mention" if self.require_mention else kw.get("trigger_mode", "always")
        self.mention_keywords = kw.get("mention_keywords", []) or ["@"]
        self.max_buffer_messages = int(kw.get("max_buffer_messages", 50))
        self.task_idle_timeout = int(kw.get("task_idle_timeout", 300))

        # 黑名单 (扩展字段)
        bl_raw = kw.get("blacklist", []) or []
        self.blacklist = [_dc(BlacklistEntry, e) if isinstance(e, dict) else e for e in bl_raw]

        # 路由规则
        self.routing_rules = kw.get("routing_rules", {}) or {}

        # Agent 标识
        self.agent_id = getattr(process, 'name', getattr(process, 'agent_id', 'default')) or 'default'

        # 全局注册
        LocalProxyChannel._instances[self.agent_id] = self
        if not LocalProxyChannel._main_instance:
            LocalProxyChannel._main_instance = self

    @classmethod
    def from_config(cls, process, config, on_reply_sent=None, show_tool_details=True):
        """从 CoPaw 配置构建，自动读取所有字段（含 extra 扩展字段）"""
        kw = {}
        for f in [
            "enabled", "weflow_token", "weflow_api",
            "dm_policy", "group_policy", "allow_from", "deny_message", "require_mention",
            "bot_prefix", "filter_tool_messages", "filter_thinking",
            "boss_wxid", "boss_name", "boss_approval_enabled", "boss_approval_timeout",
            "boss_report_enabled", "boss_report_on_complete", "boss_report_on_error",
            "boss_auto_approve_keywords",
            "trigger_mode", "mention_keywords", "max_buffer_messages", "task_idle_timeout",
            "blacklist", "routing_rules",
        ]:
            v = getattr(config, f, None)
            if v is not None:
                kw[f] = v
        return cls(process=process, on_reply_sent=on_reply_sent, **kw)

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return cls(process=process, on_reply_sent=on_reply_sent)

    def get_config_dict(self):
        """导出完整配置字典，用于保存回 agent.json"""
        return {
            "enabled": self.enabled,
            "weflow_token": self.weflow_token,
            "weflow_api": self.weflow_api,
            "dm_policy": self.dm_policy,
            "group_policy": self.group_policy,
            "allow_from": self.allow_from,
            "deny_message": self.deny_message,
            "require_mention": self.require_mention,
            "boss_wxid": self.boss_wxid,
            "boss_name": self.boss_name,
            "boss_approval_enabled": self.boss_approval_enabled,
            "boss_approval_timeout": self.boss_approval_timeout,
            "boss_report_enabled": self.boss_report_enabled,
            "boss_report_on_complete": self.boss_report_on_complete,
            "boss_report_on_error": self.boss_report_on_error,
            "boss_auto_approve_keywords": self.boss_auto_approve_keywords,
            "trigger_mode": "always" if not self.require_mention else self.trigger_mode,
            "mention_keywords": self.mention_keywords,
            "max_buffer_messages": self.max_buffer_messages,
            "task_idle_timeout": self.task_idle_timeout,
            "blacklist": [asdict(e) for e in self.blacklist],
            "routing_rules": self.routing_rules,
        }

    def apply_config(self, data):
        """运行时更新实例属性（管理后台调用后自动保存到 agent.json）"""
        for f in ["weflow_token","weflow_api","dm_policy","group_policy","deny_message",
                   "boss_wxid","boss_name","trigger_mode"]:
            if f in data: setattr(self, f, data[f])
        for f in ["enabled","require_mention","boss_approval_enabled","boss_report_enabled",
                   "boss_report_on_complete","boss_report_on_error"]:
            if f in data: setattr(self, f, bool(data[f]))
        for f in ["boss_approval_timeout","max_buffer_messages","task_idle_timeout"]:
            if f in data: setattr(self, f, int(data[f]))
        for f in ["allow_from","boss_auto_approve_keywords","mention_keywords"]:
            if f in data: setattr(self, f, data[f] or [])
        if "blacklist" in data:
            self.blacklist = [_dc(BlacklistEntry, e) if isinstance(e, dict) else e
                              for e in (data["blacklist"] or [])]
        if "routing_rules" in data:
            self.routing_rules = data["routing_rules"] or {}
        if self.require_mention and self.trigger_mode == "always":
            self.trigger_mode = "mention"

    def _route_enqueue(self, agent_id, payload):
        target = LocalProxyChannel._instances.get(agent_id, LocalProxyChannel._main_instance)
        if target and target.enabled:
            target._enqueue(payload)
            return True
        return False

    # ==================== 生命周期 ====================
    async def start(self):
        if not LocalProxyChannel._weflow_started and HAS_HTTPX and self.weflow_token:
            LocalProxyChannel._weflow_started = True
            LocalProxyChannel._main_instance = self
            LocalProxyChannel._bg_tasks = [
                asyncio.create_task(self._listen_weflow()),
                asyncio.create_task(self._idle_timeout_checker()),
                asyncio.create_task(self._approval_expiry_checker()),
            ]
            print(f"===== [local_proxy] WeFlow 已启动 | 触发:{self.trigger_mode} | 黑名单:{len(self.blacklist)} =====")

    async def stop(self):
        for t in LocalProxyChannel._bg_tasks:
            t.cancel()

    # ==================== 权限 ====================
    def _is_blacklisted(self, sid):
        for e in self.blacklist:
            if e.session_id == sid: return e
        return None

    def _is_allowed_by_policy(self, sid, stype):
        """CoPaw 通用字段 dm_policy/group_policy + allow_from"""
        policy = self.dm_policy if stype == "private" else self.group_policy
        if policy != "allowlist": return True
        return sid in self.allow_from

    def _is_boss_sender(self, data):
        if not self.boss_wxid: return False
        return data.get("sessionId", "") == self.boss_wxid

    def _should_trigger(self, sid, content):
        if self.trigger_mode == "always": return True
        ctx = G_SESSIONS.get(sid)
        if ctx and ctx.active_task_id:
            task = G_TASKS.get(ctx.active_task_id)
            if task and task.status in ("processing", "awaiting_approval", "approved"):
                return True
        cl = content.lower().strip()
        return any(kw.lower() in cl for kw in self.mention_keywords)

    # ==================== 老板审批 ====================
    async def _handle_boss_response(self, data, content):
        pending = [a for a in G_APPROVALS.values() if a.status == "pending"]
        if not pending:
            await audit("boss_ignored", "approval", f"老板消息无待审批: {content[:50]}")
            return
        approval = max(pending, key=lambda a: a.created_at)
        rl = content.strip().lower()
        reject = any(kw in rl for kw in ["拒绝","不行","不要","否","no"])
        if reject:
            await self._do_respond_approval(approval.id, content, False)
            task = G_TASKS.get(approval.task_id)
            if task:
                S.add(task.session_id, {"id":str(uuid.uuid4()),"session_id":task.session_id,
                    "text":"很抱歉，您的请求未获得批准。","created_at":_now(),"msg_type":"system"})
        else:
            await self._do_respond_approval(approval.id, content, True)
            task = G_TASKS.get(approval.task_id)
            if task:
                task.status = "processing"; task.updated_at = _now()
                self._route_enqueue(task.agent_id, {
                    "session_id": f"_continue_{task.id}",
                    "text": f"[老板已批准] 指示: {content}\n继续执行: {task.content[:200]}",
                    "metadata": {"_continuation":True,"_original_task_id":task.id,"_original_session_id":task.session_id}
                })

    async def _do_respond_approval(self, aid, response, approved):
        a = G_APPROVALS.get(aid)
        if not a or a.status != "pending": return None
        a.status = "approved" if approved else "rejected"
        a.boss_response = response; a.responded_at = _now()
        task = G_TASKS.get(a.task_id)
        if task:
            task.status = a.status; task.boss_approved = approved; task.updated_at = _now()
        await audit("approval_responded", "approval", f"{'通过' if approved else '拒绝'}: {aid}")
        return a

    # ==================== 主动发送 ====================
    def _enqueue_proactive(self, payload):
        self._route_enqueue(None, {
            "session_id": f"_sys_{payload.get('type','msg')}_{uuid.uuid4().hex[:8]}",
            "text": payload.get("text", ""),
            "metadata": {"_system_instruction":True,"_instruction_type":payload.get("type","send_wechat"),
                         "_target_wxid":payload.get("target_wxid",""),"_approval_id":payload.get("approval_id",""),
                         "_task_id":payload.get("task_id","")}
        })

    async def _ask_boss(self, approval, task):
        src = f"{'群['+task.session_name+']' if task.source_type=='group' else task.source_name}"
        text = (
            f"[系统指令] 用py4wx发微信 -> 收件人wxid:{self.boss_wxid}\n"
            f"内容:【待审批】{src}的任务：\n"
            f"{approval.question}\n"
            f"回复「同意/拒绝」+意见即可。"
        )
        self._enqueue_proactive({
            "type": "ask_boss",
            "target_wxid": self.boss_wxid,
            "text": text,
            "approval_id": approval.id,
            "task_id": task.id,
        })

    async def _report_to_boss(self, task):
        if not self.boss_report_enabled or not self.boss_wxid: return
        if task.status == "completed" and not self.boss_report_on_complete: return
        if task.status == "failed" and not self.boss_report_on_error: return
        src = f"{'群['+task.session_name+']' if task.source_type=='group' else task.source_name}"
        st = "已完成" if task.status == "completed" else "执行失败"
        text = (
            f"[系统指令] 用py4wx发微信 -> 收件人wxid:{self.boss_wxid}\n"
            f"内容:【任务{st}】{src}\n"
            f"内容:{task.content[:100]}\n"
            f"结果:{task.result or task.error or '无'}"
        )
        self._enqueue_proactive({
            "type": "report_boss",
            "target_wxid": self.boss_wxid,
            "text": text,
        })
        
    # ==================== 会话/任务/审批辅助 ====================
    def _get_session(self, sid, name="", stype="private"):
        if sid not in G_SESSIONS:
            G_SESSIONS[sid] = SessionContext(session_id=sid, display_name=name or sid, session_type=stype, last_message_time=_now())
        elif name:
            G_SESSIONS[sid].display_name = name
        return G_SESSIONS[sid]

    def _add_buffer(self, sid, msg):
        ctx = G_SESSIONS.get(sid)
        if not ctx: return
        ctx.message_buffer.append(msg)
        if len(ctx.message_buffer) > self.max_buffer_messages:
            ctx.message_buffer = ctx.message_buffer[-self.max_buffer_messages:]

    def _activate(self, sid, tid):
        ctx = G_SESSIONS.get(sid)
        if ctx: ctx.active_task_id = tid; ctx.last_trigger_time = _now()

    def _deactivate(self, sid):
        ctx = G_SESSIONS.get(sid)
        if ctx: ctx.active_task_id = None; ctx.last_trigger_time = None; ctx.message_buffer = []

    def _new_task(self, sid, sn, st, sn2, content, aid):
        t = Task(id=str(uuid.uuid4())[:12], session_id=sid, session_name=sn, source_type=st,
                 source_name=sn2, content=content, status="processing", agent_id=aid,
                 created_at=_now(), updated_at=_now())
        G_TASKS[t.id] = t; self._activate(sid, t.id); return t

    def _new_approval(self, tid, question, sinfo):
        a = ApprovalRequest(id=str(uuid.uuid4())[:12], task_id=tid, question=question,
                            session_info=sinfo, status="pending", created_at=_now())
        G_APPROVALS[a.id] = a
        task = G_TASKS.get(tid)
        if task: task.status="awaiting_approval"; task.approval_id=a.id; task.updated_at=_now()
        return a

    # ==================== 后台定时 ====================
    async def _idle_timeout_checker(self):
        while True:
            await asyncio.sleep(30)
            now = _now()
            for sid, ctx in list(G_SESSIONS.items()):
                if ctx.active_task_id and now - ctx.last_message_time > self.task_idle_timeout:
                    task = G_TASKS.get(ctx.active_task_id)
                    if task and task.status in ("processing","approved"):
                        task.status="completed"; task.result="[自动完成]空闲超时"; task.updated_at=_now()
                        self._deactivate(sid); await self._report_to_boss(task)

    async def _approval_expiry_checker(self):
        while True:
            await asyncio.sleep(60)
            now = _now()
            for a in list(G_APPROVALS.values()):
                if a.status=="pending" and now - a.created_at > self.boss_approval_timeout:
                    a.status = "expired"
                    task = G_TASKS.get(a.task_id)
                    if task and task.status=="awaiting_approval":
                        task.status="failed"; task.error="审批超时"; task.updated_at=_now()

    # ==================== WeFlow 监听 ====================
    async def _listen_weflow(self):
        print(f"[WeFlow] 连接 {self.weflow_api} ...")
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    async with c.stream("GET", f"{self.weflow_api}/api/v1/push/messages",
                                        params={"access_token":self.weflow_token}) as r:
                        if r.status_code != 200:
                            print(f"[WeFlow] 认证失败({r.status_code})，10s后重试"); await asyncio.sleep(10); continue
                        print("[WeFlow] ✅ 监听中...")
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "): continue
                            try:
                                d = json.loads(line[6:])
                                if d.get("event")=="message.new": await self._handle_wx(d)
                            except: pass
            except Exception as e:
                print(f"[WeFlow] 断开: {e}，5s后重连"); await asyncio.sleep(5)

    async def _handle_wx(self, data):
        mk = data.get("messageKey")
        if not mk or mk in processed_keys: return
        processed_keys.add(mk)
        if len(processed_keys) > 5000: processed_keys.clear()

        sid = data.get("sessionId", "unknown")
        name = data.get("sourceName", "未知")
        grp = data.get("groupName", "")
        content = data.get("content", "").strip()
        if not content: return

        stype = "group" if (grp or sid.endswith("@chatroom")) else "private"
        dname = grp or name
        ctx = self._get_session(sid, dname, stype)
        ctx.last_message_time = _now(); ctx.message_count += 1
        src = f"微信-{'群['+grp+']' if grp else '私聊'}-{name}"

        # ① 老板
        if self._is_boss_sender(data):
            print(f"\n[📥 老板 {src}]: {content}")
            await self._handle_boss_response(data, content); return

        # ② 黑名单 (扩展字段)
        bl = self._is_blacklisted(sid)
        if bl:
            print(f"\n[🚫 黑名单 {src}]: {content} → {bl.custom_reply}")
            self._enqueue_proactive({"type":"send_reply","text":bl.custom_reply,"target_wxid":sid})
            await audit("blacklist_blocked","permission",f"黑名单:{src}",sid); return

        # ③ 策略拒绝 (CoPaw 通用字段 dm_policy/group_policy + allow_from)
        if not self._is_allowed_by_policy(sid, stype):
            print(f"\n[🛡️ 策略拒绝 {src}]: {content} → {self.deny_message}")
            self._enqueue_proactive({"type":"send_reply","text":self.deny_message,"target_wxid":sid})
            await audit("policy_denied","permission",f"策略拒绝:{src}",sid); return

        # ④ 构建消息
        atts = []; txt = content
        if content in ["[图片]","[视频]","[语音]","[文件]","[表情]"]:
            media = await self._get_wx_media(sid)
            if media: atts.append(media); txt = f"收到一个{content}"
            else: txt = f"收到一个{content}，无法获取"
        msg_rec = {"session_id":sid,"source":src,"name":name,"group":grp,"text":txt,"attachments":atts,"time":_now()}

        # ⑤ 触发检查
        if not self._should_trigger(sid, content):
            self._add_buffer(sid, msg_rec)
            print(f"\n[📦 缓冲 {src}]: {content} ({len(ctx.message_buffer)}条)"); return

        # ⑥ 组装
        msgs = []
        if self.trigger_mode == "mention" and ctx.message_buffer and not ctx.active_task_id:
            msgs = list(ctx.message_buffer) + [msg_rec]
            ctx.message_buffer = []
            print(f"\n[⚡ 触发 {src}]: {content} → {len(msgs)}条缓冲")
        else:
            msgs = [msg_rec]
            print(f"\n[📥 {src}]: {content}")

        combined = "\n".join(f"[{m.get('source','')}]: {m['text']}" for m in msgs)
        all_att = [a for m in msgs for a in (m.get("attachments") or [])] or None

        # ⑦ 路由
        target = None
        if grp and grp in self.routing_rules: target = self.routing_rules[grp]
        elif sid in self.routing_rules: target = self.routing_rules[sid]

        # ⑧ 活跃任务追加
        if ctx.active_task_id:
            task = G_TASKS.get(ctx.active_task_id)
            if task and task.status in ("processing","approved"):
                task.messages_count += len(msgs); task.updated_at = _now()
                self._route_enqueue(target, {"session_id":f"weflow_{sid}","text":f"[续报] {combined}",
                    "attachments":all_att,"metadata":{"_task_id":task.id,"_task_continuation":True,"session_id":f"weflow_{sid}"}})
                asyncio.create_task(self._poll_reply(f"weflow_{sid}", 120, src))
                ctx.total_processed += len(msgs); return

        # ⑨ 新任务
        task = self._new_task(sid, dname, stype, name, combined, target or self.agent_id)
        ctx.total_processed += len(msgs)
        self._route_enqueue(target, {"session_id":f"weflow_{sid}","text":combined,"attachments":all_att,
            "metadata":{"_task_id":task.id,"session_id":f"weflow_{sid}"}})
        timeout = max(120, self.boss_approval_timeout + 120) if self.boss_approval_enabled else 120
        asyncio.create_task(self._poll_reply(f"weflow_{sid}", timeout, src))

    async def _get_wx_media(self, talker):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.weflow_api}/api/v1/messages",
                    params={"talker":talker,"limit":1,"media":"1"},
                    headers={"Authorization":f"Bearer {self.weflow_token}"})
                d = r.json()
                if d.get("success") and d.get("messages"):
                    m = d["messages"][0]; mt = m.get("mediaType","file")
                    url = m.get("mediaUrl"); lp = m.get("mediaLocalPath")
                    at = "image" if mt in ("image","emoji") else ("video" if mt=="video" else ("audio" if mt=="voice" else "file"))
                    fu = None
                    if url and "/api/v1/media/" in url:
                        fu = f"{self.weflow_api}/api/v1/media/{url.split('/api/v1/media/')[-1]}"
                    if not fu and lp: fu = lp.replace("\\","/"); fu = f"file:///{fu}" if not fu.startswith("/") else fu
                    if fu: return {"type":at,"url":fu}
        except Exception as e: print(f"[WeFlow] 媒体失败: {e}")
        return None

    async def _poll_reply(self, sid, timeout, src):
        bc = len(S.get(sid)); e = 0.0
        while e < timeout:
            await asyncio.sleep(0.5); e += 0.5
            msgs = S.get(sid); new = msgs[bc:] if len(msgs) > bc else []
            if new:
                if sid.startswith("_sys_") or sid.startswith("_continue_"):
                    if sid.startswith("_continue_"):
                        orig = sid.replace("_continue_","")
                        for m in new: S.add(orig, m)
                    S.clear(sid); return
                print(f"\n{'='*50}\n[✅ 回复 {src}]:\n{'='*50}")
                for msg in new:
                    mt = msg.get("msg_type","reply")
                    meta = msg.get("metadata") or {}
                    if mt == "approval_request":
                        tid = meta.get("task_id")
                        if tid and self.boss_approval_enabled and self.boss_wxid:
                            task = G_TASKS.get(tid)
                            if task:
                                appr = self._new_approval(tid, msg["text"], {"session_id":task.session_id})
                                await self._ask_boss(appr, task)
                                print(f"[🔐 审批] {tid}: {msg['text'][:80]}")
                            else: print(f"🤖: {msg['text']}")
                        else: print(f"🤖: {msg['text']}")
                    elif mt == "task_complete":
                        tid = meta.get("task_id")
                        if tid:
                            t = G_TASKS.get(tid)
                            if t: t.status="completed"; t.result=msg["text"]; t.updated_at=_now()
                            await self._report_to_boss(t); self._deactivate(t.session_id)
                        print(f"🤖: {msg['text']}")
                    elif mt == "task_failed":
                        tid = meta.get("task_id")
                        if tid:
                            t = G_TASKS.get(tid)
                            if t: t.status="failed"; t.error=msg["text"]; t.updated_at=_now()
                            await self._report_to_boss(t); self._deactivate(t.session_id)
                        print(f"🤖: {msg['text']}")
                    else: print(f"🤖: {msg['text']}")
                print(f"{'='*50}\n"); S.clear(sid); return

    # ==================== CoPaw 核心对接 ====================
    def get_to_handle_from_request(self, req):
        return req.channel_meta.get("session_id", req.user_id)

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
        req = self.build_agent_request_from_user_content(
            channel_id=self.channel, sender_id=uid, session_id=sid, content_parts=parts, channel_meta=m)
        req.channel_meta = m
        return req

    async def send(self, to, text, meta=None):
        meta = meta or {}
        msg = {"id":str(uuid.uuid4()),"session_id":to,"text":text,"created_at":time.time(),
               "msg_type":meta.get("msg_type","reply"),"metadata":meta}
        if to.startswith("_continue_"):
            orig = meta.get("_original_session_id","")
            if orig: msg["session_id"] = orig; S.add(orig, msg)
            return
        S.add(to, msg)


# ==================== FastAPI 路由注册 ====================
def register_app_routes(app):
    """
    注册管理后台和 API 路由到 CoPaw 的 FastAPI 实例。
    所有路由以 /api/ 开头，符合 CoPaw 规范。
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, HTMLResponse

    def ch():
        return LocalProxyChannel._main_instance

    # 管理后台页面
    @app.get("/api/local-proxy/admin")
    async def admin_page():
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copaw_admin.html")
        if os.path.exists(html_path):
            with open(html_path, 'r', encoding='utf-8') as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h2 style='color:#ccc;padding:40px;font-family:sans-serif'>"
            "管理后台文件未找到<br><br>请将 <code>copaw_admin.html</code> 放在<br>"
            "<code>custom_channels/local_proxy/</code> 目录下<br><br>"
            "安装路径: " + os.path.dirname(os.path.abspath(__file__)) + "</h2>")

    @app.get("/api/local-proxy/")
    async def status():
        return {"status":"running","agents":list(LocalProxyChannel._instances.keys()),"version":"enterprise"}

    # --- 配置 (读写 CoPaw agent.json) ---
    @app.get("/api/local-proxy/config")
    async def get_config():
        c = ch()
        return c.get_config_dict() if c else {"error":"not_initialized"}

    @app.put("/api/local-proxy/config")
    async def update_config(request: Request):
        c = ch()
        if not c: return JSONResponse({"error":"not_initialized"}, 503)
        data = await request.json()
        c.apply_config(data)
        # 通过 CoPaw 自身 API 持久化到 agent.json
        saved = False
        if HAS_HTTPX:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.put("http://127.0.0.1:8088/config/channels/local_proxy",
                                         json=c.get_config_dict())
                    saved = r.status_code == 200
            except: pass
        return {"status":"ok","saved_to_file":saved}

    # --- 统计 ---
    @app.get("/api/local-proxy/stats")
    async def get_stats():
        c = ch()
        if not c: return {"error":"not_initialized"}
        return {
            "total_messages": sum(s.message_count for s in G_SESSIONS.values()),
            "active_sessions": sum(1 for s in G_SESSIONS.values() if s.active_task_id),
            "total_sessions": len(G_SESSIONS),
            "pending_tasks": sum(1 for t in G_TASKS.values() if t.status in ("processing","awaiting_approval","approved")),
            "pending_approvals": sum(1 for a in G_APPROVALS.values() if a.status=="pending"),
            "completed_tasks": sum(1 for t in G_TASKS.values() if t.status=="completed"),
            "failed_tasks": sum(1 for t in G_TASKS.values() if t.status=="failed"),
            "blacklist_count": len(c.blacklist),
            "whitelist_count": len(c.allow_from),
            "trigger_mode": c.trigger_mode,
            "boss_configured": bool(c.boss_wxid),
            "agents": list(LocalProxyChannel._instances.keys()),
            "weflow_connected": LocalProxyChannel._weflow_started,
        }

    # --- 黑名单 ---
    @app.get("/api/local-proxy/blacklist")
    async def get_blacklist():
        c = ch()
        return {"items":[asdict(e) for e in c.blacklist]} if c else {"items":[]}

    @app.post("/api/local-proxy/blacklist")
    async def add_blacklist(request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        e = BlacklistEntry(session_id=data.get("session_id",""), display_name=data.get("display_name",""),
                           reason=data.get("reason",""), custom_reply=data.get("custom_reply","无权限"), added_at=_now())
        c.blacklist = [b for b in c.blacklist if b.session_id != e.session_id]
        c.blacklist.append(e)
        await audit("blacklist_add","permission",f"添加:{e.display_name}")
        return {"status":"ok","item":asdict(e)}

    @app.delete("/api/local-proxy/blacklist/{sid:path}")
    async def remove_blacklist(sid: str):
        c = ch()
        if not c: return {"status":"not_found"}
        before = len(c.blacklist)
        c.blacklist = [b for b in c.blacklist if b.session_id != sid]
        if len(c.blacklist) < before:
            await audit("blacklist_remove","permission",f"移除:{sid}")
            return {"status":"ok"}
        return {"status":"not_found"}

    # --- 任务 ---
    @app.get("/api/local-proxy/tasks")
    async def get_tasks(status: str = None, session_id: str = None, limit: int = 100, offset: int = 0):
        tasks = list(G_TASKS.values())
        if status: tasks = [t for t in tasks if t.status == status]
        if session_id: tasks = [t for t in tasks if t.session_id == session_id]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return {"items":[asdict(t) for t in tasks[offset:offset+limit]],"total":len(G_TASKS)}

    @app.get("/api/local-proxy/tasks/{tid}")
    async def get_task(tid: str):
        t = G_TASKS.get(tid)
        return asdict(t) if t else {"error":"not_found"}

    # --- 审批 ---
    @app.get("/api/local-proxy/approvals")
    async def get_approvals(status: str = None):
        items = list(G_APPROVALS.values())
        if status: items = [a for a in items if a.status == status]
        items.sort(key=lambda a: a.created_at, reverse=True)
        return {"items":[asdict(a) for a in items]}

    @app.post("/api/local-proxy/approvals/{aid}/respond")
    async def respond_approval(aid: str, request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        a = await c._do_respond_approval(aid, data.get("response",""), data.get("approved",False))
        if a and a.status == "approved":
            task = G_TASKS.get(a.task_id)
            if task:
                task.status = "processing"; task.updated_at = _now()
                c._route_enqueue(task.agent_id, {"session_id":f"_continue_{task.id}",
                    "text":f"[Web审批通过] 指示: {a.boss_response}\n继续执行原任务。",
                    "metadata":{"_continuation":True,"_original_task_id":task.id,"_original_session_id":task.session_id}})
        return {"status":"ok","approval":asdict(a)} if a else {"error":"not_found"}

    # --- 会话 ---
    @app.get("/api/local-proxy/sessions")
    async def get_sessions():
        sessions = [{"session_id":c.session_id,"display_name":c.display_name,"session_type":c.session_type,
                      "buffer_count":len(c.message_buffer),"active_task_id":c.active_task_id,
                      "last_message_time":c.last_message_time,"message_count":c.message_count,
                      "total_processed":c.total_processed} for c in G_SESSIONS.values()]
        sessions.sort(key=lambda s: s["last_message_time"], reverse=True)
        return {"items":sessions}

    @app.get("/api/local-proxy/sessions/{sid:path}/buffer")
    async def get_session_buffer(sid: str):
        ctx = G_SESSIONS.get(sid)
        if not ctx: return {"error":"not_found"}
        return {"session_id":sid,"display_name":ctx.display_name,"active_task_id":ctx.active_task_id,"buffer":ctx.message_buffer[-50:]}

    @app.post("/api/local-proxy/sessions/{sid:path}/deactivate")
    async def deactivate_session(sid: str):
        c = ch()
        if c: c._deactivate(sid)
        return {"status":"ok"}

    # --- 日志 ---
    @app.get("/api/local-proxy/audit")
    async def get_audit(category: str = None, limit: int = 200):
        items = G_AUDIT
        if category: items = [e for e in items if e.category == category]
        items = items[-limit:]; items.reverse()
        return {"items":[asdict(e) for e in items],"total":len(G_AUDIT)}

    # --- WeFlow 健康 ---
    @app.get("/api/local-proxy/weflow/health")
    async def weflow_health():
        c = ch()
        if not c or not HAS_HTTPX: return {"status":"unavailable"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{c.weflow_api}/health")
                return {"status":r.json().get("status","unknown"),"weflow_api":c.weflow_api}
        except Exception as e: return {"status":"error","error":str(e)}

    # --- 主动发送 ---
    @app.post("/api/local-proxy/proactive-send")
    async def proactive_send(request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        if not data.get("target_session_id") or not data.get("text"): return {"error":"missing_params"}
        c._enqueue_proactive({"type":"send_reply","text":data["text"],"target_wxid":data["target_session_id"]})
        return {"status":"queued"}

    # --- 兼容旧接口 ---
    @app.post("/api/local-proxy/send")
    async def compat_send(request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        aid = data.pop("agent_id", None)
        if not c._route_enqueue(aid, data): return {"error":"disabled"}
        sid = data.get("session_id") or "default"; bc = len(S.get(sid)); e = 0.0
        while e < (data.get("timeout") or 120):
            await asyncio.sleep(0.15); e += 0.15
            cm = S.get(sid)
            if len(cm) > bc: return {"status":"ok","messages":cm[bc:]}
        return {"status":"timeout","messages":[]}

    @app.post("/api/local-proxy/callback")
    async def compat_callback(request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        aid = data.pop("agent_id", None)
        if not c._route_enqueue(aid, data): return {"error":"disabled"}
        return {"status":"queued","session_id":data.get("session_id")}

    @app.get("/api/local-proxy/messages/{sid:path}")
    async def get_messages(sid: str): return {"messages": S.get(sid)}

    @app.delete("/api/local-proxy/messages/{sid:path}")
    async def clear_messages(sid: str): return {"cleared": S.clear(sid)}

    # --- 导出导入 ---
    @app.get("/api/local-proxy/config/export")
    async def export_config():
        c = ch()
        return c.get_config_dict() if c else {"error":"not_initialized"}

    @app.post("/api/local-proxy/config/import")
    async def import_config(request: Request):
        c = ch()
        if not c: return {"error":"not_initialized"}
        data = await request.json()
        c.apply_config(data)
        if HAS_HTTPX:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.put("http://127.0.0.1:8088/config/channels/local_proxy", json=c.get_config_dict())
            except: pass
        return {"status":"ok"}