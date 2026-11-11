"""后台维护：过期访客、旧模型向量及半年以前的审计日志。"""

from datetime import datetime, timedelta, timezone
import logging
import re
from threading import Event, Thread

from evan_agent.retrieval import Retriever
from evan_agent.images import ImageStore

logger = logging.getLogger(__name__)


def prune_audit(directory, today=None):
    """半年按 183 天计算，按 UTC 文件日期清理，仅删除规范命名的普通日志文件。"""
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=183)
    removed = 0
    if not directory.exists():
        return removed
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.jsonl', path.name):
            continue
        try:
            day = datetime.strptime(path.stem, '%Y-%m-%d').date()
        except ValueError:
            continue
        if day < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


class Maintenance:
    """启动立即巡检，之后每分钟一次；停止时等待本次清理完成再关闭数据库。"""
    def __init__(self, sessions, knowledge, audit_directory, interval=60):
        self.sessions = sessions
        self.knowledge = knowledge
        self.audit_directory = audit_directory
        self.interval = interval
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name='agent-maintenance', daemon=True)

    def sweep(self):
        # 独立捕获错误，日志删除失败不能阻止会话到期清理。
        tasks = [('sessions', self.sessions.cleanup_expired),
                 ('images', lambda: ImageStore(self.knowledge.path.parent, self.sessions.conversations).cleanup()),
                 ('vectors', lambda: Retriever(self.knowledge)),
                 ('audit', lambda: prune_audit(self.audit_directory))]
        for name, task in tasks:
            try:
                task()
            except Exception as error:
                logger.error('Maintenance %s failed (%s)', name, type(error).__name__)

    def run(self):
        while not self.stop_event.is_set():
            self.sweep()
            self.stop_event.wait(self.interval)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join()
