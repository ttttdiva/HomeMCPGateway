"""Disposable, fixed-target Windows Graphics Capture worker and bounded IPC."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zlib

from PIL import Image

MAX_PNG = 16 * 1024 * 1024
MAX_OUTPUT = (MAX_PNG * 4 // 3) + 4096
MAX_SIDE = 8192
MAX_PIXELS = 16_777_216


def _valid_target(hwnd, pid):
    return (type(hwnd) is int and 0 < hwnd < 2**64 and
            type(pid) is int and 0 < pid < 2**32)


def _check_png(raw):
    # Pillow accepts some files missing IEND; require complete, CRC-correct chunks.
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    pos = 8
    while pos + 12 <= len(raw):
        size = int.from_bytes(raw[pos:pos + 4], "big")
        end = pos + 12 + size
        if end > len(raw):
            raise ValueError("truncated PNG")
        kind = raw[pos + 4:pos + 8]
        if zlib.crc32(raw[pos + 4:end - 4]) != int.from_bytes(raw[end - 4:end], "big"):
            raise ValueError("invalid PNG checksum")
        pos = end
        if kind == b"IEND":
            if size or pos != len(raw):
                raise ValueError("invalid PNG end")
            return
    raise ValueError("missing PNG end")


def request(hwnd: int, expected_pid: int, timeout: float = 3.0) -> Image.Image:
    if not _valid_target(hwnd, expected_pid) or not 0 < timeout <= 30:
        raise ValueError("invalid WGC worker request")
    return _run([sys.executable, "-B", str(Path(__file__).resolve()), str(hwnd), str(expected_pid)],
                hwnd, expected_pid, timeout)


def _job_api():
    import ctypes
    from ctypes import wintypes as w

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [w.HANDLE, w.HANDLE], w.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    kernel.PeekNamedPipe.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    kernel.PeekNamedPipe.restype = w.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("job creation failed")
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise OSError("job configuration failed")
    return kernel, job


def _run(command, hwnd, pid, timeout):
    """Run fixed Python worker behind a job-assignment handshake; test seam is private."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    # The bootstrap cannot execute worker/fixture code or spawn descendants until
    # the parent assigns this process to the kill-on-close job and releases it.
    bootstrap = ("import sys; "
                 "sys.stdin.buffer.read(1) == b'G' or sys.exit(1); "
                 "import site; site.main(); import runpy; "
                 "a=sys.argv[1:]; sys.argv=a[1:] if a[0]=='-c' else a; "
                 "exec(a[1], {'__name__':'__main__'}) if a[0]=='-c' else runpy.run_path(a[0],run_name='__main__')")
    args = [sys.executable, "-B", "-S", "-c", bootstrap] + command[2:]
    deadline = time.monotonic() + timeout
    data = bytearray()
    child = None
    job = None
    kernel = None
    overflow = False
    code = None
    try:
        kernel, job = _job_api()
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        if not kernel.AssignProcessToJobObject(job, int(child._handle)):
            raise RuntimeError("WGC worker containment failed")
        child.stdin.write(b'G')
        child.stdin.close()
        pipe = msvcrt.get_osfhandle(child.stdout.fileno())
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError("WGC worker timed out")
            available = wintypes.DWORD()
            ok = kernel.PeekNamedPipe(pipe, None, 0, None, ctypes.byref(available), None)
            if not ok and ctypes.get_last_error() != 109:  # ERROR_BROKEN_PIPE
                raise RuntimeError("WGC worker output failed")
            if available.value:
                chunk = os.read(child.stdout.fileno(), min(available.value, 65536))
                if len(data) + len(chunk) > MAX_OUTPUT:
                    overflow = True
                    break
                data.extend(chunk)
                continue
            code = child.poll()
            if code is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("WGC worker timed out")
            try:
                child.wait(timeout=min(remaining, .02))
            except subprocess.TimeoutExpired:
                pass
    except OSError as exc:
        raise RuntimeError("WGC worker launch failed") from exc
    finally:
        # Closing the job kills the complete owned tree, including descendants
        # retaining stdout. No reader thread or blocking pipe drain is needed.
        if job:
            kernel.CloseHandle(job)
        if child:
            try:
                if child.poll() is None:
                    try:
                        child.kill()  # also covers failure before job assignment
                    except ProcessLookupError:
                        pass
                child.wait(timeout=2)
            finally:
                if child.stdin and not child.stdin.closed:
                    child.stdin.close()
                child.stdout.close()
    if overflow or code != 0:
        raise RuntimeError("WGC worker failed")
    try:
        obj = json.loads(data)
        if (type(obj) is not dict or set(obj) != {"hwnd", "pid", "png"} or
                type(obj["hwnd"]) is not int or type(obj["pid"]) is not int or
                obj["hwnd"] != hwnd or obj["pid"] != pid or type(obj["png"]) is not str):
            raise ValueError("invalid metadata")
        raw = base64.b64decode(obj["png"], validate=True)
        if len(raw) > MAX_PNG:
            raise ValueError("oversize PNG")
        _check_png(raw)
        with Image.open(io.BytesIO(raw), formats=["PNG"]) as im:
            if (im.format != "PNG" or im.mode != "RGB" or
                    not 0 < im.width <= MAX_SIDE or not 0 < im.height <= MAX_SIDE or
                    im.width * im.height > MAX_PIXELS):
                raise ValueError("invalid image")
            im.load()
            return im.copy()
    except Exception as exc:
        raise RuntimeError("WGC worker returned invalid payload") from exc


def _worker(hwnd, pid):
    import ctypes
    from ctypes import wintypes
    import numpy
    from windows_capture import WindowsCapture

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD

    def matches():
        owner = wintypes.DWORD()
        return bool(user.IsWindow(hwnd) and user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)) and owner.value == pid)

    if not matches():
        raise RuntimeError("target changed")
    done, box = threading.Event(), {}
    capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

    @capture.event
    def on_frame_arrived(frame, control):
        try:
            array = frame.frame_buffer
            height, width = array.shape[:2]
            if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and width * height <= MAX_PIXELS):
                raise ValueError("invalid dimensions")
            box["image"] = Image.fromarray(numpy.ascontiguousarray(array[:, :, 2::-1]), "RGB")
        finally:
            done.set()
            control.stop()

    @capture.event
    def on_closed():
        done.set()

    control = capture.start_free_threaded()
    try:
        if not done.wait(3.0):
            raise RuntimeError("no frame")
    finally:
        control.stop()
        control.wait()
    if "image" not in box or not matches():
        raise RuntimeError("capture failed or target changed")
    out = io.BytesIO()
    box["image"].save(out, format="PNG")
    raw = out.getvalue()
    if len(raw) > MAX_PNG:
        raise ValueError("oversize PNG")
    sys.stdout.write(json.dumps({"hwnd": hwnd, "pid": pid, "png": base64.b64encode(raw).decode("ascii")}))


if __name__ == "__main__":
    try:
        hwnd, pid = map(int, sys.argv[1:])
        if not _valid_target(hwnd, pid):
            raise ValueError("invalid target")
        _worker(hwnd, pid)
    except Exception:
        sys.exit(1)
