from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from conftest import assert_error_code, invoke, make_service


@dataclass
class FakeRunner:
    calls: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.result if self.result is not None else {"returncode": 0, "stdout": "ok", "stderr": ""}

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self(*args, **kwargs)


def test_run_check_is_disabled_by_default_and_does_not_call_runner(writable_env):
    runner = FakeRunner()
    service = make_service(writable_env["registry"], writable_env["audit"], check_runner=runner)
    assert_error_code(lambda: invoke(service, "run_check", name="pytest"), "checks_disabled")
    assert not runner.calls


def test_default_runner_is_unavailable_even_when_checks_are_enabled(writable_env):
    service = make_service(writable_env["registry"], writable_env["audit"], checks_enabled=True)
    assert_error_code(lambda: invoke(service, "run_check", name="pytest", args=["tests/example.py", "-q"]), "runner")


def test_run_check_requires_registered_allow_list_and_uses_fixed_argv(writable_env):
    runner = FakeRunner()
    service = make_service(
        writable_env["registry"],
        writable_env["audit"],
        checks_enabled=True,
        check_runner=runner,
    )
    result = invoke(service, "run_check", name="pytest", args=["tests/example.py", "-q", "-x", "--maxfail=1"])
    assert "returncode" in str(result)
    assert runner.calls
    call = runner.calls[-1]
    flat: list[str] = []
    for item in call["args"]:
        if isinstance(item, (list, tuple)):
            flat.extend(str(value) for value in item)
        else:
            flat.append(str(item))
    values = call["kwargs"].get("args", [])
    if isinstance(values, (list, tuple)):
        flat.extend(str(value) for value in values)
    elif values:
        flat.append(str(values))
    assert "pytest" in flat
    assert "tests/example.py" in flat
    assert "-q" in flat and "-x" in flat
    assert all(";" not in item and "|" not in item for item in flat)

    for bad_name in ["sh", "python", "pytest -q", "arbitrary"]:
        assert_error_code(lambda bad_name=bad_name: invoke(service, "run_check", name=bad_name), "check")

    for bad_args in [
        ["-p", "evil_plugin"],
        ["--rootdir", "C:\\outside"],
        ["--import-mode=importlib"],
        ["--confcutdir", "C:\\outside"],
        ["--pyargs", "evil"],
        ["-c", "evil"],
        ["../outside.py"],
        ["frontend/../outside.py"],
        ["tests/example.py", "--timeout=999999"],
    ]:
        assert_error_code(lambda bad_args=bad_args: invoke(service, "run_check", name="pytest", args=bad_args), "check")

    assert_error_code(lambda: invoke(service, "run_check", name="ruff", args=["tests/../outside"]), "check")
    assert_error_code(lambda: invoke(service, "run_check", name="tsc", args=["--noEmit", "src/file.ts"]), "check")


def test_pytest_requires_a_tests_path_and_rejects_options_only(writable_env):
    runner = FakeRunner()
    service = make_service(
        writable_env["registry"],
        writable_env["audit"],
        checks_enabled=True,
        check_runner=runner,
    )
    for args in [[], ["-q"], ["-x"], ["--maxfail=1"], ["--timeout=30"], ["-q", "-x"]]:
        assert_error_code(lambda args=args: invoke(service, "run_check", name="pytest", args=args), "check")
    assert not runner.calls


def test_run_check_allows_only_frontend_tsc_and_no_emit(writable_env):
    # Extend the fixture registry to give tsc an explicit allowed check. The
    # source root remains temporary and no command is actually run.
    import json

    data = json.loads(writable_env["registry"].read_text(encoding="utf-8"))
    data["workspaces"][0]["checks"] = ["tsc"]
    writable_env["registry"].write_text(json.dumps(data), encoding="utf-8")
    frontend = writable_env["root"] / "frontend"
    frontend.mkdir()
    (frontend / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
    runner = FakeRunner()
    service = make_service(
        writable_env["registry"],
        writable_env["audit"],
        checks_enabled=True,
        check_runner=runner,
    )
    result = invoke(service, "run_check", name="tsc", args=["--noEmit", "frontend/app.ts"])
    assert "returncode" in str(result)
    assert_error_code(lambda: invoke(service, "run_check", name="tsc", args=[]), "check")


def test_run_check_is_serialized_to_one_runner_call(writable_env):
    started = threading.Event()
    release = threading.Event()

    class BlockingRunner(FakeRunner):
        def __call__(self, *args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            started.set()
            release.wait(timeout=2)
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

    runner = BlockingRunner()
    service = make_service(
        writable_env["registry"],
        writable_env["audit"],
        checks_enabled=True,
        check_runner=runner,
    )
    first_result: list[Any] = []

    def run_first():
        try:
            first_result.append(invoke(service, "run_check", name="pytest", args=["tests/example.py", "-q"]))
        finally:
            release.set()

    first = threading.Thread(target=run_first)
    first.start()
    assert started.wait(timeout=2)
    assert_error_code(lambda: invoke(service, "run_check", name="pytest", args=["tests/example.py", "-q"]), "busy")
    first.join(timeout=3)
    assert not first.is_alive()
    assert first_result
    assert len(runner.calls) == 1
