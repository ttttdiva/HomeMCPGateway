"""Mock only OS capture backends; transport, tool registration and SDK remain real."""
from contextlib import ExitStack, nullcontext
from pathlib import Path
import runpy
from unittest.mock import patch

from PIL import Image
from home_mcp_gateway import desktop, android


if __name__ == "__main__":
    with ExitStack() as stack:
        stack.enter_context(patch.object(desktop, "_user32"))
        stack.enter_context(patch.object(desktop, "_physical_pixels", return_value=nullcontext()))
        stack.enter_context(patch.object(desktop, "list_monitors", return_value={"monitors": [
            {"left": 0, "top": 0, "right": 64, "bottom": 48}]}))
        stack.enter_context(patch.object(desktop, "_window_info", return_value={"hwnd": 999, "pid": 123, "minimized": False,
            "visible": True, "bounds": {"left": 0, "top": 0, "right": 64, "bottom": 48, "width": 64, "height": 48}}))
        stack.enter_context(patch.object(desktop, "_client_bounds", return_value={
            "left": 0, "top": 0, "right": 64, "bottom": 48, "width": 64, "height": 48}))
        stack.enter_context(patch.object(desktop.ImageGrab, "grab", return_value=Image.new("RGB", (64, 48), "navy")))
        import io
        out = io.BytesIO()
        Image.new("RGB", (64, 48), "green").save(out, format="PNG")
        original = android._adb
        def adb_backend(args, serial=None, adb_path=None, timeout_sec=30):
            if args == ["exec-out", "screencap", "-p"]:
                return {"ok": True, "raw": out.getvalue()}
            return original(args, serial, adb_path, timeout_sec)
        stack.enter_context(patch.object(android, "_adb", side_effect=adb_backend))
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"), run_name="__main__")
