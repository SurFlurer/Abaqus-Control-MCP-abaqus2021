"""Abaqus/CAE GUI-side MCP bridge plugin.

This file is packaged so the installer can copy it into the Abaqus plugin
search directory without requiring a source checkout.
"""

from abaqusGui import (
    AFXForm,
    AFXMode,
    FXMAPFUNC,
    SEL_COMMAND,
    SEL_TIMEOUT,
    getAFXApp,
    sendCommand,
    showAFXErrorDialog,
)
import base64
import io
import json
import os
import platform
import sys
import tempfile
import threading
import time
import traceback
import uuid

try:
    import queue
except ImportError:  # Abaqus/CAE 2021 uses Python 2.7.
    import Queue as queue

try:
    import socketserver
except ImportError:  # Abaqus/CAE 2021 uses Python 2.7.
    import SocketServer as socketserver

try:
    TimeoutError
except NameError:  # Python 2.7 compatibility.
    TimeoutError = RuntimeError

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)


class GuiBridgeExecutionError(RuntimeError):
    def __init__(self, message, state="UNKNOWN", error_type="GuiBridgeExecutionError"):
        RuntimeError.__init__(self, message)
        self.state = state
        self.error_type = error_type


HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
LOG_PATH = os.path.join(tempfile.gettempdir(), "abaqus_mcp_gui_plugin.log")
_DEBUG_LOG = os.environ.get("ABAQUS_MCP_DEBUG", "").lower() in ("1", "true", "yes", "on")
_LOG_LIMIT = 1024 * 1024
_LOG_LOCK = threading.Lock()


def _log(message):
    if not _DEBUG_LOG:
        return
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= _LOG_LIMIT:
                rotated_path = LOG_PATH + ".1"
                try:
                    if os.path.exists(rotated_path):
                        os.remove(rotated_path)
                    os.rename(LOG_PATH, rotated_path)
                except Exception:
                    pass
            with io.open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(u"%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _clear_old_log():
    if _DEBUG_LOG:
        return
    for path in (LOG_PATH, LOG_PATH + ".1"):
        try:
            os.remove(path)
        except OSError:
            pass


def _announce(message):
    print(message)
    try:
        main_window = getAFXApp().getAFXMainWindow()
        if hasattr(main_window, "writeToMessageArea"):
            main_window.writeToMessageArea(message)
    except Exception:
        pass


def _send(sock, payload):
    if sys.version_info[0] < 3:
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    else:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def _recv(sock):
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n" if sys.version_info[0] >= 3 else "\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
    if sys.version_info[0] < 3:
        return json.loads("".join(chunks))
    return json.loads(b"".join(chunks).decode("utf-8"))


def _as_utf8_bytes(value):
    if sys.version_info[0] < 3:
        if isinstance(value, unicode):
            return value.encode("utf-8")
        for encoding in (sys.getfilesystemencoding() or "", "utf-8", "mbcs", "latin-1"):
            if not encoding:
                continue
            try:
                return value.decode(encoding).encode("utf-8")
            except Exception:
                pass
        return repr(value)
    return value.encode("utf-8")


def _kernel_wrapper(code, response_path, execution_id=""):
    encoded_code = base64.b64encode(_as_utf8_bytes(code)).decode("ascii")
    encoded_path = base64.b64encode(_as_utf8_bytes(response_path)).decode("ascii")
    encoded_id = base64.b64encode(_as_utf8_bytes(execution_id)).decode("ascii")
    template = r'''
import ast
import base64
import contextlib
import difflib
import io
import inspect
import json
import linecache
import os
import re
import sys
import traceback

try:
    import builtins
except ImportError:
    import __builtin__ as builtins

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

code = base64.b64decode("__ABAQUS_MCP_CODE__").decode("utf-8")
response_path = base64.b64decode("__ABAQUS_MCP_RESPONSE__").decode("utf-8")
execution_id = base64.b64decode("__ABAQUS_MCP_ID__").decode("ascii")

def _exec_compiled(compiled, namespace):
    if sys.version_info[0] < 3:
        exec("exec compiled in namespace, namespace")
    else:
        exec(compiled, namespace, namespace)

class _redirect_streams(object):
    def __init__(self, stdout, stderr):
        self.stdout = stdout
        self.stderr = stderr
        self._old_stdout = None
        self._old_stderr = None

    def __enter__(self):
        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        sys.stdout = self.stdout
        sys.stderr = self.stderr

    def __exit__(self, exc_type, exc_value, tb):
        sys.stdout = self._old_stdout
        sys.stderr = self._old_stderr
        return False

def _jsonable(value):
    if sys.version_info[0] < 3:
        if isinstance(value, unicode):
            return value
        if isinstance(value, str):
            for encoding in ("utf-8", sys.getfilesystemencoding() or "", "mbcs", "latin-1"):
                if not encoding:
                    continue
                try:
                    return value.decode(encoding)
                except Exception:
                    pass
            return repr(value).decode("ascii", "replace")
        if isinstance(value, dict):
            return dict((_jsonable(k), _jsonable(v)) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        if isinstance(value, set):
            return [_jsonable(v) for v in sorted(list(value), key=lambda x: repr(x))]
    try:
        json.dumps(value, ensure_ascii=True, allow_nan=False)
        return value
    except Exception:
        try:
            description = repr(value)
        except Exception as exc:
            description = "<repr failed: %s>" % type(exc).__name__
        return {
            "repr": _jsonable(description[:4000]),
            "type": _jsonable("%s.%s" % (type(value).__module__, type(value).__name__)),
        }

def _node_source(node):
    unparse = getattr(ast, "unparse", None)
    if unparse is not None:
        try:
            return unparse(node)
        except Exception:
            pass
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _node_source(node.value)
        return "%s.%s" % (base, node.attr) if base else None
    if isinstance(node, ast.Subscript):
        base = _node_source(node.value)
        key = _node_source(node.slice)
        return "%s[%s]" % (base, key) if base and key else None
    if isinstance(node, ast.Index):
        return _node_source(node.value)
    if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Str):
        return repr(node.s)
    if isinstance(node, ast.Num):
        return repr(node.n)
    return None

def _key_literal(node):
    if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.Index):
        return _key_literal(node.value)
    return None

def _is_user_source(filename):
    if filename.startswith("<abaqus-mcp:"):
        return True
    if not filename or filename.startswith("<") or not os.path.isfile(filename):
        return False
    normalized = os.path.normcase(os.path.abspath(filename))
    portable = normalized.replace("\\", "/").lower()
    if "/site-packages/" in portable or "/dist-packages/" in portable:
        return False
    roots = [sys.prefix]
    base_prefix = getattr(sys, "base_prefix", None)
    if base_prefix:
        roots.append(base_prefix)
    traceback_root = os.path.dirname(os.path.dirname(traceback.__file__))
    roots.append(traceback_root)
    for root in roots:
        root_path = os.path.normcase(os.path.abspath(root))
        if normalized == root_path or normalized.startswith(root_path + os.sep):
            return False
    return True


def _traceback_frames(tb):
    frames = []
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if _is_user_source(filename):
            frames.append({
                "file": filename,
                "line": tb.tb_lineno,
                "function": tb.tb_frame.f_code.co_name,
            })
        tb = tb.tb_next
    return frames


def _extract_tb_lineno(exc, tb=None):
    if hasattr(exc, "lineno") and getattr(exc, "lineno") is not None:
        return getattr(exc, "lineno")
    if tb is None:
        tb = getattr(exc, "__traceback__", None)
    frames = _traceback_frames(tb)
    if frames:
        return frames[-1]["line"]
    return None

def _find_subscript_parent(source, missing_key, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _key_literal(node.slice) == missing_key:
            src = _node_source(node.value)
            if src is not None:
                candidates.append((getattr(node, "lineno", 0), src))
    if not candidates and lineno is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and getattr(node, "lineno", None) == lineno:
                src = _node_source(node.value)
                if src is not None:
                    candidates.append((lineno, src))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda c: abs(c[0] - lineno))
    return candidates[0][1]

def _find_attribute_parent(source, missing_attr, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == missing_attr:
            candidates.append((getattr(node, "lineno", 0), _node_source(node.value)))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda item: abs(item[0] - lineno))
    return candidates[0][1]

def _extract_func_name(exc):
    import re
    msg = str(exc)
    m = re.search(r"([\w.]+)\(\)", msg)
    if m:
        return m.group(1)
    m = re.search(r"'([\w.]+)'", msg)
    if m:
        return m.group(1)
    return None


def _extract_code_excerpt(code, lineno, radius=2):
    if lineno is None:
        return None
    lines = code.splitlines()
    if not lines:
        return None
    index = max(0, lineno - 1)
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    excerpt_lines = []
    for i in range(start, end):
        prefix = ">>" if i == index else "  "
        excerpt_lines.append("%s %4d | %s" % (prefix, i + 1, lines[i]))
    return "\n".join(excerpt_lines)

def _resolve_simple_expr(expr, namespace):
    def _eval_node(node):
        if isinstance(node, ast.Name):
            if node.id in namespace:
                return namespace[node.id]
            return getattr(builtins, node.id)
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = _eval_node(node.value)
            key = _key_literal(node.slice)
            if key is None:
                key = ast.literal_eval(node.slice)
            return base[key]
        raise ValueError("unsupported expression node")

    parsed = ast.parse(expr, mode="eval")
    return _eval_node(parsed.body)

def _extract_call_target(code, lineno):
    if lineno is None:
        return None
    try:
        tree = ast.parse(code)
    except Exception:
        try:
            lines = code.splitlines()
            if lineno - 1 < 0 or lineno - 1 >= len(lines):
                return None
            tree = ast.parse(lines[lineno - 1], mode="exec")
        except Exception:
            return None
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if start is None:
            continue
        if end is None:
            end = start
        if start <= lineno <= end:
            candidates.append(node)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            getattr(item, "end_lineno", getattr(item, "lineno", 0))
            - getattr(item, "lineno", 0)
        )
    )
    return _node_source(candidates[0].func)


def _extract_params_from_sig(sig_str):
    match = re.search(r"\((.*)\)", sig_str)
    if not match:
        return []
    content = match.group(1)
    params = []
    depth = 0
    current = []
    for char in content:
        if char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            params.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        params.append("".join(current).strip())
    names = []
    for param in params:
        match = re.match(r"^([a-zA-Z_]\w*)", param)
        if match and match.group(1) not in ("self", "cls", "args", "kwargs"):
            names.append(match.group(1))
    return names


def _extract_invalid_keyword(message):
    patterns = (
        r"got an unexpected keyword argument ['\"](\w+)['\"]",
        r"['\"](\w+)['\"] is an invalid keyword argument",
        r"keyword error on (\w+)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.I)
        if match:
            return match.group(1)
    return None

def _summarize_mapping_keys(mapping_obj, missing_key):
    result = {"possible_keys": []}
    keys_method = getattr(mapping_obj, "keys", None)
    if not callable(keys_method):
        return result
    try:
        key_texts = [str(k) for k in list(keys_method())]
    except Exception:
        return result

    result["available_key_count"] = len(key_texts)
    if missing_key is not None:
        try:
            result["possible_keys"] = difflib.get_close_matches(
                str(missing_key), key_texts, n=3, cutoff=0.6
            )
        except Exception:
            pass
    if not result["possible_keys"]:
        result["available_keys_sample"] = key_texts[:5]
    return result

def _summarize_object_members(obj, missing_attr):
    result = {"possible_members": []}
    try:
        members = sorted([name for name in dir(obj) if not name.startswith("_")])
    except Exception:
        return result

    if missing_attr:
        try:
            result["possible_members"] = difflib.get_close_matches(
                missing_attr, members, n=3, cutoff=0.6
            )
        except Exception:
            pass
    return result


def _extract_signature_from_docstring(doc, target_name):
    pattern = r"\b" + re.escape(target_name) + r"\s*\("
    match = re.search(pattern, doc)
    if not match:
        return None
    start = match.start()
    open_paren = match.end() - 1
    depth = 0
    end = -1
    for index in range(open_paren, min(open_paren + 1000, len(doc))):
        char = doc[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return None
    return " ".join(doc[start:end].split())


def _summarize_callable(target_expr, namespace, invalid_keyword=None):
    summary = {
        "call_target": target_expr,
        "callable_signature": None,
        "callable_summary": None,
    }
    if not target_expr:
        return summary
    try:
        target = _resolve_simple_expr(target_expr, namespace)
    except Exception:
        return summary

    target_name = getattr(target, "__name__", "function")
    sig_str = None
    signature = getattr(inspect, "signature", None)
    if signature is not None:
        try:
            sig_str = "%s%s" % (target_name, signature(target))
        except Exception:
            sig_str = None
    if sig_str is None:
        argspec = getattr(inspect, "getfullargspec", None) or getattr(inspect, "getargspec", None)
        if argspec is not None:
            try:
                spec = argspec(target)
                args = list(getattr(spec, "args", []) or [])
                varargs = getattr(spec, "varargs", None)
                keywords = getattr(spec, "varkw", None) or getattr(spec, "keywords", None)
                if args and args[0] in ("self", "cls"):
                    args = args[1:]
                if varargs:
                    args.append("*" + varargs)
                if keywords:
                    args.append("**" + keywords)
                sig_str = "%s(%s)" % (target_name, ", ".join(args))
            except Exception:
                sig_str = None
    if sig_str is None:
        sig_str = "%s(...)" % target_name

    doc = None
    try:
        doc = inspect.getdoc(target)
    except Exception:
        pass
    if not doc:
        try:
            doc = getattr(target, "__doc__", None)
        except Exception:
            pass
    if doc:
        lines = doc.strip().splitlines()
        if lines:
            first_line = lines[0].strip()
            if " -> " in first_line:
                first_line = first_line.split(" -> ", 1)[1].strip()
            summary["callable_summary"] = first_line or None
        if sig_str.endswith("(...)"):
            extracted = _extract_signature_from_docstring(doc, target_name)
            if extracted:
                sig_str = extracted
    if len(sig_str) > 320:
        sig_str = sig_str[:317] + "..."
    summary["callable_signature"] = sig_str

    if invalid_keyword:
        valid_params = []
        if signature is not None:
            try:
                valid_params = [
                    name for name in signature(target).parameters.keys()
                    if name not in ("self", "cls", "args", "kwargs")
                ]
            except Exception:
                valid_params = []
        if not valid_params:
            valid_params = _extract_params_from_sig(sig_str)
        if valid_params:
            summary["possible_keywords"] = difflib.get_close_matches(
                invalid_keyword, valid_params, n=3, cutoff=0.6
            )
    return summary

def _format_execution_error(source, exc, namespace=None, tb=None):
    tb_str = traceback.format_exc()
    tb_lines = [line for line in tb_str.strip().splitlines() if line.strip()]
    core_error = tb_lines[-1] if tb_lines else str(exc)
    exc_type_name = type(exc).__name__
    error_module = type(exc).__module__
    if not isinstance(error_module, str):
        error_module = getattr(error_module, "__name__", str(error_module))
    error_type = "%s.%s" % (error_module, exc_type_name)

    if tb is None:
        tb = getattr(exc, "__traceback__", None)
    user_tbs = []
    cursor = tb
    while cursor is not None:
        if _is_user_source(cursor.tb_frame.f_code.co_filename):
            user_tbs.append(cursor)
        cursor = cursor.tb_next
    primary_tb = user_tbs[-1] if user_tbs else None

    syntax_file = getattr(exc, "filename", None) if isinstance(exc, SyntaxError) else None
    if syntax_file and _is_user_source(syntax_file):
        lineno = getattr(exc, "lineno", None)
        primary_frame = {
            "file": syntax_file,
            "line": lineno,
            "function": "<module>",
        }
    elif primary_tb is not None:
        lineno = primary_tb.tb_lineno
        primary_frame = {
            "file": primary_tb.tb_frame.f_code.co_filename,
            "line": lineno,
            "function": primary_tb.tb_frame.f_code.co_name,
        }
    else:
        lineno = _extract_tb_lineno(exc, tb)
        primary_frame = None

    source_file = primary_frame.get("file") if primary_frame else None
    if source_file and not source_file.startswith("<"):
        linecache.checkcache(source_file)
    source_for_error = "".join(linecache.getlines(source_file)) if source_file else source
    if not source_for_error:
        source_for_error = source

    diagnostic_namespace = dict(namespace or {})
    if primary_tb is not None:
        diagnostic_namespace.update(primary_tb.tb_frame.f_globals)
        diagnostic_namespace.update(primary_tb.tb_frame.f_locals)

    code_excerpt = _extract_code_excerpt(source_for_error, lineno)
    source_lines = source_for_error.splitlines()
    code_line = None
    if lineno is not None and 0 < lineno <= len(source_lines):
        code_line = source_lines[lineno - 1].strip()[:240]

    is_key_error = isinstance(exc, KeyError) or exc_type_name.endswith("KeyError")
    is_attribute_error = isinstance(exc, AttributeError) or exc_type_name.endswith("AttributeError")
    is_name_error = exc_type_name == "NameError"
    is_type_error = isinstance(exc, TypeError) or exc_type_name.endswith("TypeError")
    is_syntax_error = isinstance(exc, SyntaxError) or exc_type_name.endswith("SyntaxError")

    detected_missing_key = None
    detected_parent_path = None
    if lineno is not None:
        try:
            if 0 <= lineno - 1 < len(source_lines):
                line_tree = ast.parse(source_lines[lineno - 1])
                for node in ast.walk(line_tree):
                    if not isinstance(node, ast.Subscript):
                        continue
                    val_key = _key_literal(node.slice)
                    if val_key is None and isinstance(node.slice, ast.Name):
                        val_key = diagnostic_namespace.get(node.slice.id)
                    if val_key is None:
                        continue
                    parent_path = _node_source(node.value)
                    if not parent_path:
                        continue
                    try:
                        parent_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
                        keys_method = getattr(parent_obj, "keys", None)
                        if not callable(keys_method):
                            continue
                        try:
                            keys_list = list(keys_method())
                            is_missing = val_key not in keys_list
                        except Exception:
                            is_missing = val_key not in parent_obj
                        if is_missing:
                            is_key_error = True
                            detected_missing_key = val_key
                            detected_parent_path = parent_path
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    if is_key_error:
        missing_key = (
            detected_missing_key
            if detected_missing_key is not None
            else (exc.args[0] if exc.args else None)
        )
        parent_path = (
            detected_parent_path
            if detected_parent_path is not None
            else _find_subscript_parent(source_for_error, missing_key, lineno)
        )
        recovery = {
            "missing_key": _jsonable(missing_key),
            "parent_object_path": parent_path,
        }
        if parent_path:
            try:
                parent_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
                recovery.update(_summarize_mapping_keys(parent_obj, missing_key))
            except Exception:
                pass
    elif is_attribute_error:
        missing_attr = getattr(exc, "name", None)
        if not missing_attr:
            match = re.search(r"has no attribute ['\"]([^'\"]+)['\"]", str(exc))
            if match:
                missing_attr = match.group(1)
        source_obj = getattr(exc, "obj", None)
        parent_path = (
            _find_attribute_parent(source_for_error, missing_attr, lineno)
            if missing_attr
            else None
        )
        if source_obj is None and parent_path:
            try:
                source_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
            except Exception:
                pass
        object_type = type(source_obj).__name__ if source_obj is not None else None
        recovery = {
            "missing_attribute": missing_attr,
            "object_type": object_type,
            "parent_object_path": parent_path,
        }
        if source_obj is not None:
            recovery.update(_summarize_object_members(source_obj, missing_attr))
    elif is_name_error:
        missing_var = getattr(exc, "name", None)
        if not missing_var:
            match = re.search(r"name ['\"]([^'\"]+)['\"] is not defined", str(exc))
            if match:
                missing_var = match.group(1)
        recovery = {"missing_variable": missing_var}
        if missing_var:
            names = sorted(
                set([str(key) for key in diagnostic_namespace.keys()] + dir(builtins))
            )
            recovery["possible_names"] = difflib.get_close_matches(
                missing_var, names, n=3, cutoff=0.6
            )
        abaqus_modules = set([
            "mesh", "part", "material", "assembly", "step", "interaction",
            "load", "section", "sketch", "job", "connector", "visualization",
            "xyPlot", "displayGroup", "meshEdit", "connectorBehavior",
            "symbolicConstants",
        ])
        if missing_var in abaqus_modules:
            recovery["import_suggestion"] = "from abaqus import %s" % missing_var
        elif missing_var == "C":
            recovery["import_suggestion"] = "from abaqusConstants import *"
        elif missing_var and missing_var.isupper() and len(missing_var) > 1:
            recovery["import_suggestion"] = "from abaqusConstants import *"
    elif is_syntax_error:
        recovery = {
            "syntax_line": getattr(exc, "lineno", None),
            "syntax_offset": getattr(exc, "offset", None),
            "syntax_text": getattr(exc, "text", None),
        }
    elif is_type_error:
        call_target = _extract_call_target(source_for_error, lineno)
        invalid_keyword = _extract_invalid_keyword(core_error)
        recovery = {"call_target": call_target}
        recovery.update(
            _summarize_callable(call_target, diagnostic_namespace, invalid_keyword)
        )
    else:
        recovery = {}

    if not recovery:
        call_target = _extract_call_target(source_for_error, lineno)
        if call_target:
            recovery = _summarize_callable(call_target, diagnostic_namespace)

    frames = [
        {
            "file": frame.tb_frame.f_code.co_filename,
            "line": frame.tb_lineno,
            "function": frame.tb_frame.f_code.co_name,
        }
        for frame in user_tbs[-5:]
    ]
    return {
        "ok": False,
        "core_error": core_error,
        "message": getattr(exc, "msg", str(exc)) if is_syntax_error else str(exc),
        "error_type": error_type,
        "error_line": lineno,
        "primary_frame": primary_frame,
        "frames": frames,
        "traceback_tail": tb_lines[-8:] if len(tb_lines) > 8 else tb_lines,
        "code_excerpt": code_excerpt,
        "code_line": code_line,
        "recovery": recovery,
    }


_MAX_OUTPUT = 4000


class _LimitedWriter(object):
    encoding = "utf-8"

    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.count = 0

    def write(self, value):
        if not isinstance(value, string_types):
            value = str(value)
        length = len(value)
        if self.count < self.limit:
            self.parts.append(value[:self.limit - self.count])
        self.count += length
        return length

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        result = "".join(self.parts)
        if self.count > self.limit:
            result += "\n... (truncated, total %d chars)" % self.count
        return result


namespace = globals().setdefault("_ABAQUS_MCP_GLOBALS", {
    "__name__": "__abaqus_mcp_exec__",
    "__doc__": None,
})
namespace.update({
    "mdb": globals().get("mdb"),
    "session": globals().get("session"),
})
namespace.pop("result", None)

filename = "<abaqus-mcp:%s>" % (execution_id or "legacy")
if len(code) <= 262144:
    source_keys = globals().setdefault("_ABAQUS_MCP_SOURCE_KEYS", [])
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    source_keys.append(filename)
    if len(source_keys) > 128:
        linecache.cache.pop(source_keys.pop(0), None)

stdout = _LimitedWriter(_MAX_OUTPUT)
stderr = _LimitedWriter(_MAX_OUTPUT)
returned = None
mode = "exec"
payload = None

try:
    with _redirect_streams(stdout, stderr):
        try:
            parsed = ast.parse(code, filename=filename, mode="exec")
            if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr):
                mode = "eval"
                expression = ast.Expression(body=parsed.body[0].value)
                compiled = compile(expression, filename, "eval")
                returned = eval(compiled, namespace, namespace)
            else:
                compiled = compile(parsed, filename, "exec")
                _exec_compiled(compiled, namespace)
                returned = namespace.pop("result", None)
        except Exception as exc:
            returned_error = _format_execution_error(
                code, exc, namespace, sys.exc_info()[2]
            )
            returned_error["executionId"] = execution_id
            payload = {
                "ok": True,
                "result": returned_error,
            }

    if payload is None:
        payload = {
            "ok": True,
            "result": {
                "mode": mode,
                "ok": True,
                "executionId": execution_id,
                "return_value": _jsonable(returned),
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
            },
        }
except Exception as exc:
    execution_error = _format_execution_error(
        code, exc, namespace, sys.exc_info()[2]
    )
    execution_error["executionId"] = execution_id
    payload = {
        "ok": True,
        "result": execution_error,
    }
finally:
    namespace.pop("result", None)

with open(response_path, "w") as handle:
    json.dump(_jsonable(payload), handle, ensure_ascii=True)
'''
    command = (
        template.replace("__ABAQUS_MCP_CODE__", encoded_code)
        .replace("__ABAQUS_MCP_RESPONSE__", encoded_path)
        .replace("__ABAQUS_MCP_ID__", encoded_id)
    )
    if sys.version_info[0] < 3 and isinstance(command, unicode):
        command = command.encode("ascii")
    return command


def _run_kernel_code(code, timeout, execution_id=""):
    response_path = os.path.join(
        tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex
    )
    command = _kernel_wrapper(code, response_path, execution_id)
    _log("sendCommand start response_path=%s" % response_path)
    sendCommand(command, False, False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(response_path):
            with open(response_path, "r") as handle:
                payload = json.load(handle)
            try:
                os.remove(response_path)
            except Exception:
                pass
            if not payload.get("ok"):
                error = payload.get("error", {})
                _log("kernel error: %s" % error.get("message", "kernel command failed"))
                raise GuiBridgeExecutionError(
                    error.get("message", "kernel command failed"),
                    state="UNKNOWN",
                    error_type=error.get("type", "KernelError"),
                )
            _log("kernel response ok")
            return payload["result"]
        time.sleep(0.05)

    raise GuiBridgeExecutionError(
        "timed out while Abaqus kernel execution was running; outcome unknown",
        state="UNKNOWN",
        error_type="TimeoutError",
    )


class GuiRequest:
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = "queued"
        self.result = None
        self.error = None

    def cancel_if_queued(self):
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            return True

    def start_if_queued(self):
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "running"
            return True


class McpGuiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _recv(self.request)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            execution_id = params.get("executionId") or request_id
            _log("request method=%s id=%s" % (method, request_id))

            if request_id is not None and execution_id != request_id:
                raise GuiBridgeExecutionError(
                    "request id and executionId do not match",
                    state="NOT_STARTED",
                    error_type="ExecutionIdMismatch",
                )
            if _DISPATCHER is None:
                raise GuiBridgeExecutionError(
                    "GUI dispatcher is not initialized",
                    state="NOT_STARTED",
                    error_type="DispatcherUnavailable",
                )

            wait_timeout = float(params.get("timeout") or os.environ.get("ABAQUS_MCP_TIMEOUT", "60")) + 5.0
            item = GuiRequest(method, params)
            _REQUESTS.put(item)
            _log("queued method=%s id=%s" % (method, request_id))

            if not item.event.wait(wait_timeout):
                if item.cancel_if_queued():
                    raise GuiBridgeExecutionError(
                        "timed out before GUI execution; request cancelled",
                        state="CANCELLED",
                        error_type="TimeoutError",
                    )
                raise GuiBridgeExecutionError(
                    "timed out while GUI execution was running; outcome unknown",
                    state="UNKNOWN",
                    error_type="TimeoutError",
                )
            if item.error is not None:
                raise item.error
            result = item.result

            _send(self.request, {"id": request_id, "ok": True, "result": result})
            _log("response ok id=%s" % request_id)
        except Exception as exc:
            _log("response error id=%s error=%s" % (request_id, exc))
            error_state = getattr(exc, "state", "UNKNOWN")
            error_type = getattr(
                exc,
                "error_type",
                "%s.%s" % (type(exc).__module__, type(exc).__name__),
            )
            _send(
                self.request,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": error_type,
                        "state": error_state,
                        "traceback": traceback.format_exc(),
                    },
                },
            )


class McpGuiServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


_SERVER = None
_DISPATCHER = None
_REQUESTS = queue.Queue()


def _handle_on_gui_thread(item):
    method = item.method
    params = item.params
    timeout = float(params.get("timeout") or os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))
    execution_id = params.get("executionId", "")

    if method == "ping":
        code = (
            "import os, sys, platform\n"
            "from abaqus import mdb, session\n"
            "try:\n"
            "    cpu_count = os.cpu_count()\n"
            "except AttributeError:\n"
            "    import multiprocessing\n"
            "    cpu_count = multiprocessing.cpu_count()\n"
            "result = {'python': sys.version, 'executable': sys.executable, "
            "'platform': platform.platform(), 'pid': os.getpid(), "
            "'cpu_count': cpu_count, "
            "'abaqus_version': getattr(session, 'version', None), "
            "'models': list(mdb.models.keys()), "
            "'viewports': list(session.viewports.keys())}"
        )
        result = _run_kernel_code(code, timeout, execution_id)
        result = result["return_value"]
        result["executionId"] = execution_id
        result["guiProcess"] = {
            "python": sys.version,
            "platform": platform.platform(),
            "thread": threading.current_thread().name,
        }
        return result

    if method == "execute":
        code = params.get("code")
        if not isinstance(code, string_types) or not code.strip():
            raise GuiBridgeExecutionError(
                "params.code must be a non-empty string",
                state="NOT_STARTED",
                error_type="ValueError",
            )
        return _run_kernel_code(code, timeout, execution_id)

    raise GuiBridgeExecutionError(
        "unknown method: %r" % method,
        state="NOT_STARTED",
        error_type="ValueError",
    )


def start_gui_agent():
    global _SERVER
    if _SERVER is not None:
        _log("start requested, already running")
        return "Abaqus MCP GUI agent is already running on %s:%s" % (HOST, PORT)
    _clear_old_log()
    _log("starting GUI agent on %s:%s" % (HOST, PORT))
    _SERVER = McpGuiServer((HOST, PORT), McpGuiHandler)
    thread = threading.Thread(target=_SERVER.serve_forever, name="AbaqusMcpGuiAgent")
    thread.daemon = True
    thread.start()
    _log("started GUI agent")
    return "Abaqus MCP GUI agent listening on %s:%s" % (HOST, PORT)


def stop_gui_agent():
    global _SERVER
    if _SERVER is None:
        return "Abaqus MCP GUI agent is not running."
    try:
        _SERVER.shutdown()
        _SERVER.server_close()
    finally:
        _SERVER = None
    _log("stopped GUI agent")
    return "Abaqus MCP GUI agent stopped."


class McpGuiActionForm(AFXForm):
    ID_START = AFXForm.ID_LAST + 1
    ID_STOP = AFXForm.ID_LAST + 2
    ID_POLL = AFXForm.ID_LAST + 3

    def __init__(self, owner, action):
        global _DISPATCHER
        AFXForm.__init__(self, owner)
        self.action = action
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, McpGuiActionForm.onCmdActivate)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_START, McpGuiActionForm.onCmdStart)
        FXMAPFUNC(self, SEL_COMMAND, self.ID_STOP, McpGuiActionForm.onCmdStop)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, McpGuiActionForm.onTimeout)
        _DISPATCHER = self

    def onCmdActivate(self, sender, sel, ptr):
        if self.action == "start":
            return self.onCmdStart(sender, sel, ptr)
        elif self.action == "stop":
            return self.onCmdStop(sender, sel, ptr)
        return 1

    def getFirstDialog(self):
        return None

    def _schedule_poll(self):
        getAFXApp().addTimeout(100, self, self.ID_POLL)

    def onTimeout(self, sender, sel, ptr):
        processed = 0
        while processed < 5:
            try:
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            if not item.start_if_queued():
                processed += 1
                continue
            try:
                _log("GUI thread handling method=%s" % item.method)
                item.result = _handle_on_gui_thread(item)
                _log("GUI thread handled method=%s" % item.method)
            except Exception as exc:
                item.error = exc
                _log("GUI thread error method=%s error=%s" % (item.method, exc))
            finally:
                with item.lock:
                    item.state = "done"
                item.event.set()
            processed += 1

        if _SERVER is not None:
            self._schedule_poll()
        return 1

    def onCmdStart(self, sender, sel, ptr):
        try:
            message = start_gui_agent()
            self._schedule_poll()
            _announce(message)
            _announce("Abaqus MCP GUI plugin log: %s" % LOG_PATH)
        except Exception as exc:
            message = "Abaqus MCP GUI agent failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

    def onCmdStop(self, sender, sel, ptr):
        try:
            message = stop_gui_agent()
            _announce(message)
        except Exception as exc:
            message = "Abaqus MCP GUI agent stop failed: %s" % exc
            _log(message)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
        return 1

if not globals().get("_ABAQUS_CONTROL_MCP_MENU_REGISTERED"):
    toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
    toolset.registerGuiMenuButton(
        object=McpGuiActionForm(toolset, "start"),
        buttonText="Abaqus-Control-MCP|Start MCP Bridge",
        version="0.1.0",
        author="Codex",
        applicableModules=["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"],
        description="Start the TCP bridge for the active Abaqus/CAE session.",
    )
    toolset.registerGuiMenuButton(
        object=McpGuiActionForm(toolset, "stop"),
        buttonText="Abaqus-Control-MCP|Stop MCP Bridge",
        version="0.1.0",
        author="Codex",
        applicableModules=["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"],
        description="Stop the TCP bridge.",
    )
    _ABAQUS_CONTROL_MCP_MENU_REGISTERED = True
