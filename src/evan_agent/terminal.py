"""统一终端文本输入，正确处理中文宽字符、自动折行和删除后的重绘。"""

import sys

from prompt_toolkit import prompt
from prompt_toolkit.history import DummyHistory


def read_line(message: str) -> str:
    """交互终端使用宽字符编辑器，管道输入保留标准 input 行为。

    不共享或落盘输入历史，避免登录切换后通过方向键看到其他身份的内容。
    密码仍由 auth.read_password 隐藏读取，不经过此普通文本入口。
    Ctrl+C / Ctrl+D 异常交给现有聊天及审批入口处理。
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(message)
    return prompt(message, history=DummyHistory(), multiline=False, wrap_lines=True)


class StreamingPrinter:
    """即时输出模型文本片段；不使用 sleep 人为模拟打字速度。"""

    def __init__(self):
        self.has_text = False
        self.last_text = ""
        self._line_open = False

    def new_message(self):
        self.finish()
        self.last_text = ""

    def write(self, text: str):
        if not self._line_open:
            print("Agent：", end="", flush=True)
            self._line_open = True
        print(text, end="", flush=True)
        self.last_text += text
        self.has_text = True

    def finish(self):
        if self._line_open:
            print(flush=True)
            self._line_open = False
