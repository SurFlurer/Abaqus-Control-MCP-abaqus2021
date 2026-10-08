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
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from abaqus_mcp_bridge import client as client_module
from abaqus_mcp_bridge.client import AbaqusBridgeClient, BridgeError


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "abaqus_mcp_bridge" / "gui_plugin.py"
SERVER_PATH = Path(__file__).resolve().parents[1] / "abaqus_mcp_bridge" / "server.py"


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


def load_server_symbols(*names):
    source = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    symbols = []
    for node in source.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            symbols.append(node)
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id in names
                for target in node.targets
            ):
                symbols.append(node)
    scope = {"Any": Any}
    module = ast.Module(body=symbols, type_ignores=[])
    exec(compile(module, str(SERVER_PATH), "exec"), scope)
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


class JobStateTests(unittest.TestCase):
    def setUp(self):
        symbols = load_server_symbols("_code", "_JOB_CODE")
        self.code_template = symbols["_JOB_CODE"]
        self.render_code = symbols["_code"]

    def execute_job_code(self, job, terminate=False, since=None):
        fake_abaqus = types.ModuleType("abaqus")
        fake_abaqus.mdb = types.SimpleNamespace(jobs={"Probe": job} if job else {})
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                if terminate:
                    Path("Probe.lck").write_text("still running", encoding="utf-8")
                code = self.render_code(
                    self.code_template,
                    job_name="Probe",
                    terminate=terminate,
                    since=since,
                )
                scope = {}
                with patch.dict(sys.modules, {"abaqus": fake_abaqus}):
                    exec(code, scope)
                return scope["result"], folder
            finally:
                os.chdir(previous)

    def test_old_completion_marker_does_not_override_running_cae_job(self):
        class Job:
            status = "RUNNING"

        fake_abaqus = types.ModuleType("abaqus")
        fake_abaqus.mdb = types.SimpleNamespace(jobs={"Probe": Job()})
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path("Probe.log").write_text(
                    "Abaqus JOB Probe COMPLETED\n",
                    encoding="utf-8",
                )
                Path("Probe.sta").write_text(
                    "1  16  1  0  0\nTHE ANALYSIS HAS COMPLETED SUCCESSFULLY\n",
                    encoding="utf-8",
                )
                Path("Probe.dat").write_text(
                    "0 ERROR MESSAGES\n0 WARNING MESSAGES\n",
                    encoding="utf-8",
                )
                Path("Probe.lck").write_text("active", encoding="utf-8")
                code = self.render_code(
                    self.code_template,
                    job_name="Probe",
                    terminate=False,
                    since=None,
                )
                scope = {}
                with patch.dict(sys.modules, {"abaqus": fake_abaqus}):
                    exec(code, scope)
                result = scope["result"]
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertFalse(result["terminal"])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["warnings"], [])
            finally:
                os.chdir(previous)

    def test_kill_request_requires_terminal_evidence(self):
        class Job:
            status = "RUNNING"

            def kill(self):
                self.status = "TERMINATED"

        result, _ = self.execute_job_code(Job(), terminate=True, since=None)

        self.assertTrue(result["termination"]["requested"])
        self.assertFalse(result["termination"]["confirmed"])
        self.assertEqual(result["state"], "UNKNOWN")

    def test_file_only_completion_needs_a_run_start_time(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                Path("Probe.log").write_text(
                    "Abaqus JOB Probe COMPLETED\n",
                    encoding="utf-8",
                )
                fake_abaqus = types.ModuleType("abaqus")
                fake_abaqus.mdb = types.SimpleNamespace(jobs={})
                code = self.render_code(
                    self.code_template,
                    job_name="Probe",
                    terminate=False,
                    since=None,
                )
                unknown = {}
                with patch.dict(sys.modules, {"abaqus": fake_abaqus}):
                    exec(code, unknown)
                code = self.render_code(
                    self.code_template,
                    job_name="Probe",
                    terminate=False,
                    since=1.0,
                )
                known = {}
                with patch.dict(sys.modules, {"abaqus": fake_abaqus}):
                    exec(code, known)

                self.assertEqual(unknown["result"]["state"], "UNKNOWN")
                self.assertEqual(unknown["result"]["fileState"], "COMPLETED")
                self.assertEqual(known["result"]["state"], "COMPLETED")
            finally:
                os.chdir(previous)


class OdbInspectionTests(unittest.TestCase):
    def setUp(self):
        symbols = load_server_symbols("_code", "_ODB_CODE")
        self.code_template = symbols["_ODB_CODE"]
        self.render_code = symbols["_code"]

    def test_read_only_field_and_bounded_history_inspection(self):
        class Item:
            def __init__(self, data, position="NODAL"):
                self.data = data
                self.position = position

        class Field:
            type = "VECTOR"
            componentLabels = ["U1", "U2"]

            def __init__(self, values):
                self.values = values

            def getSubset(self, region):
                return self

            def getScalarField(self, componentLabel):
                return Field([Item(item.data[0], item.position) for item in self.values])

        class Frame:
            def __init__(self, index):
                self.frameId = index
                self.frameValue = float(index)
                self.description = "frame %d" % index
                self.fieldOutputs = {
                    "U": Field([
                        Item([3.0, 4.0]),
                        Item([0.0, 5.0]),
                    ])
                }

        class Output:
            def __init__(self, data):
                self.data = data

        class Region:
            historyOutputs = {
                "U1": Output([[float(index), float(index)] for index in range(600)])
            }

        class Step:
            description = "load step"
            procedure = "Static"
            domain = "TIME"
            totalTime = 2.5
            timePeriod = 1.5
            frames = [Frame(0), Frame(1), Frame(2)]
            historyRegions = {"Node 1": Region()}

        class Odb:
            title = "probe"
            description = "test odb"
            parts = {"PART-1": object()}
            rootAssembly = types.SimpleNamespace(
                instances={},
                nodeSets={"SET-ALL": object()},
                elementSets={},
            )
            steps = {"Step-1": Step()}

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        odb = Odb()
        opened = {}
        module = types.ModuleType("odbAccess")

        def openOdb(path, readOnly):
            opened["path"] = path
            opened["readOnly"] = readOnly
            return odb

        module.openOdb = openOdb
        with tempfile.TemporaryDirectory() as folder:
            odb_path = Path(folder) / "probe.odb"
            odb_path.write_bytes(b"odb")
            code = self.render_code(
                self.code_template,
                odb_path=str(odb_path),
                step="Step-1",
                frame=-1,
                variable="U",
                set_name="",
                component="",
                history_region="Node 1",
                history_variable="U1",
                max_points=10,
            )
            scope = {}
            with patch.dict(sys.modules, {"odbAccess": module}):
                exec(code, scope)
            result = scope["result"]

        self.assertTrue(opened["readOnly"])
        self.assertTrue(odb.closed)
        self.assertEqual(result["steps"][0]["startTime"], 2.5)
        self.assertEqual(result["steps"][0]["endTime"], 4.0)
        self.assertEqual(result["steps"][0]["duration"], 1.5)
        self.assertEqual(result["fieldSummary"]["quantity"], "magnitude")
        self.assertEqual(result["fieldSummary"]["min"], 5.0)
        self.assertEqual(result["fieldSummary"]["max"], 5.0)
        self.assertEqual(result["historySummary"]["pointCount"], 600)
        self.assertEqual(result["historySummary"]["returnedPoints"], 10)
        self.assertEqual(len(result["historySummary"]["points"]), 10)


class ServerRegistrationTests(unittest.TestCase):
    def test_mcp_tools_expose_p1_parameters(self):
        try:
            import asyncio

            from abaqus_mcp_bridge import server
        except ImportError as exc:
            self.skipTest("MCP host dependencies are unavailable: %s" % exc)

        tools = asyncio.run(server.mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            sorted(by_name),
            [
                "capture_viewport",
                "inspect_odb",
                "monitor_job_status",
                "ping",
                "run_python",
                "set_workdir",
            ],
        )
        job_tool = by_name["monitor_job_status"]
        job_schema = getattr(job_tool, "input_schema", None)
        if job_schema is None:
            job_schema = job_tool.inputSchema
        job_properties = job_schema["properties"]
        self.assertIn("terminate", job_properties)
        self.assertIn("since", job_properties)
        odb_tool = by_name["inspect_odb"]
        odb_schema = getattr(odb_tool, "input_schema", None)
        if odb_schema is None:
            odb_schema = odb_tool.inputSchema
        odb_properties = odb_schema["properties"]
        self.assertIn("variable", odb_properties)
        self.assertIn("history_variable", odb_properties)
        self.assertIn("max_points", odb_properties)


if __name__ == "__main__":
    unittest.main()
