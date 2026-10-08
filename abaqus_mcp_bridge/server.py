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


_ODB_CODE = r"""
import math
import os
from odbAccess import openOdb

path = os.path.abspath(__ODB_PATH__)
step_name = __STEP__
frame_index = __FRAME__
variable = __VARIABLE__
set_name = __SET_NAME__
component = __COMPONENT__
history_region = __HISTORY_REGION__
history_variable = __HISTORY_VARIABLE__
max_points = __MAX_POINTS__

if not os.path.isfile(path):
    raise IOError('ODB file not found: %s' % path)

def _finite(value):
    try:
        return math.isfinite(value)
    except AttributeError:
        return not (math.isnan(value) or math.isinf(value))

odb = openOdb(path=path, readOnly=True)
try:
    def _step_meta(name, step):
        domain = str(getattr(step, 'domain', ''))
        start = float(getattr(step, 'totalTime', 0.0)) if domain == 'TIME' else None
        duration = float(getattr(step, 'timePeriod', 0.0)) if domain == 'TIME' else None
        count = len(step.frames)
        if count <= 5:
            indices = list(range(count))
        else:
            indices = sorted(set([
                0,
                count // 4,
                count // 2,
                (3 * count) // 4,
                count - 1,
            ]))
        frames = []
        for index in indices:
            frame = step.frames[index]
            frames.append({
                'index': index,
                'frameId': frame.frameId,
                'frameValue': frame.frameValue,
                'description': str(getattr(frame, 'description', '')),
            })
        fields = {}
        if count:
            for frame in (step.frames[0], step.frames[-1]):
                for key in frame.fieldOutputs.keys():
                    field = frame.fieldOutputs[key]
                    fields[key] = {
                        'name': key,
                        'type': str(getattr(field, 'type', '')),
                        'components': list(getattr(field, 'componentLabels', ()) or ()),
                    }
        regions = []
        for key in list(step.historyRegions.keys())[:100]:
            region = step.historyRegions[key]
            regions.append({
                'name': key,
                'variables': list(region.historyOutputs.keys())[:100],
            })
        return {
            'name': name,
            'description': str(getattr(step, 'description', '')),
            'procedure': str(getattr(step, 'procedure', '')),
            'domain': domain,
            'startTime': start,
            'endTime': (
                start + duration
                if start is not None and duration is not None
                else None
            ),
            'duration': duration,
            'frameCount': count,
            'frames': frames,
            'fieldOutputs': list(fields.values()),
            'historyRegions': regions,
        }

    names = list(odb.steps.keys())
    if step_name and step_name not in odb.steps:
        raise KeyError(
            'Unknown step %r; available: %s' % (step_name, names)
        )
    selected_name = step_name or (names[-1] if names else None)
    info = {
        'path': path,
        'title': str(getattr(odb, 'title', '')),
        'description': str(getattr(odb, 'description', '')),
        'parts': list(odb.parts.keys()),
        'instances': list(odb.rootAssembly.instances.keys()),
        'assemblyNodeSets': list(odb.rootAssembly.nodeSets.keys())[:200],
        'assemblyElementSets': list(odb.rootAssembly.elementSets.keys())[:200],
        'steps': [_step_meta(name, odb.steps[name]) for name in names],
    }

    if variable or history_variable:
        if selected_name is None:
            raise ValueError('ODB has no steps')
        step = odb.steps[selected_name]
        info['selectedStep'] = selected_name

    if variable:
        if not step.frames:
            raise ValueError('Selected step has no frames')
        index = frame_index if frame_index >= 0 else len(step.frames) + frame_index
        if not 0 <= index < len(step.frames):
            raise IndexError(
                'frame index out of range; frameCount=%d' % len(step.frames)
            )
        frame = step.frames[index]
        available_fields = list(frame.fieldOutputs.keys())
        if variable not in frame.fieldOutputs:
            raise KeyError(
                'Field %r unavailable in selected frame; available: %s'
                % (variable, available_fields)
            )
        field = frame.fieldOutputs[variable]
        if set_name:
            assembly = odb.rootAssembly
            location = ''
            if field.values:
                location = str(field.values[0].position)
            collection_names = (
                ('nodeSets',)
                if 'NODAL' in location
                else ('elementSets',)
            )
            matches = []
            for collection_name in collection_names:
                collection = getattr(assembly, collection_name)
                if set_name in collection:
                    matches.append(collection[set_name])
            for instance_name in assembly.instances.keys():
                instance = assembly.instances[instance_name]
                for collection_name in collection_names:
                    collection = getattr(instance, collection_name)
                    for key in collection.keys():
                        if set_name == instance_name + '.' + key:
                            matches.append(collection[key])
            if not matches:
                raise KeyError(
                    'Unknown set %r; use assembly set or INSTANCE.SET'
                    % set_name
                )
            if len(matches) != 1:
                raise ValueError(
                    'Set name is ambiguous; use INSTANCE.SET or a unique assembly set'
                )
            field = field.getSubset(region=matches[0])

        if component:
            labels = list(getattr(field, 'componentLabels', ()) or ())
            if component not in labels:
                raise KeyError(
                    'Unknown component %r; available: %s'
                    % (component, labels)
                )
            field = field.getScalarField(componentLabel=component)

        count = 0
        minimum = None
        maximum = None
        field_type = str(getattr(field, 'type', ''))
        for item in field.values:
            data = item.data
            if hasattr(data, '__len__') and not isinstance(data, (str, bytes)):
                if component:
                    raise ValueError(
                        'Component extraction returned nonscalar data'
                    )
                if field_type not in ('VECTOR', 'SCALAR'):
                    raise ValueError('Tensor field requires a component')
                scalar = math.sqrt(sum(float(part) ** 2 for part in data))
            else:
                scalar = float(data)
            if not _finite(scalar):
                continue
            count += 1
            minimum = scalar if minimum is None else min(minimum, scalar)
            maximum = scalar if maximum is None else max(maximum, scalar)
        quantity = (
            'component'
            if component
            else ('magnitude' if field_type == 'VECTOR' else 'scalar')
        )
        info['fieldSummary'] = {
            'step': selected_name,
            'frameIndex': index,
            'frameValue': frame.frameValue,
            'variable': variable,
            'component': component,
            'set': set_name,
            'quantity': quantity,
            'count': count,
            'min': minimum,
            'max': maximum,
        }

    if history_variable:
        keys = list(step.historyRegions.keys())
        region_key = history_region or (keys[0] if len(keys) == 1 else None)
        if region_key is None:
            raise ValueError(
                'Specify history_region; available: %s' % keys[:100]
            )
        if region_key not in step.historyRegions:
            raise KeyError(
                'Unknown history region %r; available: %s'
                % (region_key, keys[:100])
            )
        outputs = step.historyRegions[region_key].historyOutputs
        available_history = list(outputs.keys())
        if history_variable not in outputs:
            raise KeyError(
                'Unknown history variable %r; available: %s'
                % (history_variable, available_history)
            )
        points = outputs[history_variable].data
        count = len(points)
        if count <= max_points:
            indices = list(range(count))
        else:
            indices = sorted(set([
                int(round(i * (count - 1) / float(max_points - 1)))
                for i in range(max_points)
            ]))
        sampled = [
            [float(points[index][0]), float(points[index][1])]
            for index in indices
        ]
        values = [
            float(pair[1])
            for pair in points
            if _finite(float(pair[1]))
        ]
        info['historySummary'] = {
            'step': selected_name,
            'region': region_key,
            'variable': history_variable,
            'pointCount': count,
            'returnedPoints': len(sampled),
            'min': min(values) if values else None,
            'max': max(values) if values else None,
            'points': sampled,
            'xAxis': (
                'step time'
                if str(getattr(step, 'domain', '')) == 'TIME'
                else str(getattr(step, 'domain', ''))
            ),
        }
    result = info
finally:
    odb.close()
"""


@mcp.tool()
async def inspect_odb(
    odb_path: str,
    step: str = "",
    frame: int = -1,
    variable: str = "",
    set_name: str = "",
    component: str = "",
    history_region: str = "",
    history_variable: str = "",
    max_points: int = 200,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Inspect an ODB read-only and optionally summarize fields or history.

    Args:
        odb_path: Path to the ODB file.
        step: Step to inspect. Defaults to the last step.
        frame: Frame index, with negative values counting from the end.
        variable: Field output to summarize.
        set_name: Optional assembly or INSTANCE.SET subset.
        component: Optional vector/tensor component.
        history_region: History region required when the step has several.
        history_variable: History output to sample.
        max_points: Maximum number of history points returned.
    """
    if not odb_path.strip():
        raise ValueError("odb_path must not be empty")
    if not 2 <= max_points <= 1000:
        raise ValueError("max_points must be between 2 and 1000")
    code = _code(
        _ODB_CODE,
        odb_path=odb_path,
        step=step,
        frame=int(frame),
        variable=variable,
        set_name=set_name,
        component=component,
        history_region=history_region,
        history_variable=history_variable,
        max_points=int(max_points),
    )
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
