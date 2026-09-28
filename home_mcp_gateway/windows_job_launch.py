"""Start independently of the client's Job Object using a one-shot interactive task.

The task has no trigger/password and is removed by the worker immediately after
startup. Task deletion does not stop the already running worker. Environment is
handed over with current-user DPAPI and removed before the command is launched.
"""
from __future__ import annotations

import json
import ctypes
from pathlib import Path
import subprocess
import sys


def _scheduler():
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def remove_task(name: str) -> None:
    import pythoncom
    pythoncom.CoInitialize()
    try:
        _scheduler().GetFolder("\\").DeleteTask(name, 0)
    finally:
        pythoncom.CoUninitialize()


def launch(directory: Path, environment: dict, metadata: dict) -> None:
    import pythoncom
    import win32api
    import win32crypt
    import win32security
    from .jobs import _save
    pythoncom.CoInitialize()
    name = "HomeMCPJob-" + metadata["job_id"]
    env_file = directory / "startup.env.dpapi"
    registered = False
    service = task = action = definition = None
    try:
        env_file.write_bytes(win32crypt.CryptProtectData(json.dumps(environment).encode("utf-8"), None, None, None, None, 0))
        service = _scheduler()
        task = service.NewTask(0)
        task.RegistrationInfo.Description = "Home MCP Gateway one-shot job bootstrap; no recurring trigger."
        user = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, win32api.GetUserNameEx(2))[0])
        task.Principal.UserId = user
        task.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN
        task.Principal.RunLevel = 1 if ctypes.windll.shell32.IsUserAnAdmin() else 0
        task.Settings.Hidden = True
        task.Settings.ExecutionTimeLimit = "PT0S"
        task.Settings.DisallowStartIfOnBatteries = False
        task.Settings.StopIfGoingOnBatteries = False
        task.Settings.AllowDemandStart = True
        task.Settings.MultipleInstances = 2
        action = task.Actions.Create(0)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        action.Path = str(pythonw)
        action.Arguments = subprocess.list2cmdline([str(Path(__file__).with_name("job_worker.py")), str(directory)])
        action.WorkingDirectory = str(directory)
        metadata.update(launch_method="windows_interactive_task", scheduler_task=name)
        _save(directory / "metadata.json", metadata)
        definition = service.GetFolder("\\").RegisterTaskDefinition(name, task, 2, user, None, 3)
        registered = True
        definition.Run("")
    except Exception:
        env_file.unlink(missing_ok=True)
        if registered:
            remove_task(name)
        raise
    finally:
        definition = action = task = service = None
        pythoncom.CoUninitialize()


def consume_environment(directory: Path) -> None:
    import os
    import win32crypt
    path = directory / "startup.env.dpapi"
    if path.exists():
        try:
            environment = json.loads(win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1])
            os.environ.clear()
            os.environ.update(environment)
        finally:
            path.unlink(missing_ok=True)
