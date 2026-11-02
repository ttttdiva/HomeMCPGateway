# Changelog

## Unreleased

- Add the bounded `desktop_hold_keys` MCP tool for approved Unferat UE game QA. It requires a fresh HWND/PID/process-create-time observation, foreground focus, a project-bound Unreal process, allowlisted movement keys plus explicit left Ctrl (`VK_LCONTROL`), and a finite 100–8000 ms duration. It checks target identity during the hold and returns explicit accepted-key release receipts.
