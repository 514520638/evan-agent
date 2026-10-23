"""Agent 服务装配与接口审批校验，无终端 input/print 依赖。"""

import os
from copy import deepcopy
from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic

from evan_agent.auth import AuthContext, AuthStore, require_owner
from evan_agent.knowledge import KnowledgeStore
from evan_agent.knowledge_tools import build_knowledge_tools
from evan_agent.middleware import build_middleware
from evan_agent.sessions import SessionStore, SessionBusyError


def build_agent(knowledge, sessions):
    """终端与 HTTP 共用模型、工具、中间件和 checkpoint 装配。"""
    key = os.getenv('MINIMAX_API_KEY', '').strip()
    if not key or key == 'your-minimax-key-here':
        raise ValueError('请配置 MINIMAX_API_KEY。')
    name = (os.getenv('MODEL') or 'MiniMax-M3.1-Flash').strip()
    model = ChatAnthropic(model=name, api_key=key,
                          base_url=(os.getenv('MINIMAX_BASE_URL') or 'https://api.minimax.cn/anthropic').strip(),
                          max_tokens=8192, timeout=60, max_retries=1)
    return create_agent(model=model, tools=build_knowledge_tools(knowledge),
                        middleware=build_middleware(model, lifecycle_logs=False),
                        context_schema=AuthContext, checkpointer=sessions.checkpointer)


class AgentService:
    def __init__(self, auth=None, knowledge=None, sessions=None, agent_factory=build_agent):
        self.auth = auth if auth is not None else AuthStore()
        self.knowledge = knowledge if knowledge is not None else KnowledgeStore()
        self.sessions = sessions if sessions is not None else SessionStore()
        self._factory = agent_factory
        self._agent = None
        from threading import Lock
        self._agent_lock = Lock()

    @property
    def agent(self):
        # 认证和资料接口不依赖模型 Key；第一次聊天才创建适配器。
        with self._agent_lock:
            if self._agent is None:
                self._agent = self._factory(self.knowledge, self.sessions)
            return self._agent

    def prepare_review(self, identity, conversation_id, approval_id, decisions):
        """只允许编辑原工具的白名单参数；不能借审批改工具名、记录归属或 ID。"""
        require_owner(identity)
        pending = self.sessions.pending_for(identity, conversation_id)
        if not pending or pending['approval_id'] != approval_id:
            raise SessionBusyError('审批已失效或已处理。')
        actions = pending['action_requests']
        if len(actions) != len(decisions):
            raise ValueError('审批数量必须与待确认事项一致。')
        result = []
        for action, decision in zip(actions, decisions):
            kind = decision['type']
            if kind == 'reject':
                result.append({'type': 'reject', 'message': '本人拒绝本次写入，不要重复提出相同写入。'})
                continue
            if kind not in ('approve', 'edit'):
                raise ValueError('不支持的审批类型。')
            args = deepcopy(action['args'])
            changes = decision.get('changes') or {}
            allowed = {'content', 'visibility', 'status'}
            if action['name'] == 'save_personal_memory':
                allowed.add('category')
            elif action['name'] != 'update_personal_memory':
                raise ValueError('未知写入工具。')
            if changes.keys() - allowed or (kind == 'approve' and changes):
                raise ValueError('审批包含不允许修改的字段。')
            if any(not isinstance(value, str) or len(value) > 20000 for value in changes.values()):
                raise ValueError('审批字段必须为不超过 20000 字符的文本。')
            self.knowledge._validate(changes)
            args.update(changes)
            self.knowledge._validate({key: value for key, value in args.items() if key in allowed and value is not None})
            result.append({'type': 'approve'} if kind == 'approve' else
                          {'type': 'edit', 'edited_action': {'name': action['name'], 'args': args}})
        return {'decisions': result}
