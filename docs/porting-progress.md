# Abaqus 2021/2022 Python 2.7 Backport Progress

## Baseline

- Stable source repository: `D:\AI Works\Abaqus-Control-MCP-abaqus2021`
- Compatibility branch: `backport/abaqus-2021-v2-semantics`
- Compatibility baseline tag: `baseline-34c9f19`
- Compatibility baseline commit: `34c9f192e9427706900335b56481cba3bc7c966a`
- Upstream reference: `Whfkl/Abaqus-Control-MCP` main `53d32434481abca1d42d32653f32acf0d02559f5`

## P0 Completed

| Commit | Change |
| --- | --- |
| `d529ac4` | Pin MCP SDK to `>=1.0.0,<2` for the existing FastMCP API. |
| `7a5c541` | Activate GUI menu actions through `AFXMode.ID_ACTIVATE`. |
| `f01b33a` | Clear and pop the kernel `result` for every execution. |
| `eb35dc7` | Correlate bridge requests with `executionId` and add timeout states. |
| `c09fde4` | Align the runtime package version with `pyproject.toml`. |

The implementation also includes regression coverage for result isolation,
execution ID round trips, mismatched IDs, connection refusal, and queued
request cancellation.

## Verified On 2026-10-08

Abaqus/CAE 2022 was connected through the installed GUI plugin on
`127.0.0.1:48152`.

- Kernel Python: `2.7.15`
- GUI thread: `MainThread`
- Model: `Model-1`
- Viewport: `Viewport: 1`
- CPU count: `12`
- Ping `executionId` matched `requestId`
- `result = {'sum': 1 + 1, 'python': __import__('sys').version}` returned
  `sum = 2` and Python `2.7.15`
- Consecutive calls `result = 123` and `value = 456` returned `123` and
  `None`, confirming that stale results are not retained

Local checks:

- `python -m unittest discover -s tests -v`: 6 tests passed
- Abaqus Python 2.7.15 successfully compiled the source and installed GUI plugin
- Installed GUI plugin SHA-256 matched the source checkout

## Packaging Note

The local uv tool package was rebuilt as `0.1.1` from this checkout. The active
Codex MCP server process held the old launcher executable open during the
install, so that process must be restarted before it loads the new code. The
package environment itself points to this source checkout.

## Next Phase

P1 work remains:

- Python 2 traceback and structured error diagnostics
- Per-call stdout/stderr limits and debug-only GUI logging
- Evidence-based Abaqus job monitoring
- ODB step, frame, field, and bounded history inspection

