import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "fake_webhook_server", ROOT / "tools" / "fake_webhook_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeWebhookServerTests(unittest.TestCase):
    def test_selftest_passes(self):
        """本地假服务器是给使用者做联调的工具，必须保持可用。"""

        tool = load_tool()
        with redirect_stdout(io.StringIO()):
            exit_code = tool.run_selftest()
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
