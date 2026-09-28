from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from dotenv import dotenv_values

from . import core, aoitalk, aoitalk_tasks, image_library, local_plan
from . import android, browser_qa, desktop, git_qa, jobs, readiness, telemetry
from .qa_common import register_tools
from .telemetry import instrument_tool, tracked_tool

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
_AOITALK_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def _aoitalk_enabled() -> bool:
    """Resolve the local opt-in without globally loading any dotenv file."""
    name = "HOME_MCP_AOITALK_ENABLED"
    if name in os.environ:
        value = os.environ[name]
    else:
        try:
            values = dotenv_values(GATEWAY_ROOT / ".env", encoding="utf-8-sig", interpolate=False)
        except Exception:
            # A missing/unreadable/malformed local .env must never enable the
            # integration accidentally.
            return False
        value = values.get(name)
    return isinstance(value, str) and value.strip().casefold() in _AOITALK_ENABLED_VALUES


@asynccontextmanager
async def lifespan(server):
    try:
        yield {}
    finally:
        await browser_qa.close_all()


mcp = MCPServer("Home MCP Gateway", lifespan=lifespan, middleware=[telemetry.ToolTelemetryMiddleware()])

# Register the two real-desktop Computer Use tools first. Some remote MCP
# consumers cache or cap large tool catalogs; keeping these first makes the
# primary observe -> act -> verify workflow available even on constrained
# clients. The full server still exposes every tool over normal MCP discovery.
register_tools(mcp, desktop.TOOLS)


@tracked_tool(mcp)
def system_info() -> dict[str, Any]:
    """Return host OS, CPU, RAM, disks, network interfaces, Python runtime, and NVIDIA GPU information when available."""
    return core.system_info()


@tracked_tool(mcp)
def get_environment(name: str = "") -> dict[str, Any]:
    """Read one environment variable, or all environment variables when name is empty."""
    return core.get_environment(name)


@tracked_tool(mcp)
def set_environment(name: str, value: str | None = None) -> dict[str, Any]:
    """Set an environment variable for this MCP server process. Pass null to remove it."""
    return core.set_environment(name, value)


@tracked_tool(mcp)
def list_directory(path: str, recursive: bool = False) -> dict[str, Any]:
    """List files and directories at any host path. Set recursive to walk the full tree."""
    return core.list_directory(path, recursive)


@tracked_tool(mcp)
def glob_paths(pattern: str, recursive: bool = True, max_results: int = 1000, timeout_sec: float = 10) -> dict[str, Any]:
    """Expand a filesystem glob pattern with bounded result and time limits."""
    return core.glob_paths(pattern, recursive, max_results, timeout_sec)


@tracked_tool(mcp)
def search_text(root: str, query: str, file_glob: str = "*", case_sensitive: bool = False,
                max_results: int = 0, timeout_sec: float = 30, max_files: int | None = None) -> dict[str, Any]:
    """Search text recursively with bounded timeout/output limits; max_files optionally caps enumeration."""
    return core.search_text(root, query, file_glob, case_sensitive, max_results, timeout_sec, max_files)


@tracked_tool(mcp)
def path_info(path: str) -> dict[str, Any]:
    """Return metadata for any filesystem path."""
    return core.path_info(path)


@tracked_tool(mcp)
def read_text(path: str, encoding: str = "utf-8", max_chars: int = 0) -> dict[str, Any]:
    """Read a text file. max_chars=0 means return the complete file."""
    return core.read_text(path, encoding, max_chars)


# One bounded, static read-only batch reduces MCP round trips without adding an
# arbitrary shell/process/write/network escape hatch.  Its search operation
# uses core's fixed-argv, bounded ripgrep backend when available.  local_plan
# validates every step before execution and applies its own result/time limits.
mcp.tool(annotations=ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
))(instrument_tool(local_plan.local_read_plan))


@tracked_tool(mcp)
def write_text(path: str, text: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    """Create or replace a text file at any path."""
    return core.write_text(path, text, encoding, create_parents)


@tracked_tool(mcp)
def append_text(path: str, text: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    """Append text to a file."""
    return core.append_text(path, text, encoding, create_parents)


@tracked_tool(mcp)
def read_file_base64(path: str) -> dict[str, Any]:
    """Read any binary file and return its bytes as base64."""
    return core.read_file_base64(path)


@tracked_tool(mcp)
def write_file_base64(path: str, base64_data: str, create_parents: bool = True) -> dict[str, Any]:
    """Create or replace any binary file from base64 data."""
    return core.write_file_base64(path, base64_data, create_parents)


@tracked_tool(mcp)
def make_directory(path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
    """Create a directory."""
    return core.make_directory(path, parents, exist_ok)


@tracked_tool(mcp)
def copy_path(source: str, destination: str, overwrite: bool = True) -> dict[str, Any]:
    """Copy a file or directory."""
    return core.copy_path(source, destination, overwrite)


@tracked_tool(mcp)
def move_path(source: str, destination: str) -> dict[str, Any]:
    """Move or rename a file or directory."""
    return core.move_path(source, destination)


@tracked_tool(mcp)
def delete_path(path: str, recursive: bool = True) -> dict[str, Any]:
    """Delete a file, symlink, or directory. Directories are recursive by default."""
    return core.delete_path(path, recursive)


@tracked_tool(mcp)
def hash_file(path: str, algorithm: str = "sha256") -> dict[str, Any]:
    """Calculate a cryptographic or hashlib-supported digest for a file."""
    return core.hash_file(path, algorithm)


@tracked_tool(mcp)
def run_command(command: str, cwd: str | None = None, timeout_sec: float = 0, env: dict[str, str] | None = None, max_output_chars: int = 0) -> dict[str, Any]:
    """Run an arbitrary command through the host shell. timeout_sec=0 and max_output_chars=0 mean unlimited."""
    return core.run_command(command, cwd, timeout_sec, env, max_output_chars)


@tracked_tool(mcp)
def run_powershell(script: str, cwd: str | None = None, timeout_sec: float = 0, env: dict[str, str] | None = None, max_output_chars: int = 0) -> dict[str, Any]:
    """Execute arbitrary PowerShell using pwsh or Windows PowerShell."""
    return core.run_powershell(script, cwd, timeout_sec, env, max_output_chars)


@tracked_tool(mcp)
def run_python(code: str, cwd: str | None = None, timeout_sec: float = 0, env: dict[str, str] | None = None, max_output_chars: int = 0) -> dict[str, Any]:
    """Execute arbitrary Python code with the same interpreter running this MCP server."""
    return core.run_python(code, cwd, timeout_sec, env, max_output_chars)


@tracked_tool(mcp)
def start_process(command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Start an arbitrary long-running process in the background and return its PID plus stdout/stderr log paths."""
    return core.start_process(command, cwd, env)


@tracked_tool(mcp)
def list_processes(name_contains: str | None = None, detailed: bool = False,
                   include_details: bool | None = None) -> dict[str, Any]:
    """List host processes; detailed status fields are opt-in."""
    return core.list_processes(name_contains, detailed, include_details)


@tracked_tool(mcp)
def process_status(pid: int, tail_chars: int = 20000) -> dict[str, Any]:
    """Inspect a process and, for processes started by start_process, return captured output tails."""
    return core.process_status(pid, tail_chars)


@tracked_tool(mcp)
def kill_process(pid: int, recursive: bool = True, force: bool = True) -> dict[str, Any]:
    """Terminate or kill a process, optionally including its descendants."""
    return core.kill_process(pid, recursive, force)


@tracked_tool(mcp)
def http_request(method: str, url: str, headers: dict[str, str] | None = None, body: str | None = None, body_base64: str | None = None, timeout_sec: float = 60, verify_tls: bool = True, max_response_bytes: int = 0) -> dict[str, Any]:
    """Send an arbitrary HTTP/HTTPS request, including to local services. Supports text or base64 request bodies."""
    return core.http_request(method, url, headers, body, body_base64, timeout_sec, verify_tls, max_response_bytes)


@tracked_tool(mcp)
def download_url(url: str, destination: str, headers: dict[str, str] | None = None, timeout_sec: float = 0, verify_tls: bool = True) -> dict[str, Any]:
    """Download a URL directly to any host filesystem path."""
    return core.download_url(url, destination, headers, timeout_sec, verify_tls)


@tracked_tool(mcp)
def screenshot(path: str = "") -> dict[str, Any]:
    """Capture all host screens to a PNG. Leave path empty to use the temporary directory."""
    return core.screenshot(path)


@tracked_tool(mcp)
def tool_log_info() -> dict[str, Any]:
    """Return the current privacy-safe MCP tool timing log location and status."""
    return telemetry.log_info()


@tracked_tool(mcp)
def tool_log_summary(window_minutes: int = 60, top_n: int = 20) -> dict[str, Any]:
    """Summarize recent MCP tool timings by call count, errors, total, average, p95, and maximum latency."""
    return telemetry.summarize_logs(window_minutes, top_n)


for module in (git_qa, jobs, android, readiness, browser_qa):
    register_tools(mcp, module.TOOLS)


# Dedicated ClipIngest tools have explicit write/read annotations. Their adapter
# returns bounded errors and never exposes local login credentials to MCP.
if _aoitalk_enabled():
    for function in aoitalk.TOOLS:
        writes_docs = function is aoitalk.aoitalk_clip_ingest
        mcp.tool(annotations=ToolAnnotations(
            read_only_hint=not writes_docs,
            destructive_hint=writes_docs,
            idempotent_hint=True,
            open_world_hint=True,
        ))(instrument_tool(function))

    # AoiTalk local-agent delegation: start returns a task_id immediately and the
    # other tools only read/cancel, so no MCP request waits on the long work.
    for function in aoitalk_tasks.TOOLS:
        read_only = function in aoitalk_tasks.READ_ONLY_TOOLS
        mcp.tool(annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=function is aoitalk_tasks.aoitalk_local_task_start,
            idempotent_hint=True,
            open_world_hint=not read_only,
        ))(instrument_tool(function))

for function in image_library.TOOLS:
    mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ))(instrument_tool(function))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
