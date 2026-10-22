"""用本地模型替身检查身份切换、聊天隔离及运行时上下文。"""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain.agents import create_agent as real_create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from evan_agent.auth import AuthStore
from evan_agent.main import main
from evan_agent.middleware import AuditWriter, build_middleware
from evan_agent.knowledge import KnowledgeStore


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class CliAuthTests(unittest.TestCase):
    def test_login_logout_clear_history_and_keep_password_out_of_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(Path(directory) / "auth.sqlite3")
            store.initialize("test-only-password-2026")
            calls = []

            def build_agent(**kwargs):
                agent = real_create_agent(**kwargs)

                class RecordingAgent:
                    def stream(self, value, *, context, config=None, **kwargs):
                        calls.append((value, context))
                        return agent.stream(value, context=context, config=config, **kwargs)

                    def invoke(self, value, *, context, config=None):
                        calls.append((value, context))
                        return agent.invoke(value, context=context, config=config)

                return RecordingAgent()

            model = ToolModel(responses=[AIMessage(content="本地模拟回复")])
            inputs = ["访客问题", "/login", "/profile add", "仅存本地的个人资料", "/end", "", "", "", "保存", "本人问题", "/whoami", "/logout", "新访客问题", "exit"]
            with (
                patch("evan_agent.main.load_dotenv"),
                patch.dict("os.environ", {"MINIMAX_API_KEY": "fake-local-only", "MINIMAX_BASE_URL": "https://example.invalid"}, clear=True),
                patch("evan_agent.main.AuthStore", return_value=store),
                patch("evan_agent.main.KnowledgeStore", return_value=KnowledgeStore(Path(directory) / "knowledge.sqlite3")),
                patch("evan_agent.main.read_password", return_value="test-only-password-2026"),
                patch("evan_agent.main.ChatAnthropic", return_value=model),
                patch("evan_agent.main.create_agent", side_effect=build_agent),
                patch("evan_agent.main.build_middleware", side_effect=lambda model, **kwargs: build_middleware(model, writer=AuditWriter(Path(directory) / "audit"), **kwargs)),
                patch("builtins.input", side_effect=inputs),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                main()
            self.assertEqual([ctx.role for _, ctx in calls], ["guest", "owner", "guest"])
            self.assertEqual(len({ctx.session_id for _, ctx in calls}), 3)
            self.assertEqual([len(value["messages"]) for value, _ in calls], [1, 1, 1])
            self.assertNotIn("test-only-password-2026", output.getvalue())
            for value, _ in calls:
                self.assertNotIn("test-only-password-2026", str(value))
                self.assertNotIn("/login", str(value))
                self.assertNotIn("仅存本地的个人资料", str(value))
                self.assertNotIn("/profile", str(value))


if __name__ == "__main__":
    unittest.main()
