"""Mock OS only; real desktop registration and MCP response serialization."""
from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

from mcp.server import MCPServer
from PIL import Image
from home_mcp_gateway import desktop as d
from home_mcp_gateway.qa_common import register_tools


if __name__ == "__main__":
    user = MagicMock()
    user.GetForegroundWindow.return_value = 123
    bounds = dict(left=0, top=0, right=64, bottom=48, width=64, height=48)
    info = dict(hwnd=123, pid=456, process_create_time=1,
                visible=True, minimized=False, bounds=bounds)
    held_keys = set()

    def send_without_input(_user, events, _hwnd, **_kwargs):
        for event in events:
            if event.ki.dwFlags & d.desktop_input.KEYUP:
                held_keys.discard(event.ki.wVk)
            else:
                held_keys.add(event.ki.wVk)
        return len(events)

    user.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk in held_keys else 0
    user.MapVirtualKeyExW.side_effect = lambda vk, _mode, _layout: vk
    with ExitStack() as stack:
        for name, value in (
            ("_user32", user), ("_physical_pixels", nullcontext()),
            ("_window_info", info), ("_client_bounds", bounds),
            ("list_monitors", {"monitors": [dict(monitor=1, **bounds)]}),
        ):
            stack.enter_context(patch.object(d, name, return_value=value))
        stack.enter_context(patch.object(d.desktop_input, "configure", side_effect=lambda u: u))
        stack.enter_context(patch.object(d, "_approved_unreal_project_process", return_value=True))
        stack.enter_context(patch.object(d, "_is_cloaked", return_value=False))
        stack.enter_context(patch.object(d.desktop_input, "activate"))
        stack.enter_context(patch.object(d.desktop_input, "perform",
                                         return_value={"backend": "SendInput"}))
        stack.enter_context(patch.object(d.desktop_input, "send", side_effect=send_without_input))
        stack.enter_context(patch.object(d.ImageGrab, "grab",
                                         return_value=Image.new("RGB", (64, 48))))
        server = MCPServer("Desktop action contract fixture")
        register_tools(server, d.TOOLS)
        server.run()
