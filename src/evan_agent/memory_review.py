"""长期记忆人工审批：本地编辑工具参数，确认后恢复被暂停的 Agent。"""

from copy import deepcopy
import sqlite3

from evan_agent.auth import require_owner
from evan_agent.profile_commands import ask, choose, Cancelled
from evan_agent.knowledge import EntryNotFoundError


class ReviewCancelled(Exception):
    """终止当前尚未确认的审批批次，不恢复其中的写入工具。"""


class MemoryReviewer:
    """把 HITL 的 action_requests 转为按顺序排列的 edit 决策。"""

    def __init__(self, store):
        self.store = store

    def __call__(self, payload, identity):
        require_owner(identity)
        decisions = []
        try:
            for action in payload['action_requests']:
                name = action['name']
                if name not in {'save_personal_memory', 'update_personal_memory'}:
                    raise ReviewCancelled('未知审批操作，已停止本轮。')
                args = deepcopy(action['args'])
                original = None
                if name == 'update_personal_memory':
                    original = self.store.get(identity, args['entry_id'])
                    print(f"\n[长期记忆审批] 更新 {original.id}\n原文：{original.content}")
                else:
                    print('\n[长期记忆审批] 新增资料（尚未写入）')
                print(f"拟保存内容：{args['content']}")
                replacement = ask('修改为其他完整内容（回车保留，/cancel 取消）：')
                if replacement.strip():
                    args['content'] = replacement
                if any(word in args['content'] for word in ('工作', '任职', '职位', '公司')):
                    print('可选补充以下工作信息，逐项回车跳过；薪酬、待遇无需填写。')
                    labels = ('公司', '职位', '行业', '薪酬', '待遇')
                else:
                    labels = ('其他补充',)
                for label in labels:
                    extra = ask(f'{label}（可跳过）：').strip()
                    if extra:
                        args['content'] += f'\n{label}：{extra}'
                # 新增采用公开、已发布默认值；明确指定私有或草稿时保留提案，最终仍由本人确认。
                args['visibility'] = choose('公开范围（private 仅本人 / public 访客可见）',
                    original.visibility if original else args.get('visibility', 'public'),
                    {'private': 'private', '仅本人': 'private', 'public': 'public', '公开': 'public'})
                args['status'] = choose('发布状态（draft 草稿 / published 用于问答 / archived 归档）',
                    original.status if original else args.get('status', 'published'),
                    {'draft': 'draft', '草稿': 'draft', 'published': 'published', '已发布': 'published', 'archived': 'archived', '归档': 'archived'})
                require_owner(identity)
                print(f"\n最终内容：\n{args['content']}\n公开范围：{args['visibility']}；状态：{args['status']}")
                if args['visibility'] == 'public' and args['status'] == 'published':
                    print('本条全部内容（包括所填薪酬和待遇）将可被访客查询。')
                if ask('输入 确认写入 继续；其他输入取消本批次：').strip() != '确认写入':
                    raise ReviewCancelled('已取消本批次，未执行其中的写入。')
                require_owner(identity)
                decisions.append({'type': 'edit', 'edited_action': {'name': name, 'args': args}})
        except (Cancelled, EOFError, KeyboardInterrupt):
            raise ReviewCancelled('已取消本批次，未执行其中的写入。') from None
        except (KeyError, TypeError, ValueError, EntryNotFoundError, sqlite3.Error):
            raise ReviewCancelled('待审批资料已不可用或参数不完整，本批次未执行，请重新提出保存请求。') from None
        return {'decisions': decisions}
