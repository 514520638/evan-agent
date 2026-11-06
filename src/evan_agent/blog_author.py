"""博客写作 skill 与 Java 博客 API 适配。管理员凭据只在服务端使用。"""
from functools import lru_cache
from contextvars import ContextVar
from hashlib import sha256
import os
from pathlib import Path
import re
from threading import Lock

import httpx
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import HumanMessage

from evan_agent.auth import AuthContext, require_owner

SITE = 'https://evanlink.cn'
BLOG_COOKIE = 'evanlink_admin_token'
# 只在当前 HTTP 请求的工具执行上下文有效，不进入提示词、消息或 checkpoint。
blog_session: ContextVar[str | None] = ContextVar('blog_session', default=None)
SEPARATOR = '\n\n<!-- evanlink:en -->\n\n'
PUBLISH = re.compile(r'^\s*(?:发布博客\s*[:：]|/publish\s+)(?=\s*\S)', re.I)
publishing = Lock()


def wants_publication(text: str) -> bool:
    """识别本轮明确发布请求。否定、暂缓和询问操作方法均不执行写入。"""
    text = text.strip()
    if re.search(r'(?:不要|不用|暂不|别|不必|先不|不允许|取消|禁止).{0,8}(?:发布|放到|发到|上传|写入)|只.{0,4}(?:草稿|预览)|确认后|先确认', text):
        return False
    if PUBLISH.match(text):
        return True
    # 明确以发布动作开头时，后面的主题说明不应影响授权判断。
    if re.match(r'^(?:请帮我|请给我|帮我|给我|请)?\s*发布(?:一篇|一份|这篇|这份)?(?:博客|文章)', text):
        return True
    # “写一篇……放到博客文章里面”也是写入请求。要求有创作/指代动作和明确博客目标。
    direct = re.match(r'^(?:请帮我|请给我|帮我|给我|请)?\s*(?:写|生成|整理|把|将)', text)
    destination = re.search(r'(?:发布到|放到|发到|上传到|写入|保存到)(?:我的)?博客(?:文章)?(?:库|里|里面|中)?[。！!\s]*$', text)
    if direct and destination:
        return True
    return bool(re.fullmatch(
        r'(?:请|帮我|给我|请帮我|请给我)?(?:写|生成|整理).+?(?:并|然后|直接|自动)'
        r'(?:自动|直接)?发布(?:到(?:我的)?博客)?[。！!\s]*', text, re.S))


def user_turn(messages):
    return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)


@lru_cache(maxsize=1)
def instructions():
    return (Path(__file__).parent / 'skills/blog-author/SKILL.md').read_text(encoding='utf-8')


@tool
def blog_taxonomy(runtime: ToolRuntime[AuthContext]) -> dict:
    """获取发布文章可选的分类与标签名称和 ID。"""
    require_owner(runtime.context)
    try:
        response = httpx.get(SITE + '/api/articles/filters', timeout=12)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return {'error': '无法获取博客分类和标签，请稍后再试。'}


@tool
def publish_blog(title: str, title_en: str, excerpt: str, excerpt_en: str,
                 markdown: str, markdown_en: str, category_id: int, tag_ids: list[int],
                 runtime: ToolRuntime[AuthContext]) -> dict:
    """发布一篇双语博客，只允许主人在本轮使用“发布博客：主题”或“/publish 主题”。"""
    require_owner(runtime.context)
    turn = user_turn(runtime.state['messages'])
    if not turn or not turn.id or not wants_publication(turn.text):
        return {'error': '请在本人会话中使用“发布博客：主题”。'}
    if not all(s.strip() for s in (title, title_en, excerpt, excerpt_en, markdown, markdown_en)) or not tag_ids:
        return {'error': '需要完整双语标题、摘要、正文和至少一个标签。'}
    if max(len(title), len(title_en)) > 255 or max(len(excerpt), len(excerpt_en)) > 500:
        return {'error': '标题限 255 字，摘要限 500 字。'}
    if SEPARATOR.strip() in markdown or SEPARATOR.strip() in markdown_en:
        return {'error': '双语分隔符由程序添加，请从正文中移除。'}
    username, password = os.getenv('BLOG_ADMIN_USERNAME'), os.getenv('BLOG_ADMIN_PASSWORD')
    cookie = blog_session.get()
    if not cookie and not (username and password):
        return {'error': '请在同一域名和浏览器登录博客管理页 /admin，再回到聊天发送发布指令。草稿可继续使用。'}
    # 同一用户消息固定一个 slug，断线或工具重试时先检查已有文章。
    slug = 'evan-note-' + sha256(turn.id.encode()).hexdigest()[:24]
    url = SITE + '/blog/' + slug
    payload = dict(slug=slug, title=title, titleEn=title_en, excerpt=excerpt, excerptEn=excerpt_en,
                   content=markdown.strip() + SEPARATOR + markdown_en.strip(), categoryId=category_id,
                   tagIds=sorted(set(tag_ids)), author='Evan', status='PUBLISHED', featured=False)
    try:
        with publishing, httpx.Client(base_url=SITE, timeout=20) as client:
            existing = client.get('/api/articles/' + slug)
            if existing.status_code == 200:
                article = existing.json()
                if article.get('slug') == slug and article.get('status') == 'PUBLISHED':
                    return {'status': 'already_published', 'title': article['title'], 'url': url}
                return {'error': '同名文章状态需要本人核对。'}
            if existing.status_code != 404:
                existing.raise_for_status()
                return {'error': '无法核对文章状态。'}
            filters = client.get('/api/articles/filters')
            filters.raise_for_status()
            options = filters.json()
            if category_id not in {c['id'] for c in options['categories']} or not set(tag_ids) <= {t['id'] for t in options['tags']}:
                return {'error': '分类或标签已变更，请重新查询。'}
            if cookie:
                client.cookies.set(BLOG_COOKIE, cookie, domain='evanlink.cn', path='/')
            else:
                login = client.post('/api/admin/login', json={'username': username, 'password': password})
                login.raise_for_status()
            require_owner(runtime.context)
            result = client.post('/api/articles', json=payload)
            if result.status_code in (401, 403):
                return {'error': '博客管理员登录已失效或无发布权限，请重新登录 /admin 后再发布。'}
            result.raise_for_status()
            article = result.json()
            if article.get('slug') != slug or article.get('status') != 'PUBLISHED':
                return {'error': '发布响应与预期不同，请到博客管理页核对。'}
            return {'status': 'published', 'title': article['title'], 'url': url}
    except (httpx.HTTPError, ValueError, KeyError):
        return {'error': '未能确认发布成功，请核对博客管理页。', 'check_url': url}
