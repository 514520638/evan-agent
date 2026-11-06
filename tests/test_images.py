"""远端图片响应与文件落盘边界。"""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
import base64
import unittest
from PIL import Image
from evan_agent.images import ImageStore, build_image_tools
from evan_agent.sessions import SessionStore
from evan_agent.auth import AuthContext


class ImageTests(unittest.TestCase):
    def test_generation_stores_private_file_and_analysis_uses_image_block(self):
        with TemporaryDirectory() as root:
            sessions = SessionStore()
            user = AuthContext.guest()
            conversation = sessions.create(user)
            store = ImageStore(Path(root), sessions)
            runtime = SimpleNamespace(context=replace(user, conversation_id=conversation))
            image = BytesIO()
            Image.new('RGB', (100,100), 'blue').save(image, format='PNG')
            tools = build_image_tools(store)
            with patch('evan_agent.images.provider_request', return_value={'data': {'image_base64': [base64.b64encode(image.getvalue()).decode()]}}):
                result = tools[1].func('画蓝色方块', runtime)
            path, mime = store.get(user, conversation, result['id'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(mime, 'image/png')
            with patch('evan_agent.images.provider_request', return_value={'content': [{'type':'text','text':'蓝色方块'}]}) as request:
                self.assertEqual(tools[0].func(result['id'], '什么颜色', runtime)['analysis'], '蓝色方块')
                blocks = request.call_args.args[1]['messages'][0]['content']
                self.assertEqual(blocks[0]['type'], 'image')
            sessions.delete(user, conversation)
            store.cleanup()
            self.assertFalse(path.exists())
            sessions.close()
