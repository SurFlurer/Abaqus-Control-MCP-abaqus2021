"""Regression checks for the Abaqus bridge protocol."""

import ast
import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from abaqus_mcp_bridge import client as client_module
from abaqus_mcp_bridge.client import AbaqusBridgeClient, BridgeError


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "abaqus_mcp_bridge" / "gui_plugin.py"


def load_plugin_symbols(*names):
    source = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    symbols = [
        node
        for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    scope = {"base64": base64, "sys": sys, "threading": threading}
    module = ast.Module(body=symbols, type_ignores=[])
    exec(compile(module, str(PLUGIN_PATH), "exec"), scope)
    return scope


class KernelResultLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.wrapper = load_plugin_symbols(
            "_as_utf8_bytes",
            "_kernel_wrapper",
        )["_kernel_wrapper"]
        self.kernel_scope = {"mdb": object(), "session": object()}

    def execute(self, code, execution_id="execution-1"):
        with tempfile.TemporaryDirectory() as folder:
            response_path = Path(folder) / "response.json"
            exec(
                self.wrapper(code, str(response_path), execution_id),
                self.kernel_scope,
            )
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            return payload["result"]

    def test_result_is_cleared_between_calls(self):
        first = self.execute("result = 123")
        second = self.execute("value = 456")

        self.assertEqual(first["return_value"], 123)
        self.assertIsNone(second["return_value"])
        self.assertEqual(first["executionId"], "execution-1")
        self.assertEqual(second["executionId"], "execution-1")
        self.assertNotIn("result", self.kernel_scope["_ABAQUS_MCP_GLOBALS"])

    def test_deep_error_reports_compact_user_frame(self):
        error = self.execute(
            "def broken():\n"
            "    raise ValueError('bad')\n"
            "broken()",
            "deep-error",
        )

        self.assertFalse(error["ok"])
        self.assertEqual(error["error_line"], 2)
        self.assertEqual(error["primary_frame"]["function"], "broken")
        self.assertIn("raise ValueError('bad')", error["code_excerpt"])
        self.assertNotIn("traceback", error)

    def test_function_defined_in_prior_call_uses_its_source(self):
        self.execute(
            "def build():\n    raise ValueError('inside build')",
            "definition",
        )
        failure = self.execute("build()", "invocation")

        self.assertEqual(
            failure["primary_frame"],
            {"file": "<abaqus-mcp:definition>", "line": 2, "function": "build"},
        )
        self.assertIn("raise ValueError('inside build')", failure["code_excerpt"])

    def test_external_script_reports_actual_file_and_key_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "build_model.py"
            script.write_text(
                "def build():\n"
                "    parts = {'ConcreteCylinder': 1}\n"
                "    return parts['ConcreteCylindr']\n"
                "build()\n",
                encoding="utf-8",
            )
            code = (
                "exec(compile(open(%r, encoding='utf-8').read(), %r, 'exec'), globals())"
                % (str(script), str(script))
            )
            failure = self.execute(code, "external")

        self.assertEqual(
            failure["primary_frame"],
            {"file": str(script), "line": 3, "function": "build"},
        )
        self.assertEqual(failure["code_line"], "return parts['ConcreteCylindr']")
        self.assertIn("ConcreteCylinder", failure["recovery"]["possible_keys"])
        self.assertNotIn("available_keys_sample", failure["recovery"])

    def test_diagnostic_suggestions_are_bounded_and_actionable(self):
        method = self.execute(
            "class Part:\n"
            "    def generateMesh(self):\n"
            "        pass\n"
            "part = Part()\n"
            "part.genrateMesh()",
            "method",
        )
        self.assertIn("generateMesh", method["recovery"]["possible_members"])

        keyword = self.execute("sorted([2, 1], revers=True)", "keyword")
        self.assertIn("reverse", keyword["recovery"]["possible_keywords"])

        name = self.execute("mesh.Mesh()", "name")
        self.assertEqual(name["recovery"]["import_suggestion"], "from abaqus import mesh")

        typo = self.execute("pritn('hello')", "name-typo")
        self.assertIn("print", typo["recovery"]["possible_names"])

    def test_syntax_error_reports_line_offset_and_text(self):
        failure = self.execute("def broken(:\n    pass\n", "syntax")

        self.assertEqual(failure["primary_frame"]["line"], 1)
        self.assertEqual(failure["recovery"]["syntax_line"], 1)
        self.assertIsNotNone(failure["recovery"]["syntax_offset"])
        self.assertIn("def broken(:", failure["recovery"]["syntax_text"])

    def test_nonserializable_result_does_not_poison_next_call(self):
        first = self.execute(
            "class Bad:\n"
            "    def __repr__(self):\n"
            "        raise RuntimeError('repr failed')\n"
            "result = Bad()",
            "bad-result",
        )
        second = self.execute("print('second only')", "after-bad-result")

        self.assertIn("repr failed", first["return_value"]["repr"])
        self.assertIsNone(second["return_value"])
        self.assertEqual(second["stdout"], "second only\n")

    def test_stdout_and_stderr_are_truncated_with_total_length(self):
        result = self.execute(
            "import sys\n"
            "print('x' * 5000)\n"
            "sys.stderr.write('y' * 5000)",
            "limited-output",
        )

        self.assertTrue(result["stdout"].endswith("(truncated, total 5001 chars)"))
        self.assertTrue(result["stderr"].endswith("(truncated, total 5000 chars)"))
        self.assertLess(len(result["stdout"]), 4100)
        self.assertLess(len(result["stderr"]), 4100)

    def test_plugin_log_is_disabled_by_default_and_rotated_in_debug_mode(self):
        scope = load_plugin_symbols("_log", "_clear_old_log")
        with tempfile.TemporaryDirectory() as folder:
            log_path = Path(folder) / "abaqus_mcp_gui_plugin.log"
            log_path.write_text("old log", encoding="utf-8")
            scope.update({
                "io": io,
                "os": os,
                "time": time,
                "LOG_PATH": str(log_path),
                "_DEBUG_LOG": False,
                "_LOG_LIMIT": 20,
                "_LOG_LOCK": threading.Lock(),
            })

            scope["_clear_old_log"]()
            scope["_log"]("disabled")
            self.assertFalse(log_path.exists())

            scope["_DEBUG_LOG"] = True
            scope["_log"]("first entry")
            scope["_log"]("second entry")
            self.assertTrue(log_path.exists())
            self.assertTrue(Path(str(log_path) + ".1").exists())


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout


class ClientExecutionIdTests(unittest.TestCase):
    def _request_with_response(self, response_factory):
        sent = []
        fake_socket = _FakeSocket()

        def capture_message(sock, payload):
            sent.append(payload)

        def read_response(sock):
            return response_factory(sent[0])

        with patch.object(
            client_module.socket,
            "create_connection",
            return_value=fake_socket,
        ), patch.object(
            client_module,
            "send_message",
            side_effect=capture_message,
        ), patch.object(
            client_module,
            "read_message",
            side_effect=read_response,
        ):
            return AbaqusBridgeClient().execute("result = 1"), sent[0]

    def test_execution_id_round_trip(self):
        def response(request):
            return {
                "id": request["id"],
                "ok": True,
                "result": {
                    "executionId": request["id"],
                    "return_value": 1,
                },
            }

        result, request = self._request_with_response(response)

        self.assertEqual(request["params"]["executionId"], request["id"])
        self.assertEqual(result["executionId"], request["id"])
        self.assertEqual(result["requestId"], request["id"])

    def test_mismatched_execution_id_is_rejected(self):
        def response(request):
            return {
                "id": request["id"],
                "ok": True,
                "result": {
                    "executionId": "different-execution",
                    "return_value": 1,
                },
            }

        with self.assertRaises(BridgeError) as raised:
            self._request_with_response(response)

        self.assertEqual(raised.exception.state, "UNKNOWN")
        self.assertTrue(raised.exception.execution_id)

    def test_connection_refused_is_not_started(self):
        with patch.object(
            client_module.socket,
            "create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            with self.assertRaises(BridgeError) as raised:
                AbaqusBridgeClient().ping()

        self.assertEqual(raised.exception.state, "NOT_STARTED")
        self.assertEqual(raised.exception.error_type, "ConnectionRefusedError")

    def test_agent_reports_cancelled_and_unknown(self):
        for state in ("CANCELLED", "UNKNOWN"):
            with self.subTest(state=state):
                def response(request):
                    return {
                        "id": request["id"],
                        "ok": False,
                        "error": {
                            "message": state,
                            "type": "TimeoutError",
                            "state": state,
                        },
                    }

                with self.assertRaises(BridgeError) as raised:
                    self._request_with_response(response)

                self.assertEqual(raised.exception.state, state)
                self.assertEqual(raised.exception.error_type, "TimeoutError")

    def test_queued_request_can_be_cancelled_before_start(self):
        gui_request = load_plugin_symbols("GuiRequest")["GuiRequest"]
        item = gui_request("execute", {})

        self.assertTrue(item.cancel_if_queued())
        self.assertFalse(item.start_if_queued())


if __name__ == "__main__":
    unittest.main()
