"""离线检查中文输入编辑，不连接模型。"""

import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from evan_agent.terminal import read_line


class TerminalTests(unittest.TestCase):
    def edit(self, keys):
        with create_pipe_input() as pipe:
            with (create_app_session(input=pipe, output=DummyOutput()),
                  patch('evan_agent.terminal.sys.stdin.isatty', return_value=True),
                  patch('evan_agent.terminal.sys.stdout.isatty', return_value=True)):
                pipe.send_text(keys)
                return read_line('你：')

    def test_long_chinese_backspace_to_empty(self):
        text = '我在北京工作，正在学习编程。' * 30
        self.assertEqual(self.edit(text + '\x7f' * len(text) + '\r'), '')

    def test_mixed_text_delete_and_replace(self):
        self.assertEqual(self.edit('北京Python开发' + '\x7f' * 2 + '学习\r'), '北京Python学习')

    def test_control_u_clears_line(self):
        self.assertEqual(self.edit('很长的中文内容' * 30 + '\x15' + '新内容\r'), '新内容')

    def test_non_terminal_falls_back(self):
        with patch('evan_agent.terminal.sys.stdin.isatty', return_value=False), patch('builtins.input', return_value='管道内容'):
            self.assertEqual(read_line('你：'), '管道内容')


if __name__ == '__main__':
    unittest.main()
