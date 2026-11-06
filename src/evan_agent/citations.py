"""展示层过滤内部资料标记，保留存储中的原始回答供评估。"""
import re

# 兼容半角、全角冒号与方括号。只识别带资料前缀的标识符。
_MARK = re.compile(r'[\[【]资料[ \t]*[:：][ \t]*[\w-]+[ \t]*[\]】]')
_PARTIAL = re.compile(r'[\[【](?:资(?:料(?:[ \t]*(?:[:：][ \t]*[\w-]*[ \t]*)?)?)?)?\Z')


def display_text(text: str) -> str:
    return _MARK.sub('', text)


class CitationStream:
    """仅暂存可能属于引用的末尾片段，普通文本立即输出。"""
    def __init__(self):
        self.pending = ''

    def feed(self, chunk: str) -> str:
        text = display_text(self.pending + chunk)
        partial = _PARTIAL.search(text)
        self.pending = partial.group() if partial else ''
        return text[:partial.start()] if partial else text

    def finish(self) -> str:
        text, self.pending = self.pending, ''
        return text
