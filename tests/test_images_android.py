import base64
from contextlib import nullcontext
import io
import shlex
import unittest
import subprocess
from unittest.mock import patch, MagicMock

from PIL import Image
from home_mcp_gateway import android, desktop
from home_mcp_gateway.qa_common import image_result


class ImageAndroidTests(unittest.TestCase):
    def assert_png(self, result, size=(32, 24)):
        image = next(block for block in result.content if block.type == "image")
        self.assertEqual(image.mime_type, "image/png")
        data = base64.b64decode(image.data)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
            self.assertEqual(picture.size, size)
        self.assertEqual(result.structured_content["width"], size[0])

    def test_native_png_content(self):
        self.assert_png(image_result(Image.new("RGB", (32, 24), "green")))

    def test_desktop_monitor_and_window_image(self):
        with patch.object(desktop, "_user32"), patch.object(desktop, "_physical_pixels", return_value=nullcontext()), \
             patch.object(desktop, "list_monitors", return_value={"monitors": [{"left": -32, "top": 0, "right": 0, "bottom": 24}]}), \
             patch.object(desktop.ImageGrab, "grab", return_value=Image.new("RGB", (32, 24))) as grab:
            self.assert_png(desktop.screenshot_image(1))
            self.assertEqual(grab.call_args.kwargs["bbox"], (-32, 0, 0, 24))
            self.assert_png(desktop.screenshot_image(0))
            with self.assertRaises(ValueError):
                desktop.screenshot_image(2)
            info = {"pid": 10, "minimized": False, "visible": True, "bounds": {"width": 32, "height": 24}}
            with patch.object(desktop, "_window_info", return_value=info), \
                 patch.object(desktop, "_client_bounds", return_value={"left": 0, "top": 0, "width": 32, "height": 24}):
                self.assert_png(desktop.capture_window(123, 10))
                with self.assertRaises(ValueError):
                    desktop.capture_window(123, 11)
                info["minimized"] = True
                with self.assertRaisesRegex(ValueError, "minimized"):
                    desktop.capture_window(123)

    def test_adb_absent_and_screenshot_error(self):
        with patch.object(android.shutil, "which", return_value=None):
            result = android.adb_devices()
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "adb_not_found")
            self.assertTrue(android.adb_screenshot().is_error)

    def test_serial_and_binary_capture(self):
        raw = io.BytesIO()
        Image.new("RGB", (32, 24)).save(raw, format="PNG")
        with patch.object(android.shutil, "which", return_value="adb"), \
             patch.object(android.subprocess, "run", return_value=MagicMock(returncode=0, stdout=raw.getvalue(), stderr=b"")) as run:
            self.assert_png(android.adb_screenshot("device-2"))
            self.assertEqual(run.call_args.args[0], ["adb", "-s", "device-2", "exec-out", "screencap", "-p"])
            android.adb_text("hello & world", "device-2")
            self.assertEqual(run.call_args.args[0][-1], "input text 'hello%s&%sworld'")

    def test_adb_text_exact_temporarily_uses_english_subtype_and_restores(self):
        latin = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        original = "com.example.ime/.JapaneseIme"
        english = "1594443099"
        state = {"ime": original, "subtype": "1095049687"}
        calls = []

        details = (
            latin + ":\n"
            "  InputMethodSubtype array: count=2\n"
            "    InputMethodSubtype #0:\n"
            "      mSubtypeLocale=ja_JP mSubtypeLanguageTag=ja-JP mSubtypeMode=keyboard "
            "mIsAsciiCapable=true mSubtypeHashCode=328256390\n"
            "    InputMethodSubtype #1:\n"
            "      mSubtypeLocale=en_US mSubtypeLanguageTag=en-US mSubtypeMode=keyboard "
            "mIsAsciiCapable=true mSubtypeHashCode=" + english + "\n"
        )

        def shell(command, serial=None, timeout_sec=30, adb_path=None):
            calls.append(command)
            if command == "settings get secure default_input_method":
                return {"ok": True, "stdout": state["ime"] + "\n", "stderr": ""}
            if command == "settings get secure selected_input_method_subtype":
                return {"ok": True, "stdout": state["subtype"] + "\n", "stderr": ""}
            if command == "ime list -s":
                return {"ok": True, "stdout": original + "\n" + latin + "\n", "stderr": ""}
            if command == "settings get secure enabled_input_methods":
                return {"ok": True, "stdout": original + ":" + latin + ";328256390;" + english + "\n", "stderr": ""}
            if command == "ime list -a":
                return {"ok": True, "stdout": details, "stderr": ""}
            if command == "ime set " + shlex.quote(latin):
                state["ime"] = latin
                state["subtype"] = "-1"
                return {"ok": True, "stdout": "", "stderr": ""}
            if command == "input keyevent KEYCODE_LANGUAGE_SWITCH":
                state["subtype"] = english
                return {"ok": True, "stdout": "", "stderr": ""}
            if command == "ime set " + shlex.quote(original):
                state["ime"] = original
                state["subtype"] = "1095049687"
                return {"ok": True, "stdout": "", "stderr": ""}
            if command.startswith("input text "):
                return {"ok": True, "stdout": "", "stderr": ""}
            raise AssertionError(command)

        with (
            patch.object(android, "adb_shell", side_effect=shell),
            patch.object(android.time, "sleep"),
        ):
            result = android.adb_text("QA exact_0055", "device-2", exact=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["exact"])
        self.assertTrue(result["ime_restored"])
        self.assertEqual(result["temporary_subtype"], english)
        self.assertIn("input keyevent KEYCODE_LANGUAGE_SWITCH", calls)
        self.assertIn("input text QA%sexact_0055", calls)
        self.assertEqual(state, {"ime": original, "subtype": "1095049687"})

    def test_adb_text_exact_refuses_unknown_keyboard_fallback(self):
        with patch.object(android, "adb_shell", side_effect=[
            {"ok": True, "stdout": "third.party/.Ime\n", "stderr": ""},
            {"ok": True, "stdout": "123\n", "stderr": ""},
            {"ok": True, "stdout": "third.party/.Ime\n", "stderr": ""},
        ]):
            result = android.adb_text("safe", exact=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "exact_text_latin_ime_unavailable")

    def test_gboard_english_subtype_must_be_enabled_and_unique(self):
        latin = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
        details = (
            latin + ":\n"
            "  mSubtypeLocale=en_US mSubtypeLanguageTag=en-US mSubtypeMode=keyboard "
            "mIsAsciiCapable=true mSubtypeHashCode=111\n"
            "  mSubtypeLocale=ja_JP mSubtypeLanguageTag=ja-JP mSubtypeMode=keyboard "
            "mIsAsciiCapable=true mSubtypeHashCode=222\n"
        )
        self.assertEqual(
            android._gboard_enabled_english_subtype(latin + ";222;111", details, latin),
            "111",
        )
        self.assertIsNone(
            android._gboard_enabled_english_subtype(latin + ";222", details, latin)
        )

    def test_devices_and_failed_command(self):
        with patch.object(android, "_adb", return_value={"ok": True, "stdout": "List of devices attached\na\tdevice model:Pixel\nb\tunauthorized\n"}):
            result = android.adb_devices()
            self.assertEqual(result["devices"][1]["state"], "unauthorized")

    def test_adb_commands_select_device_and_report_timeout(self):
        with patch.object(android.shutil, "which", return_value="adb"), \
             patch.object(android.subprocess, "run", return_value=MagicMock(returncode=0, stdout=b"ok", stderr=b"")) as run:
            for fn, args in ((android.adb_tap, (10, 20)), (android.adb_swipe, (1, 2, 3, 4)),
                             (android.adb_keyevent, ("KEYCODE_BACK",)), (android.adb_install, ("app.apk",)),
                             (android.adb_stop, ("com.example.qa",)), (android.adb_package_info, ("com.example.qa",)),
                             (android.adb_logcat, ())):
                self.assertTrue(fn(*args, serial="selected")["ok"])
                self.assertEqual(run.call_args.args[0][1:3], ["-s", "selected"])
            android.adb_start("com.example.qa", ".MainActivity", serial="selected")
            self.assertIn("com.example.qa/.MainActivity", run.call_args.args[0][-1])
            run.side_effect = subprocess.TimeoutExpired("adb", 1, output=b"partial")
            self.assertEqual(android.adb_shell("sleep 10")["error"], "adb_timeout")
            run.side_effect = OSError("broken executable")
            self.assertEqual(android.adb_devices()["error"], "adb_launch_failed")

    def test_launcher_resolution(self):
        with patch.object(android, "adb_shell", side_effect=[
            {"ok": True, "stdout": "com.example/.MainActivity\n"},
            {"ok": True, "stdout": "Status: ok", "stderr": ""}]) as shell:
            self.assertTrue(android.adb_start("com.example", serial="selected")["ok"])
            self.assertEqual(shell.call_args.args[0], "am start -W -n com.example/.MainActivity")
