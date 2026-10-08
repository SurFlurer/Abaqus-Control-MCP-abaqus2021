"""Regression checks for the Abaqus bridge protocol."""

import ast
import base64
import json
import sys
import tempfile
import threading
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
