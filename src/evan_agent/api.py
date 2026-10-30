"""本地 FastAPI 接口：Cookie 认证、SSE 聊天与跨请求人工审批。

默认监听 127.0.0.1:8001。当前会话和 checkpoint 使用 SQLite，仍为单进程，不能启用多个 worker。
"""

from dataclasses import asdict
from contextlib import asynccontextmanager
import json
import asyncio
import os
from threading import BoundedSemaphore
from typing import Literal

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from evan_agent.auth import AuthContext, AuthenticationError, require_owner
from evan_agent.http_auth import WebSessions, RequestLimits
from evan_agent.service import AgentService
from evan_agent.sessions import SessionBusyError

PREFIX = '/agent-api'
COOKIE = 'evan_agent_session'


class LoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: SecretStr = Field(min_length=1, max_length=256)


class MessageBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=12000)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['approve', 'edit', 'reject']
    changes: dict[str, str] = Field(default_factory=dict, max_length=4)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decisions: list[ReviewDecision] = Field(min_length=1, max_length=8)


def create_app(service=None):
    load_dotenv('.env')
    managed_service = service is None
    service = service if service is not None else AgentService()
    sessions = WebSessions(service.sessions)
    limits = RequestLimits()
    capacity = BoundedSemaphore(2)
    origins = [v.strip() for v in os.getenv('AGENT_ALLOWED_ORIGINS',
        'http://127.0.0.1:8081,http://localhost:8081,http://127.0.0.1:8001,http://localhost:8001').split(',') if v.strip()]
    if '*' in origins:
        raise ValueError('凭据接口不能使用通配 CORS 来源。')
    secure = os.getenv('AGENT_COOKIE_SECURE', 'false').lower() == 'true'
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if managed_service:
                service.sessions.close()

    app = FastAPI(lifespan=lifespan, title='Evan Agent API', version='0.1.0', docs_url=PREFIX + '/docs', openapi_url=PREFIX + '/openapi.json')
    app.state.service = service
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=['GET', 'POST', 'PATCH', 'DELETE'], allow_headers=['Content-Type', 'X-Agent-Request'])

    @app.middleware('http')
    async def request_boundary(request, call_next):
        # 自定义请求头阻止跨站表单写入，Origin 白名单阻止恶意网页携带 Cookie。
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('X-Agent-Request') != '1':
                return JSONResponse({'detail': '缺少 X-Agent-Request: 1。'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin not in origins:
                return JSONResponse({'detail': '请求来源不允许。'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(PermissionError)
    async def permission_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # 默认校验响应可能包含原始输入；认证接口不能回显密码。
        return JSONResponse({'detail': '请求参数不符合接口格式。',
                             'fields': [list(item['loc']) for item in error.errors()]}, status_code=422)

    @app.exception_handler(SessionBusyError)
    async def conflict(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    def identity(request):
        try:
            return sessions.require(request.cookies.get(COOKIE))
        except PermissionError as error:
            raise HTTPException(401, str(error)) from None

    def throttle(request, kind, limit):
        peer = request.client.host if request.client else 'unknown'
        if not limits.check((kind, peer), limit):
            raise HTTPException(429, '请求过于频繁，请稍后重试。', headers={'Retry-After': '60'})

    def issue(response, user):
        token, age = sessions.issue(user)
        response.set_cookie(COOKIE, token, max_age=age, httponly=True, secure=secure,
                            samesite='lax', path=PREFIX)
        return {'role': user.role, 'expires_in': age}

    @app.get(PREFIX + '/health')
    def health():
        return {'status': 'ok'}

    @app.post(PREFIX + '/auth/guest')
    def guest(request: Request, response: Response):
        throttle(request, 'guest', 20)
        sessions.revoke(request.cookies.get(COOKIE))
        return issue(response, AuthContext.guest())

    @app.post(PREFIX + '/auth/login')
    def login(body: LoginBody, request: Request, response: Response):
        throttle(request, 'login', 10)
        try:
            user = service.auth.login(body.password.get_secret_value())
        except AuthenticationError as error:
            raise HTTPException(401, str(error)) from None
        sessions.revoke(request.cookies.get(COOKIE))
        return issue(response, user)

    @app.get(PREFIX + '/auth/me')
    def me(request: Request):
        user = identity(request)
        return {'role': user.role, 'user_id': user.user_id}

    @app.post(PREFIX + '/auth/logout')
    def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path=PREFIX, secure=secure, httponly=True, samesite='lax')
        return {'status': 'logged_out'}

    @app.post(PREFIX + '/conversations')
    def create_conversation(request: Request):
        user = identity(request)
        if len(service.sessions.list_for(user)) >= 20:
            raise HTTPException(429, '每个登录会话最多创建 20 段聊天。')
        return {'conversation_id': service.sessions.create(user)}

    @app.get(PREFIX + '/conversations')
    def conversations(request: Request):
        return {'items': [asdict(item) for item in service.sessions.list_for(identity(request))]}

    @app.get(PREFIX + '/conversations/{conversation_id}')
    def conversation(conversation_id: str, request: Request):
        user = identity(request)
        history = service.sessions.history(user, conversation_id)
        # 不把工具结果、thinking 或内部参数通过历史接口直接暴露。
        return {'conversation_id': conversation_id,
                'messages': [{'role': 'user' if m.type == 'human' else 'assistant', 'content': m.text}
                             for m in history if m.type in ('human', 'ai') and m.text],
                'pending': service.sessions.pending_for(user, conversation_id),
                **service.sessions.operation_for(user, conversation_id)}

    @app.delete(PREFIX + '/conversations/{conversation_id}/messages')
    def clear(conversation_id: str, request: Request):
        service.sessions.clear(identity(request), conversation_id)
        return {'status': 'cleared'}

    def stream(request, user, conversation_id, **kwargs):
        # 先验证归属，尽量在发送 SSE 响应头之前返回 HTTP 错误。
        service.sessions.history(user, conversation_id)
        token = request.cookies.get(COOKIE)
        throttle(request, 'chat', 30)
        agent = service.agent
        def encode(event):
            return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        async def generate():
            if not capacity.acquire(blocking=False):
                yield encode({'type': 'error', 'code': 'busy', 'message': '当前请求较多，请稍后再试。'})
                return
            iterator = service.sessions.events(agent, user, conversation_id, **kwargs)
            advancing = None
            sentinel = object()
            def advance():
                sessions.require(token)
                return next(iterator, sentinel)
            try:
                while not await request.is_disconnected():
                    advancing = asyncio.create_task(anyio.to_thread.run_sync(advance))
                    while not advancing.done():
                        done, _ = await asyncio.wait({advancing}, timeout=15)
                        if not done:
                            sessions.require(token)
                            if await request.is_disconnected():
                                return
                            # SSE 注释保活，不会被前端当成模型文本。
                            yield ': heartbeat\n\n'
                    event = advancing.result()
                    advancing = None
                    if event is sentinel:
                        break
                    sessions.require(token)
                    yield encode(event)
            except Exception as error:
                code = 'unauthorized' if isinstance(error, PermissionError) else 'conflict' if isinstance(error, SessionBusyError) else 'agent_error'
                # 不把服务端堆栈、模型异常原文或密钥发送到浏览器。
                yield encode({'type': 'error', 'code': code, 'message': '请求未完成，请检查身份或待审批事项后重试。'})
            finally:
                # 断流时关闭生成器，释放 busy 标记与非审批 checkpoint。
                with anyio.CancelScope(shield=True):
                    if advancing is not None:
                        try:
                            await advancing
                        except Exception:
                            pass
                    await anyio.to_thread.run_sync(iterator.close)
                capacity.release()
        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})

    @app.post(PREFIX + '/conversations/{conversation_id}/messages')
    def message(conversation_id: str, body: MessageBody, request: Request):
        user = identity(request)
        if not body.message.strip():
            raise HTTPException(422, '消息不能为空。')
        if service.sessions.pending_for(user, conversation_id):
            raise HTTPException(409, '请先确认或取消待审批事项。')
        return stream(request, user, conversation_id, prompt=body.message)

    @app.post(PREFIX + '/conversations/{conversation_id}/approvals/{approval_id}')
    def approve(conversation_id: str, approval_id: str, body: ReviewBody, request: Request):
        user = identity(request)
        decision = service.prepare_review(user, conversation_id, approval_id,
                                          [item.model_dump() for item in body.decisions])
        return stream(request, user, conversation_id, approval_id=approval_id, decision=decision)

    @app.patch(PREFIX + '/conversations/{conversation_id}/approvals/{approval_id}/draft')
    def review_draft(conversation_id: str, approval_id: str, body: ReviewBody, request: Request):
        user = identity(request)
        decisions = [item.model_dump() for item in body.decisions]
        service.prepare_review(user, conversation_id, approval_id, decisions)
        service.sessions.save_review_draft(user, conversation_id, approval_id, decisions)
        return {'status': 'draft_saved'}

    @app.delete(PREFIX + '/conversations/{conversation_id}/approvals/{approval_id}')
    def cancel(conversation_id: str, approval_id: str, request: Request):
        user = identity(request)
        require_owner(user)
        service.sessions.cancel_pending(user, conversation_id, approval_id)
        return {'status': 'cancelled'}

    return app


def main():
    import uvicorn
    uvicorn.run('evan_agent.api:create_app', factory=True, host='127.0.0.1', port=8001, workers=1)
