"""MCP stdio server that forwards Python execution requests to Abaqus.

Core tools: ping, run_python — plus viewport capture and ODB metadata.
Most debugging should come from run_python's structured error payload.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from .client import AbaqusBridgeClient


def _ensure_gui_plugin() -> None:
    """Install the GUI plugin silently if not already present."""
    try:
        import shutil
        from importlib import resources
        from pathlib import Path

        target_dir = Path(os.environ.get("ABAQUS_MCP_PLUGIN_DIR", Path.home() / "abaqus_plugins"))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "abaqus_mcp_gui_plugin.py"

        source = resources.files("abaqus_mcp_bridge").joinpath("gui_plugin.py")
        with resources.as_file(source) as src:
            if target.exists() and target.read_bytes() == Path(src).read_bytes():
                return
            shutil.copy2(src, target)
    except Exception:
        pass


_ensure_gui_plugin()


DEFAULT_HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "60"))

INSTRUCTIONS = """You are an elite simulation engineer controlling a live Abaqus/CAE session via MCP tools.
Your goal is to build robust, highly stable, and production-grade finite element models.

CORE SIMULATION RULES:
1. SEMANTIC GEOMETRY (Golden Rule): Better not use raw coordinates or `findAt()` to assign boundary conditions, sections, or loads.
    - Immediately after creating a part or feature, grab its geometry using robust methods:
      * `getByBoundingBox(xMin, yMin, zMin, xMax, yMax, zMax)`
      * `getByBoundingCylinder(...)`
      * Topological filtering (e.g., `part.faces[0:1]`, `part.edges.findAt(...)` only for static boundaries).
    - Wrap the grabbed geometry into named Sets (for cells/nodes/vertices) or Surfaces (for faces/edges) IMMEDIATELY.
    - All subsequent steps (meshing, section assignments, interactions, loads, and BCs) MUST reference these semantic names (e.g., `region=part.sets['Set-Name']` or `surface=instance.surfaces['Surf-Name']`).

2. DYNAMIC CHUNKING: Do not write massive scripts that perform modeling, meshing, solver submission, and post-processing all at once.
    - Divide your work into logical validation milestones (e.g., Phase 1: Base Geometry & Named Sets -> Phase 2: Assembly, Step & Mesh -> Phase 3: Physics, Loads & Submission).
    - Verify each phase's correctness via execution before proceeding. No rigid linear chains — adapt the phase size to the task's complexity.

3. DOCKING & DIAGNOSTICS (No Guessing & Web Search): Never guess Abaqus API methods, attributes, or signatures.
    - If live local reflection is insufficient, or if you encounter complex API signature mismatches, ALWAYS call the web search tool (including the term 'Abaqus' and target versions, e.g., 'Abaqus 2024 Python API ...') to find official documentation, forum examples, or method signatures.


4. WORKING DIRECTORY & PERSISTENCE: Every Abaqus analysis generates a massive number of solver files (.inp, .odb, .sta, .msg, etc.).
    - Before writing any model, ALWAYS query or set a clean working directory (using the `set_workdir` tool or standard Python `os.chdir` at the start of your script). Do NOT let Abaqus run in default system paths, which causes permission errors and directory pollution.
    - Periodically save the database (.cae file) to the working directory using `mdb.saveAs(pathName=...)` (e.g., `mdb.saveAs(pathName='D:/temp/My-Model.cae')`). Never leave the CAE session unsaved, ensuring persistence against solver crashes.

5. CONCISE PAIR-PROGRAMMING: Avoid robotic headers, repetitive intent declarations, and verbose apologies. Keep explanations technical, clear, and direct. Focus on finite element modeling best practices.

"""

mcp = FastMCP("abaqus-control-mcp", instructions=INSTRUCTIONS)


def _client(timeout: float | None = None) -> AbaqusBridgeClient:
    return AbaqusBridgeClient(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Generic python execution wrapper
# ---------------------------------------------------------------------------


async def _exec(code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute Python code in Abaqus and return the result dict."""
    return await anyio.to_thread.run_sync(_client(timeout).execute, code)


def _code(template: str, **values: Any) -> str:
    for key, value in values.items():
        template = template.replace("__" + key.upper() + "__", repr(value))
    return template


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ping(timeout: float | None = None) -> dict[str, Any]:
    """Check whether the Abaqus-side bridge agent is reachable.

    Returns session state including Python version, platform, PID, CPU count,
    models, and viewports.
    """
    return await _exec(
        "from abaqus import mdb, session\n"
        "import os, sys, platform\n"
        "try:\n"
        "  cpu_count = os.cpu_count()\n"
        "except AttributeError:\n"
        "  import multiprocessing\n"
        "  cpu_count = multiprocessing.cpu_count()\n"
        "result = {\n"
        "  'python': sys.version,\n"
        "  'executable': sys.executable,\n"
        "  'platform': platform.platform(),\n"
        "  'pid': os.getpid(),\n"
        "  'cpu_count': cpu_count,\n"
        "  'abaqus_version': getattr(session, 'version', None),\n"
        "  'models': list(mdb.models.keys()),\n"
        "  'viewports': list(session.viewports.keys()),\n"
        "}",
        timeout,
    )


@mcp.tool()
async def run_python(code: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute Python code in the active Abaqus/CAE kernel.

    Single-line expressions are evaluated and their value returned.
    Multi-line code is executed; set a variable named ``result`` to return data.
    Stdout, stderr, traceback, error line, and code excerpts are included in the response.
    """
    if not code.strip():
        raise ValueError("code must not be empty")
    return await _exec(code, timeout)


@mcp.tool()
async def set_workdir(path: str, timeout: float | None = None) -> dict[str, Any]:
    """Change the Abaqus working directory.

    Args:
        path: Absolute path to set as the working directory.
    Returns the previous and new working directory.
    """
    if not path.strip():
        raise ValueError("path must not be empty")
    code = r"""
import os

new_path = __PATH__

result = {}
try:
    old_dir = os.getcwd()
    if not os.path.isdir(new_path):
        result = {'success': False, 'error': 'Directory does not exist: ' + new_path}
    else:
        os.chdir(new_path)
        result = {'success': True, 'previous': old_dir, 'current': os.getcwd()}
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
""".replace("__PATH__", json.dumps(path.strip()))
    return await _exec(code, timeout)


_JOB_CODE = r"""
import os
import re
import time
from abaqus import mdb

name = __JOB_NAME__
terminate = __TERMINATE__
since = __SINCE__
root = os.getcwd()

def _tail(path, maximum=262144):
    if not os.path.isfile(path):
        return ''
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - maximum))
        return handle.read().decode('utf-8', 'replace')

def _file_info(path):
    exists = os.path.isfile(path)
    return {
        'path': path,
        'exists': exists,
        'modifiedAt': os.path.getmtime(path) if exists else None,
    }

def _status(job):
    try:
        return str(job.status).upper() if job is not None else None
    except Exception:
        return None

if not name:
    result = {
        'jobs': [
            {'name': key, 'caeStatus': _status(mdb.jobs[key])}
            for key in mdb.jobs.keys()
        ],
        'workingDirectory': root,
    }
else:
    if name != os.path.basename(name) or name in ('.', '..'):
        raise ValueError('job_name must be a single file name')
    job = mdb.jobs[name] if name in mdb.jobs else None
    if terminate and job is None:
        raise ValueError('Termination requires a job registered in the current CAE session')

    kill_requested = False
    kill_error = None
    terminal_cae = ('COMPLETED', 'ABORTED', 'TERMINATED', 'CHECK_COMPLETED')
    if terminate:
        if _status(job) in terminal_cae:
            kill_error = 'Job already has a terminal CAE status; no kill command sent'
        else:
            try:
                job.kill()
                kill_requested = True
            except Exception as exc:
                kill_error = '%s: %s' % (type(exc).__name__, exc)
            deadline = time.time() + 5.0
            while (
                kill_requested
                and time.time() < deadline
                and _status(job) not in ('TERMINATED', 'ABORTED', 'COMPLETED')
            ):
                time.sleep(0.25)

    paths = {
        ext: os.path.join(root, name + '.' + ext)
        for ext in ('log', 'sta', 'dat', 'msg', 'lck')
    }
    files = dict((ext, _file_info(path)) for ext, path in paths.items())
    log = _tail(paths['log'])
    sta = _tail(paths['sta'])
    dat = _tail(paths['dat'])
    msg = _tail(paths['msg'])
    cae = _status(job)
    combined = '\n'.join((log, sta, dat, msg))
    diagnostics = [
        line.strip()
        for line in combined.splitlines()
        if re.search(r'^\s*(?:\*{2,}\s*|ABAQUS\s+)(?:ERROR|WARNING)\b', line, re.I)
    ]
    errors = [
        line for line in diagnostics if re.search(r'\bERROR\b', line, re.I)
    ][-10:]
    warnings = [
        line for line in diagnostics if re.search(r'\bWARNING\b', line, re.I)
    ][-10:]

    log_upper = log.upper()
    sta_upper = sta.upper()
    file_state = None
    if re.search(
        r'ABAQUS JOB .* (?:ABORTED|FAILED)|THE ANALYSIS HAS NOT BEEN COMPLETED|'
        r'\bA N A L Y S I S\s+T E R M I N A T E D\b',
        log_upper,
    ):
        file_state = 'FAILED'
    elif re.search(r'ABAQUS JOB .* TERMINATED|USER REQUESTED TERMINATION', log_upper):
        file_state = 'TERMINATED'
    elif re.search(
        r'ABAQUS JOB .* COMPLETED|THE ANALYSIS HAS COMPLETED SUCCESSFULLY',
        log_upper + '\n' + sta_upper,
    ):
        file_state = 'COMPLETED'

    cae_state = {
        'COMPLETED': 'COMPLETED',
        'CHECK_COMPLETED': 'COMPLETED',
        'ABORTED': 'FAILED',
        'TERMINATED': 'TERMINATED',
        'RUNNING': 'RUNNING',
        'CHECK_RUNNING': 'RUNNING',
        'SUBMITTED': 'SUBMITTED',
    }.get(cae)
    lock_exists = files['lck']['exists']
    terminal_mtime = max(
        [
            files[ext]['modifiedAt']
            for ext in ('log', 'sta')
            if files[ext]['modifiedAt'] is not None
        ]
        or [0]
    )
    file_is_fresh = since is not None and terminal_mtime >= since
    effective_file_state = file_state if since is None or file_is_fresh else None
    conflict = bool(
        effective_file_state
        and cae_state
        and (
            cae_state in ('RUNNING', 'SUBMITTED')
            or (
                cae_state in ('COMPLETED', 'FAILED', 'TERMINATED')
                and cae_state != effective_file_state
            )
        )
    )

    if conflict:
        state, source = 'UNKNOWN', 'conflicting CAE and file evidence'
    elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED') and lock_exists:
        state, source = 'UNKNOWN', 'terminal CAE status while lock file exists'
    elif cae_state in ('COMPLETED', 'FAILED', 'TERMINATED'):
        state, source = cae_state, 'CAE job status'
    elif effective_file_state and not lock_exists and job is None and file_is_fresh:
        state, source = effective_file_state, 'log/status file modified after since'
    elif file_state and not lock_exists and job is None:
        state, source = 'UNKNOWN', 'file terminal marker but current run freshness unverified'
    elif file_state and not lock_exists and job is not None and cae_state is None:
        state, source = 'UNKNOWN', 'terminal file marker without a current CAE run'
    elif effective_file_state and lock_exists:
        state, source = 'UNKNOWN', 'terminal file marker while lock file exists'
    elif cae_state:
        state, source = cae_state, 'CAE job status'
    elif lock_exists:
        state, source = 'UNKNOWN', 'lock file only; process not verified'
    else:
        state, source = 'UNKNOWN', 'insufficient evidence'

    progress = [line.strip() for line in sta.splitlines() if line.strip()]
    numeric = [line for line in progress if re.match(r'^\d+\s+\d+\s+', line)]
    current_step = None
    increment = None
    if numeric:
        match = re.match(r'^(\d+)\s+(\d+)\b', numeric[-1])
        if match:
            current_step = int(match.group(1))
            increment = int(match.group(2))

    result = {
        'jobName': name,
        'workingDirectory': root,
        'state': state,
        'terminal': state in ('COMPLETED', 'FAILED', 'TERMINATED'),
        'source': source,
        'caeStatus': cae,
        'fileState': file_state,
        'currentStep': current_step,
        'increment': increment,
        'progressTail': progress[-8:],
        'errors': errors,
        'warnings': warnings,
        'errorCountInTails': len(
            [line for line in diagnostics if re.search(r'\bERROR\b', line, re.I)]
        ),
        'warningCountInTails': len(
            [line for line in diagnostics if re.search(r'\bWARNING\b', line, re.I)]
        ),
        'files': files,
        'observedAt': time.time(),
        'since': since,
        'runFreshness': (
            'confirmed by CAE'
            if source == 'CAE job status' and state in ('COMPLETED', 'FAILED', 'TERMINATED')
            else ('bounded by since' if file_is_fresh else 'unverified')
        ),
    }
    if terminate:
        result['termination'] = {
            'requested': kill_requested,
            'confirmed': state == 'TERMINATED' and not lock_exists,
            'error': kill_error,
        }
"""


@mcp.tool()
async def monitor_job_status(
    job_name: str = "",
    terminate: bool = False,
    since: float | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Inspect Abaqus job state using CAE and filesystem evidence.

    Args:
        job_name: Job to inspect. If empty, list the jobs registered in CAE.
        terminate: Request termination and report requested/confirmed separately.
        since: Unix submission time used to bind file-only evidence to a run.
    """
    if terminate and not job_name.strip():
        raise ValueError("job_name is required for termination")
    if since is not None and not 0 <= since <= 4102444800:
        raise ValueError("since must be a Unix timestamp between 1970 and 2100")
    code = _code(
        _JOB_CODE,
        job_name=job_name.strip(),
        terminate=bool(terminate),
        since=since,
    )
    return await _exec(code, timeout)


@mcp.tool()
async def inspect_odb(odb_path: str, timeout: float | None = None) -> dict[str, Any]:
    """Open an ODB file (read-only) and return its metadata.

    Returns steps (with sliced frames and total time), parts, instances, section points,
    and available field/history output variables with positions and components.
    """
    code = r"""
from abaqus import mdb
from odbAccess import openOdb

odb_path = __ODB_PATH__
info = {}
odb = None
try:
    odb = openOdb(path=odb_path, readOnly=True)
    info['title'] = str(getattr(odb, 'title', ''))
    info['description'] = str(getattr(odb, 'description', ''))
    info['parts'] = list(odb.parts.keys()) if hasattr(odb, 'parts') else []
    info['instances'] = list(odb.rootAssembly.instances.keys()) if hasattr(odb, 'rootAssembly') else []
    steps = []

    def _slice_frames(frames):
        count = len(frames)
        if count <= 5:
            return list(frames)
        idxs = [0]
        for k in range(1, 4):
            idxs.append(int(round(k * (count - 1) / 4.0)))
        idxs.append(count - 1)
        seen = set()
        uniq = []
        for i in idxs:
            if i not in seen and 0 <= i < count:
                uniq.append(i)
                seen.add(i)
        return [frames[i] for i in uniq]

    def _field_meta(field_output):
        meta = {}
        try:
            meta['position'] = str(getattr(field_output, 'position', ''))
        except Exception:
            meta['position'] = ''
        comps = []
        try:
            comps = list(getattr(field_output, 'componentLabels', []) or [])
        except Exception:
            comps = []
        try:
            invariants = list(getattr(field_output, 'validInvariants', []) or [])
        except Exception:
            invariants = []
        meta['components'] = comps + [str(x) for x in invariants if str(x) not in comps]
        return meta

    for sname in odb.steps.keys():
        s = odb.steps[sname]
        frames = []
        for f in _slice_frames(s.frames):
            frames.append({
                'frameId': f.frameId,
                'frameValue': f.frameValue,
                'description': str(getattr(f, 'description', '')),
            })
        step_info = {
            'name': sname,
            'procedure': str(getattr(s, 'procedure', '')),
            'totalTime': getattr(s, 'totalTime', 0.0),
            'frames': frames,
            'description': str(getattr(s, 'description', '')),
        }
        if s.frames:
            try:
                frame = s.frames[0]
                fov = []
                for desc in frame.fieldOutputs.keys():
                    fo = frame.fieldOutputs[desc]
                    meta = _field_meta(fo)
                    meta['name'] = desc
                    fov.append(meta)
                step_info['fieldOutputs'] = fov
            except Exception:
                step_info['fieldOutputs'] = []
            try:
                frame = s.frames[-1]
                hov = []
                for desc in frame.historyOutputs.keys():
                    hov.append(desc)
                step_info['historyOutputs'] = hov
            except Exception:
                step_info['historyOutputs'] = []
        steps.append(step_info)
    info['steps'] = steps
    result = {'success': True, 'data': info}
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
finally:
    try:
        if odb is not None:
            odb.close()
    except Exception:
        pass
""".replace("__ODB_PATH__", json.dumps(odb_path))
    return await _exec(code, timeout or 60.0)


# ---------------------------------------------------------------------------
# Viewport capture
# ---------------------------------------------------------------------------


@mcp.tool()
async def capture_viewport(
    viewport_name: str = "",
    image_format: str = "PNG",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Capture a screenshot of an Abaqus viewport as a base64-encoded image.

    Leave viewport_name empty to use the current viewport.
    Supported formats: PNG, JPEG, TIFF, SVG.
    """
    code = r"""
import os, tempfile, base64
from abaqus import session
import abaqusConstants as C

vp_name = __VP__
fmt = __FMT__

result = {}
try:
    if not vp_name or vp_name not in session.viewports.keys():
        vp_name = session.currentViewportName
    vp = session.viewports[vp_name]
    tmp = tempfile.NamedTemporaryFile(suffix='.' + fmt.lower(), delete=False)
    tmp.close()
    fmt_const = getattr(C, fmt.upper())
    session.printToFile(fileName=tmp.name, format=fmt_const, canvasObjects=(vp,))
    with open(tmp.name, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    os.unlink(tmp.name)
    result = {
        'success': True,
        'viewport': vp_name,
        'format': fmt.lower(),
        'image_base64': b64,
        'size_bytes': len(b64) * 3 // 4,
    }
except Exception as e:
    import traceback
    result = {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
""".replace("__VP__", json.dumps(viewport_name)) \
      .replace("__FMT__", json.dumps(image_format.upper()))
    return await _exec(code, timeout or 60.0)


# ---------------------------------------------------------------------------
# MCP Resource - real-time status
# ---------------------------------------------------------------------------


@mcp.resource("abaqus://session-telemetry")
def session_telemetry() -> str:
    """Retrieve active Abaqus/CAE session telemetry and environment metadata."""
    import json as _json
    try:
        r = _client(5.0).ping()
        if isinstance(r, dict) and r.get("ok", False):
            ret = r.get("return_value", r)
            return _json.dumps(ret, indent=2, ensure_ascii=False)
        return _json.dumps({"connected": False, "detail": str(r)}, indent=2)
    except Exception as e:
        return _json.dumps({"connected": False, "error": str(e)}, indent=2)


# ---------------------------------------------------------------------------
# MCP Resource - agent instructions
# ---------------------------------------------------------------------------


@mcp.resource("abaqus://agent-instructions")
def abaqus_agent_instructions() -> str:
    """Mandatory instructions for any AI agent controlling Abaqus via this MCP."""
    return INSTRUCTIONS





def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
