"""Regression checks for the Abaqus bridge protocol."""

import ast
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "abaqus_mcp_bridge" / "gui_plugin.py"


def load_plugin_functions(*names):
    source = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    functions = [
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    scope = {"base64": base64, "sys": sys}
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, str(PLUGIN_PATH), "exec"), scope)
    return scope


class KernelResultLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.wrapper = load_plugin_functions(
            "_as_utf8_bytes",
            "_kernel_wrapper",
        )["_kernel_wrapper"]
        self.kernel_scope = {"mdb": object(), "session": object()}

    def execute(self, code):
        with tempfile.TemporaryDirectory() as folder:
            response_path = Path(folder) / "response.json"
            exec(self.wrapper(code, str(response_path)), self.kernel_scope)
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            return payload["result"]

    def test_result_is_cleared_between_calls(self):
        first = self.execute("result = 123")
        second = self.execute("value = 456")

        self.assertEqual(first["return_value"], 123)
        self.assertIsNone(second["return_value"])
        self.assertNotIn("result", self.kernel_scope["_ABAQUS_MCP_GLOBALS"])


if __name__ == "__main__":
    unittest.main()
