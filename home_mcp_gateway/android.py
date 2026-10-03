"""Explicit-device ADB helpers; screenshots preserve binary exec-out bytes."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import Any

from mcp.types import CallToolResult

from .core import _decode
from .qa_common import png_result


def _adb(args: list[str], serial: str | None = None, adb_path: str | None = None,
         timeout_sec: float = 30) -> dict:
    requested = adb_path or os.environ.get("ADB_PATH") or "adb"
    exe = shutil.which(requested)
    if not exe:
        return {"ok": False, "error": "adb_not_found", "message": "ADB not found. Install Android SDK platform-tools, add it to PATH, or pass adb_path / set ADB_PATH.", "requested_path": requested}
    argv = [exe] + (["-s", serial] if serial else []) + args
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_sec,
                              **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        return {"ok": proc.returncode == 0, "serial": serial, "command": argv, "returncode": proc.returncode,
                "stdout": _decode(proc.stdout), "stderr": _decode(proc.stderr), "raw": proc.stdout}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "adb_timeout", "serial": serial, "timeout_sec": timeout_sec,
                "stdout": _decode(exc.stdout or b""), "stderr": _decode(exc.stderr or b"")}
    except OSError as exc:
        return {"ok": False, "error": "adb_launch_failed", "message": str(exc), "requested_path": requested}


def _public(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "raw"}


def adb_devices(adb_path: str | None = None) -> dict[str, Any]:
    """List ADB serials, device/offline/unauthorized state and model/transport details. Pass serial to subsequent tools."""
    result = _public(_adb(["devices", "-l"], adb_path=adb_path))
    if result["ok"]:
        devices = []
        for line in result["stdout"].splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] != "List" and not fields[0].startswith("*"):
                devices.append({"serial": fields[0], "state": fields[1], "details": " ".join(fields[2:])})
        result["devices"] = devices
    return result


def adb_shell(command: str, serial: str | None = None, timeout_sec: float = 30, adb_path: str | None = None) -> dict[str, Any]:
    """Run an arbitrary Android shell command. Specify serial with multiple devices; ADB reports ambiguity/offline/auth failures."""
    return _public(_adb(["shell", command], serial, adb_path, timeout_sec))


def adb_screenshot(serial: str | None = None, adb_path: str | None = None) -> CallToolResult:
    """Capture Android PNG directly as MCP ImageContent with dimensions using adb exec-out screencap -p."""
    result = _adb(["exec-out", "screencap", "-p"], serial, adb_path)
    if not result["ok"]:
        from mcp.types import TextContent
        import json
        diagnostic = _public(result)
        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(diagnostic))], structuredContent=diagnostic)
    return png_result(result["raw"], serial=serial, source="adb exec-out screencap")


def adb_logcat(serial: str | None = None, lines: int = 500, filter_spec: str = "", adb_path: str | None = None) -> dict[str, Any]:
    """Read a finite logcat snapshot (last lines), optionally filtered, e.g. 'ActivityManager:I *:S'. Use job_start for continuous recording."""
    if lines <= 0:
        raise ValueError("lines must be positive")
    return _public(_adb(["logcat", "-d", "-t", str(lines), *shlex.split(filter_spec)], serial, adb_path))


def adb_tap(x: int, y: int, serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Tap an Android display coordinate on the selected serial."""
    return adb_shell(f"input tap {x} {y}", serial, adb_path=adb_path)


def adb_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Swipe between Android display coordinates over duration_ms."""
    return adb_shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", serial, adb_path=adb_path)


def _gboard_enabled_english_subtype(
    enabled_input_methods: str,
    ime_details: str,
    latin_ime: str,
) -> str | None:
    """Resolve an enabled English Gboard subtype without changing user settings."""
    enabled_hashes: set[str] = set()
    for entry in enabled_input_methods.strip().split(":"):
        parts = entry.split(";")
        if parts and parts[0] == latin_ime:
            enabled_hashes.update(part for part in parts[1:] if part)
            break
    if not enabled_hashes:
        return None

    english_hashes: list[str] = []
    in_latin = False
    for line in ime_details.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and "/" in stripped and not stripped.startswith("m"):
            in_latin = stripped[:-1] == latin_ime
            continue
        if not in_latin:
            continue
        if (
            ("mSubtypeLocale=en_US" in stripped or "mSubtypeLanguageTag=en-US" in stripped)
            and "mIsAsciiCapable=true" in stripped
        ):
            marker = "mSubtypeHashCode="
            if marker in stripped:
                value = stripped.split(marker, 1)[1].split()[0].strip()
                if value in enabled_hashes:
                    english_hashes.append(value)
    return english_hashes[0] if len(set(english_hashes)) == 1 else None


def adb_text(text: str, serial: str | None = None, adb_path: str | None = None,
             exact: bool = False) -> dict[str, Any]:
    """Type text on Android.

    Default mode preserves Android's normal input-text behavior. With exact=True
    the Gateway temporarily selects an enabled English Gboard subtype, types the
    value, then restores the user's original IME/subtype. This prevents Japanese
    IMEs (including Gboard's Japanese subtype) from transliterating ASCII QA
    strings. If a unique enabled English Gboard subtype cannot be proven, the
    exact operation refuses to type rather than guessing.
    """
    command = "input text " + shlex.quote(text.replace(" ", "%s"))
    if not exact:
        return adb_shell(command, serial, adb_path=adb_path)

    latin_ime = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
    current = adb_shell(
        "settings get secure default_input_method",
        serial,
        adb_path=adb_path,
    )
    if not current.get("ok"):
        return {**current, "exact": True, "error": "exact_text_ime_query_failed"}
    original_ime = str(current.get("stdout", "")).strip()

    original_subtype_result = adb_shell(
        "settings get secure selected_input_method_subtype",
        serial,
        adb_path=adb_path,
    )
    original_subtype = (
        str(original_subtype_result.get("stdout", "")).strip()
        if original_subtype_result.get("ok")
        else ""
    )
    if original_ime == latin_ime and not original_subtype_result.get("ok"):
        return {
            **original_subtype_result,
            "exact": True,
            "error": "exact_text_subtype_query_failed",
        }

    installed = adb_shell("ime list -s", serial, adb_path=adb_path)
    if not installed.get("ok"):
        return {**installed, "exact": True, "error": "exact_text_ime_query_failed"}
    installed_imes = {
        line.strip()
        for line in str(installed.get("stdout", "")).splitlines()
        if line.strip()
    }
    if latin_ime not in installed_imes:
        return {
            "ok": False,
            "error": "exact_text_latin_ime_unavailable",
            "serial": serial,
            "exact": True,
            "original_ime": original_ime or None,
        }

    enabled = adb_shell(
        "settings get secure enabled_input_methods",
        serial,
        adb_path=adb_path,
    )
    details = adb_shell("ime list -a", serial, adb_path=adb_path)
    if not enabled.get("ok") or not details.get("ok"):
        failed = enabled if not enabled.get("ok") else details
        return {**failed, "exact": True, "error": "exact_text_ime_query_failed"}

    english_subtype = _gboard_enabled_english_subtype(
        str(enabled.get("stdout", "")),
        str(details.get("stdout", "")),
        latin_ime,
    )
    if not english_subtype:
        return {
            "ok": False,
            "error": "exact_text_english_subtype_unavailable",
            "serial": serial,
            "exact": True,
            "original_ime": original_ime or None,
        }

    switched_ime = original_ime != latin_ime
    selected_english = False
    restore: dict[str, Any] | None = None
    subtype_restore_ok = True
    try:
        if switched_ime:
            switch = adb_shell(
                "ime set " + shlex.quote(latin_ime),
                serial,
                adb_path=adb_path,
            )
            if not switch.get("ok"):
                return {**switch, "exact": True, "error": "exact_text_ime_switch_failed"}
            time.sleep(.25)

        for _ in range(4):
            selected = adb_shell(
                "settings get secure selected_input_method_subtype",
                serial,
                adb_path=adb_path,
            )
            current_method = adb_shell(
                "settings get secure default_input_method",
                serial,
                adb_path=adb_path,
            )
            if not selected.get("ok") or not current_method.get("ok"):
                return {
                    "ok": False,
                    "error": "exact_text_subtype_query_failed",
                    "serial": serial,
                    "exact": True,
                }
            if (
                str(current_method.get("stdout", "")).strip() == latin_ime
                and str(selected.get("stdout", "")).strip() == english_subtype
            ):
                selected_english = True
                break
            if str(current_method.get("stdout", "")).strip() != latin_ime:
                switch = adb_shell(
                    "ime set " + shlex.quote(latin_ime),
                    serial,
                    adb_path=adb_path,
                )
                if not switch.get("ok"):
                    return {**switch, "exact": True, "error": "exact_text_ime_switch_failed"}
                time.sleep(.25)
            language_switch = adb_shell(
                "input keyevent KEYCODE_LANGUAGE_SWITCH",
                serial,
                adb_path=adb_path,
            )
            if not language_switch.get("ok"):
                return {
                    **language_switch,
                    "exact": True,
                    "error": "exact_text_subtype_switch_failed",
                }
            time.sleep(.25)

        if not selected_english:
            return {
                "ok": False,
                "error": "exact_text_english_subtype_not_selected",
                "serial": serial,
                "exact": True,
                "english_subtype": english_subtype,
            }

        result = adb_shell(command, serial, adb_path=adb_path)
        # Let the focused editor commit injected key events before restoring IME.
        time.sleep(.25)
    finally:
        if switched_ime and original_ime and original_ime != "null":
            restore = adb_shell(
                "ime set " + shlex.quote(original_ime),
                serial,
                adb_path=adb_path,
            )
            time.sleep(.15)
        elif not switched_ime and original_subtype == "-1":
            implicit_restore = adb_shell(
                "ime set " + shlex.quote(latin_ime),
                serial,
                adb_path=adb_path,
            )
            subtype_restore_ok = bool(implicit_restore.get("ok"))
            time.sleep(.15)
        elif (
            not switched_ime
            and original_subtype
            and original_subtype != english_subtype
        ):
            subtype_restore_ok = False
            for _ in range(4):
                selected = adb_shell(
                    "settings get secure selected_input_method_subtype",
                    serial,
                    adb_path=adb_path,
                )
                if (
                    selected.get("ok")
                    and str(selected.get("stdout", "")).strip() == original_subtype
                ):
                    subtype_restore_ok = True
                    break
                switched = adb_shell(
                    "input keyevent KEYCODE_LANGUAGE_SWITCH",
                    serial,
                    adb_path=adb_path,
                )
                if not switched.get("ok"):
                    break
                time.sleep(.15)

    result = {
        **result,
        "exact": True,
        "temporary_ime": latin_ime,
        "temporary_subtype": english_subtype,
    }
    if restore is not None:
        result["ime_restored"] = bool(restore.get("ok"))
        if not restore.get("ok") and result.get("ok"):
            result.update(
                ok=False,
                error="exact_text_ime_restore_failed",
                restore=restore,
            )
    elif not subtype_restore_ok and result.get("ok"):
        result.update(
            ok=False,
            error="exact_text_subtype_restore_failed",
        )
    return result


def adb_keyevent(keycode: str, serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Send an Android numeric keycode or name such as KEYCODE_HOME or KEYCODE_BACK."""
    return adb_shell("input keyevent " + shlex.quote(keycode), serial, adb_path=adb_path)


def adb_install(apk_path: str, serial: str | None = None, replace: bool = True, timeout_sec: float = 180, adb_path: str | None = None) -> dict[str, Any]:
    """Install an APK file from the Windows workspace on the selected Android device, optionally replacing the existing package."""
    return _public(_adb(["install", *(["-r"] if replace else []), str(Path(apk_path).expanduser().resolve())], serial, adb_path, timeout_sec))


def adb_start(package: str, activity: str = "", serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Start package/activity with am start -W, or resolve the package's launcher activity if activity is empty."""
    if not activity:
        resolved = adb_shell("cmd package resolve-activity --brief " + shlex.quote(package), serial, adb_path=adb_path)
        if not resolved["ok"]:
            return resolved
        candidates = [line.strip() for line in resolved["stdout"].splitlines() if "/" in line]
        if not candidates:
            return {"ok": False, "error": "launcher_activity_not_found", "resolution": resolved}
        component = candidates[-1]
    else:
        component = activity if "/" in activity else f"{package}/{activity}"
    result = adb_shell("am start -W -n " + shlex.quote(component), serial, adb_path=adb_path)
    if result["ok"] and "Error:" in result["stdout"] + result["stderr"]:
        result.update(ok=False, error="activity_start_failed")
    return result


def adb_stop(package: str, serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Force-stop an Android package on the selected device."""
    return adb_shell("am force-stop " + shlex.quote(package), serial, adb_path=adb_path)


def adb_package_info(package: str, serial: str | None = None, adb_path: str | None = None) -> dict[str, Any]:
    """Return Android dumpsys package details, including version, permissions and activities."""
    return adb_shell("dumpsys package " + shlex.quote(package), serial, adb_path=adb_path)


TOOLS = [adb_devices, adb_shell, adb_screenshot, adb_logcat, adb_tap, adb_swipe, adb_text, adb_keyevent,
         adb_install, adb_start, adb_stop, adb_package_info]
