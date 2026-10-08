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

## P1 Completed

| Commit | Change |
| --- | --- |
| `b2e1980` | Port Python 2 traceback handling and structured diagnostics. |
| `76b8f72` | Bound captured output and make GUI request logging debug-only. |
| `e9fbd09` | Replace simple job tails with evidence-based job monitoring. |
| `c938f13` | Upgrade ODB inspection for frames, fields, sets, and history. |
| `e8b9f7e` | Verify the P1 MCP tool schema under the MCP 1.x host. |

P1 behavior now includes:

- `sys.exc_info()[2]` based traceback extraction with original script paths
- Compact user frames, syntax details, and bounded error suggestions
- Import hints for missing Abaqus modules and constants
- 4000-character stdout/stderr capture limits with total-length markers
- Debug-only GUI logging with 1 MiB rotation
- CAE and file-evidence reconciliation for job state
- Separate termination requested/confirmed reporting
- Read-only ODB metadata, field summaries, and bounded history samples

Verification:

- `python -m unittest discover -s tests -v`: 19 tests passed, 1 host-only test
  skipped because system Python has no MCP SDK
- `uv run python -m unittest discover -s tests -v`: 19 tests passed
- Abaqus Python 2.7.15 compiled the updated GUI plugin
- The MCP 1.x host exposes the new P1 job and ODB parameters

## Next Phase

P2 work remains:

- Native MCP image content for viewport captures
- Optional viewport save paths and additional image formats
- Connection-refused versus timeout user messaging
- Setup CLI decoupled from Claude-specific registration
