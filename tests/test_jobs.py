import json
import asyncio
import concurrent.futures
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch
import uuid

import psutil
from home_mcp_gateway import job_worker, jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = self.temp.name
        self.ids = []

    def tearDown(self):
        for job_id in self.ids:
            state = jobs.job_stop(job_id, runtime_dir=self.runtime)
            end = time.monotonic() + 5
            while jobs.identity(state.get("worker_pid"), state.get("worker_create_time")) == "alive" and time.monotonic() < end:
                time.sleep(.05)
        self.temp.cleanup()

    def start(self, code):
        job = jobs.job_start([sys.executable, "-u", "-c", code], cwd=self.runtime, runtime_dir=self.runtime)
        self.ids.append(job["job_id"])
        return job

    def test_start_logs_exit_and_persistence(self):
        job = self.start("import sys; print('stdout marker'); print('stderr marker', file=sys.stderr); sys.exit(7)")
        result = jobs.job_wait(job["job_id"], 10, self.runtime)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["exit_code"], 7)
        self.assertIsNotNone(result["process_create_time"])
        tails = jobs.job_tail(job["job_id"], runtime_dir=self.runtime)
        self.assertIn("stdout marker", tails["stdout"])
        self.assertIn("stderr marker", tails["stderr"])
        disk = json.loads((Path(self.runtime) / "jobs" / job["job_id"] / "metadata.json").read_text())
        self.assertEqual(disk["exit_code"], 7)
        self.assertEqual(jobs.job_list(self.runtime)["jobs"][0]["job_id"], job["job_id"])

    def test_wait_timeout_and_stop_descendants(self):
        code = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); print(child.pid, flush=True); time.sleep(120)"
        job = self.start(code)
        timed_out = jobs.job_wait(job["job_id"], .1, self.runtime)
        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(timed_out["status"], "running")
        self.assertGreaterEqual(timed_out["elapsed_sec"], 0)
        self.assertIn("last_output", timed_out)
        self.assertIn("last_output_at", timed_out)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            output = jobs.job_tail(job["job_id"], runtime_dir=self.runtime)["stdout"].strip()
            if output:
                break
            time.sleep(.05)
        child_pid = int(output)
        child_time = psutil.Process(child_pid).create_time()
        result = jobs.job_stop(job["job_id"], runtime_dir=self.runtime)
        self.assertEqual(result["status"], "stopped")
        self.assertNotEqual(jobs.identity(child_pid, child_time), "alive")
        self.assertNotEqual(jobs.identity(job["pid"], job["process_create_time"]), "alive")

    def test_survives_starting_gateway_process_exit(self):
        code = "from home_mcp_gateway.jobs import job_start; import json,sys; print(json.dumps(job_start([sys.executable,'-u','-c',\"import time; print('survived'); time.sleep(1); print('done')\"], runtime_dir=sys.argv[1])))"
        result = subprocess.check_output([sys.executable, "-c", code, self.runtime], timeout=15)
        job = json.loads(result)
        self.ids.append(job["job_id"])
        restored = jobs.job_wait(job["job_id"], 10, self.runtime)
        self.assertEqual(restored["exit_code"], 0)
        self.assertIn("done", jobs.job_tail(job["job_id"], runtime_dir=self.runtime)["stdout"])

    def test_pid_reuse_does_not_signal_other_process(self):
        job_id = str(uuid.uuid4())
        directory = jobs._directory(job_id, self.runtime)
        directory.mkdir(parents=True)
        jobs._save(directory / "metadata.json", {"job_id": job_id, "status": "running", "pid": os.getpid(),
            "process_create_time": psutil.Process().create_time() - 100, "worker_pid": None,
            "worker_create_time": None, "started_at": time.time(), "exit_code": None})
        with patch.object(psutil.Process, "kill") as kill:
            self.assertEqual(jobs.job_status(job_id, self.runtime)["status"], "lost")
            self.assertEqual(jobs.job_stop(job_id, runtime_dir=self.runtime)["process_identity"], "pid_reused")
            self.assertEqual(jobs._terminate_tree(os.getpid(), psutil.Process().create_time() - 100)["identity"], "pid_reused")
            kill.assert_not_called()

    def test_failed_launch_is_persisted(self):
        job = jobs.job_start(["__missing_job_executable__"], runtime_dir=self.runtime)
        self.ids.append(job["job_id"])
        result = jobs.job_wait(job["job_id"], 5, self.runtime)
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result)

    def test_job_environment_handoff(self):
        code = (
            "import json,os; print(json.dumps({"
            "'explicit_present': bool(os.environ.get('QA_CUSTOM_VALUE')),"
            "'dummy_key_present': 'DUMMY_API_KEY' in os.environ,"
            "'path_present': bool(os.environ.get('PATH'))}))"
        )
        with patch.dict(
            os.environ,
            {
                "DUMMY_API_KEY": "unique-parent-key-sentinel",
                "HOME_MCP_JOB_ENV_ALLOW": "DUMMY_API_KEY*",
            },
            clear=False,
        ):
            job = jobs.job_start(
                [sys.executable, "-c", code],
                env={"QA_CUSTOM_VALUE": "unique-explicit-env-sentinel"},
                runtime_dir=self.runtime,
            )
        self.ids.append(job["job_id"])
        self.assertEqual(jobs.job_wait(job["job_id"], 10, self.runtime)["exit_code"], 0)
        output = json.loads(jobs.job_tail(job["job_id"], runtime_dir=self.runtime)["stdout"])
        self.assertTrue(output["explicit_present"])
        self.assertFalse(output["dummy_key_present"])
        self.assertTrue(output["path_present"])
        self.assertFalse((jobs._directory(job["job_id"], self.runtime) / "startup.env.dpapi").exists())
        metadata_path = jobs._directory(job["job_id"], self.runtime) / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["env_policy"], "allowlist-v1")
        self.assertGreater(metadata["env_inherited_count"], 0)
        self.assertGreater(metadata["env_dropped_count"], 0)
        self.assertIn("DUMMY_API_KEY", metadata["env_dropped_secret_like_names"])
        serialized_metadata = metadata_path.read_text(encoding="utf-8")
        self.assertNotIn("unique-parent-key-sentinel", serialized_metadata)
        self.assertNotIn("unique-explicit-env-sentinel", serialized_metadata)
        if os.name == "nt":
            self.assertEqual(metadata["launch_method"], "windows_interactive_task")

    def test_scheduler_environment_missing_fails_closed(self):
        from home_mcp_gateway.windows_job_launch import consume_environment

        directory = Path(self.runtime) / "scheduled-job"
        directory.mkdir()
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "unique-ambient-sentinel", "PATH": "ambient-path"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "^scheduler_environment_unavailable$"):
                consume_environment(directory)
            self.assertEqual(dict(os.environ), {})

    def test_scheduler_worker_does_not_launch_when_environment_handoff_is_missing(self):
        job_id = str(uuid.uuid4())
        directory = jobs._directory(job_id, self.runtime)
        directory.mkdir(parents=True)
        jobs._save(directory / "metadata.json", {
            "job_id": job_id,
            "status": "starting",
            "pid": None,
            "process_create_time": None,
            "worker_pid": None,
            "worker_create_time": None,
            "started_at": time.time(),
            "finished_at": None,
            "exit_code": None,
            "command": [sys.executable, "-c", "print('must-not-run')"],
            "cwd": self.runtime,
            "stdout_path": str(directory / "stdout.log"),
            "stderr_path": str(directory / "stderr.log"),
            "worker_log_path": str(directory / "worker.log"),
            "runtime_dir": self.runtime,
            "scheduler_task": "HomeMCPJob-test-missing-env",
        })
        from home_mcp_gateway import job_worker

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "unique-ambient-sentinel"}, clear=True),
            patch("home_mcp_gateway.windows_job_launch.remove_task") as remove_task,
            patch("home_mcp_gateway.job_worker.subprocess.Popen") as popen,
            patch("home_mcp_gateway.job_worker._write_safe_traceback"),
        ):
            job_worker.main(directory)
            self.assertEqual(dict(os.environ), {})
        remove_task.assert_called_once_with("HomeMCPJob-test-missing-env")
        popen.assert_not_called()
        metadata = jobs._read(directory / "metadata.json")
        self.assertEqual(metadata["status"], "failed")
        self.assertTrue(metadata["scheduler_task_removed"])
        self.assertEqual(metadata["error_detail"]["exception_type"], "RuntimeError")

    def test_job_backed_http_service_readiness(self):
        from home_mcp_gateway.readiness import wait_http, wait_tcp
        job = self.start("from http.server import HTTPServer, SimpleHTTPRequestHandler; s=HTTPServer(('127.0.0.1',0),SimpleHTTPRequestHandler); print(s.server_port,flush=True); s.serve_forever()")
        end = time.monotonic() + 5
        while time.monotonic() < end:
            output = jobs.job_tail(job["job_id"], runtime_dir=self.runtime)["stdout"].strip()
            if output:
                break
            time.sleep(.05)
        port = int(output)
        self.assertTrue(asyncio.run(wait_tcp(port, job_id=job["job_id"], runtime_dir=self.runtime))["ready"])
        self.assertTrue(asyncio.run(wait_http(f"http://127.0.0.1:{port}", job_id=job["job_id"], runtime_dir=self.runtime))["ready"])
        jobs.job_stop(job["job_id"], runtime_dir=self.runtime)
        self.assertEqual(asyncio.run(wait_tcp(port, job_id=job["job_id"], runtime_dir=self.runtime))["reason"], "job_not_running")

    def test_worker_final_metadata_is_reread_after_process_exit(self):
        running = dict(job_id=str(uuid.uuid4()), status='running', pid=123, process_create_time=1,
                       worker_pid=456, worker_create_time=1, started_at=0, exit_code=None)
        exited = {**running, 'status': 'exited', 'exit_code': 7, 'finished_at': 2}
        with patch.object(jobs, '_read', side_effect=[dict(running), exited]) as read, \
             patch.object(jobs, 'identity', return_value='missing'):
            result = jobs.job_status(running['job_id'], self.runtime)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result['status'], 'exited')
        self.assertEqual(result['exit_code'], 7)

    def test_missing_worker_without_final_metadata_remains_lost(self):
        running = dict(job_id=str(uuid.uuid4()), status='running', pid=123, process_create_time=1,
                       worker_pid=456, worker_create_time=1, started_at=0, exit_code=None)
        with patch.object(jobs, '_read', side_effect=lambda path: dict(running)), \
             patch.object(jobs, 'identity', return_value='missing'):
            result = jobs.job_status(running['job_id'], self.runtime)
        self.assertEqual(result['status'], 'lost')
        self.assertIsNone(result['exit_code'])

    def test_metadata_read_retries_transient_sharing_violation(self):
        with patch.object(jobs, "_open_metadata_read", side_effect=[
                 PermissionError("sharing"), nullcontext(io.BytesIO(b'{"status":"exited"}'))
             ]) as read, \
             patch.object(jobs.time, 'sleep') as sleep:
            self.assertEqual(jobs._read(Path('metadata.json'))['status'], 'exited')
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(.025)

    def test_metadata_read_persistent_permission_error_is_not_hidden(self):
        with patch.object(jobs, "_open_metadata_read", side_effect=PermissionError("denied")) as read, \
             patch.object(jobs.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                jobs._read(Path('metadata.json'))
        self.assertEqual(read.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    @unittest.skipUnless(os.name == "nt", "requires Windows file-sharing semantics")
    def test_metadata_reader_allows_delete_access_while_its_handle_is_open(self):
        import ctypes
        from ctypes import wintypes

        directory = Path(self.runtime) / "share-delete"
        directory.mkdir()
        target = directory / "metadata.json"
        target.write_text('{"version":1}', encoding="utf-8")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]

        with jobs._open_metadata_read(target) as stream:
            self.assertEqual(json.loads(stream.read().decode("utf-8")), {"version": 1})
            delete_handle = create_file(
                str(target), 0x00010000, 0x00000001 | 0x00000002 | 0x00000004,
                None, 3, 0x00000080, None,
            )
            self.assertNotEqual(delete_handle, ctypes.c_void_p(-1).value,
                                f"delete-share open failed: winerror={ctypes.get_last_error()}")
            close_handle(delete_handle)

    @unittest.skipUnless(os.name == "nt", "requires Windows file-sharing semantics")
    def test_metadata_save_retries_until_reader_releases_path(self):
        directory = Path(self.runtime) / "transient-sharing"
        directory.mkdir()
        target = directory / "metadata.json"
        target.write_text('{"version":1}', encoding="utf-8")
        reader_opened = threading.Event()
        release_reader = threading.Event()
        reader_errors = []

        def hold_metadata_open():
            try:
                with target.open("r", encoding="utf-8") as stream:
                    json.load(stream)
                    reader_opened.set()
                    release_reader.wait(1.5)
            except Exception as exc:
                reader_errors.append(type(exc).__name__)

        reader = threading.Thread(target=hold_metadata_open, daemon=True)
        reader.start()
        self.assertTrue(reader_opened.wait(2))
        started = time.monotonic()
        try:
            jobs._save(target, {"version": 2})
        finally:
            release_reader.set()
            reader.join(2)
        elapsed = time.monotonic() - started
        self.assertFalse(reader.is_alive())
        self.assertEqual(reader_errors, [])
        self.assertGreaterEqual(elapsed, 1.2)
        self.assertLess(elapsed, 5)
        self.assertEqual(jobs._read(target), {"version": 2})
        self.assertEqual(list(directory.glob(".metadata.json.*.tmp")), [])

    def test_metadata_save_expires_after_ten_second_deadline_and_removes_temp(self):
        directory = Path(self.runtime) / "persistent-sharing"
        directory.mkdir()
        target = directory / "metadata.json"
        now = [0.0]
        sleeps = []

        class SharingViolation(OSError):
            def __init__(self):
                super().__init__(5, "synthetic share conflict")
                self.winerror = 32

        def advance(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        with patch.object(Path, "replace", side_effect=SharingViolation()), \
             patch.object(jobs.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(jobs.time, "sleep", side_effect=advance):
            with self.assertRaises(SharingViolation):
                jobs._save(target, {"version": 1})

        self.assertGreaterEqual(now[0], 10.0)
        self.assertLessEqual(max(sleeps), .25)
        self.assertAlmostEqual(sleeps[0], .025)
        self.assertEqual(list(directory.iterdir()), [])

    def test_keyed_start_replays_same_reservation_and_conflicts_on_payload_change(self):
        command = [sys.executable, "-u", "-c", "print('only-once')"]
        key = "wd2:codex_start:" + "a" * 64
        fingerprint = hashlib.sha256(b"pinned request").hexdigest()
        with patch.dict(os.environ, {"D80_AMBIENT_IDENTITY_TEST": "parent-one"}, clear=False):
            first = jobs.job_start(command, cwd=self.runtime, env={"QA_EXPLICIT": "one"}, runtime_dir=self.runtime,
                                   idempotency_key=key, request_fingerprint=fingerprint)
        self.ids.append(first["job_id"])
        with patch.dict(os.environ, {"D80_AMBIENT_IDENTITY_TEST": "parent-two"}, clear=False):
            second = jobs.job_start(command, cwd=self.runtime, env={"QA_EXPLICIT": "one"}, runtime_dir=self.runtime,
                                    idempotency_key=key, request_fingerprint=fingerprint)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertNotIn("idempotency_conflict", second)
        conflict = jobs.job_start(command, cwd=self.runtime, env={"QA_EXPLICIT": "two"}, runtime_dir=self.runtime,
                                  idempotency_key=key, request_fingerprint=fingerprint)
        self.assertEqual(conflict["error"], "idempotency_conflict")
        changed_command = jobs.job_start([*command[:-1], "print('changed')"], cwd=self.runtime, env={"QA_EXPLICIT": "one"},
                                         runtime_dir=self.runtime, idempotency_key=key, request_fingerprint=fingerprint)
        changed_cwd = jobs.job_start(command, cwd=str(Path(self.runtime) / "other"), env={"QA_EXPLICIT": "one"},
                                     runtime_dir=self.runtime, idempotency_key=key, request_fingerprint=fingerprint)
        changed_request = jobs.job_start(command, cwd=self.runtime, env={"QA_EXPLICIT": "one"}, runtime_dir=self.runtime,
                                         idempotency_key=key, request_fingerprint=hashlib.sha256(b"changed request").hexdigest())
        self.assertEqual(changed_command["error"], "idempotency_conflict")
        self.assertEqual(changed_cwd["error"], "idempotency_conflict")
        self.assertEqual(changed_request["error"], "idempotency_conflict")
        metadata = jobs._read(jobs._directory(first["job_id"], self.runtime) / "metadata.json")
        self.assertEqual(metadata["explicit_env_digest"], jobs._explicit_env_digest({"QA_EXPLICIT": "one"}))
        result = jobs.job_wait(first["job_id"], 10, self.runtime)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(jobs.job_tail(first["job_id"], max_bytes=0, runtime_dir=self.runtime)["stdout"].count("only-once"), 1)

    def test_keyed_concurrent_start_has_one_worker_and_one_process_effect(self):
        command = [sys.executable, "-u", "-c", "import time; print('one-effect',flush=True); time.sleep(.2)"]
        key = "wd2:concurrent:" + "b" * 64
        fingerprint = hashlib.sha256(b"immutable").hexdigest()
        def start():
            return jobs.job_start(command, cwd=self.runtime, runtime_dir=self.runtime, idempotency_key=key,
                                  request_fingerprint=fingerprint)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(first["job_id"], second["job_id"])
        self.ids.append(first["job_id"])
        result = jobs.job_wait(first["job_id"], 10, self.runtime)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(jobs.job_tail(first["job_id"], max_bytes=0, runtime_dir=self.runtime)["stdout"].count("one-effect"), 1)

    def test_keyed_empty_reservation_stays_unknown_without_launch(self):
        key = "wd2:empty:" + "c" * 64
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "home-mcp-gateway-job:" + key))
        jobs._directory(job_id, self.runtime).mkdir(parents=True)
        result = jobs.job_start([sys.executable, "-c", "print('must-not-run')"], cwd=self.runtime,
                                runtime_dir=self.runtime, idempotency_key=key,
                                request_fingerprint=hashlib.sha256(b"pinned").hexdigest())
        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse((jobs._directory(job_id, self.runtime) / "metadata.json").exists())

    def test_duplicate_keyed_worker_bootstrap_cannot_take_over_metadata(self):
        from home_mcp_gateway.job_worker import main
        job_id = str(uuid.uuid4())
        directory = jobs._directory(job_id, self.runtime)
        directory.mkdir(parents=True)
        metadata = {"job_id": job_id, "idempotency_key": "wd2:fence:" + "d" * 64,
                    "request_fingerprint": hashlib.sha256(b"fp").hexdigest(), "dispatcher_request_fingerprint": None,
                    "dispatcher_binding": None, "launch_fence_state": "pending", "command": [sys.executable, "-c", "print('fenced')"],
                    "cwd": self.runtime, "started_at": time.time(), "finished_at": None, "status": "starting", "exit_code": None,
                    "pid": None, "process_create_time": None, "worker_pid": None, "worker_create_time": None,
                    "stdout_path": str(directory / "stdout.log"), "stderr_path": str(directory / "stderr.log"),
                    "runtime_dir": self.runtime}
        jobs._save(directory / "metadata.json", metadata)
        main(directory)
        first = jobs._read(directory / "metadata.json")
        main(directory)
        second = jobs._read(directory / "metadata.json")
        self.assertEqual(first["status"], "exited")
        self.assertEqual(second["worker_pid"], first["worker_pid"])
        self.assertEqual((directory / "stdout.log").read_text().count("fenced"), 1)


class WorkerFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def make_keyed_job(self):
        job_id = str(uuid.uuid4())
        directory = jobs._directory(job_id, self.runtime)
        directory.mkdir(parents=True)
        jobs._save(directory / "metadata.json", {
            "job_id": job_id,
            "idempotency_key": "wd2:worker-failure:" + "e" * 64,
            "request_fingerprint": hashlib.sha256(b"fixture").hexdigest(),
            "dispatcher_request_fingerprint": None,
            "dispatcher_binding": None,
            "launch_fence_state": "pending",
            "command": ["safe-command-placeholder"],
            "cwd": self.runtime,
            "started_at": time.time(),
            "finished_at": None,
            "status": "starting",
            "exit_code": None,
            "pid": None,
            "process_create_time": None,
            "worker_pid": None,
            "worker_create_time": None,
            "stdout_path": str(directory / "stdout.log"),
            "stderr_path": str(directory / "stderr.log"),
            "worker_log_path": str(directory / "worker.log"),
            "runtime_dir": self.runtime,
        })
        return job_id, directory

    def test_two_concurrent_keyed_workers_popen_at_most_once(self):
        _job_id, directory = self.make_keyed_job()
        barrier = threading.Barrier(3)
        errors = []

        class FakeProc:
            pid = 45678

            def __init__(self):
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                return None if self.poll_calls == 1 else 0

            def wait(self):
                return 0

        proc = FakeProc()

        def run_worker():
            try:
                barrier.wait(5)
                job_worker.main(directory)
            except BaseException as exc:
                errors.append(type(exc).__name__)

        workers = [threading.Thread(target=run_worker) for _ in range(2)]
        for worker in workers:
            worker.start()
        with patch.object(job_worker.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(job_worker, "process_create_time", return_value=1.0):
            barrier.wait(5)
            for worker in workers:
                worker.join(5)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        popen.assert_called_once()
        metadata = jobs._read(directory / "metadata.json")
        self.assertEqual(metadata["status"], "exited")
        self.assertTrue((directory / "worker.claim").exists())
        self.assertTrue((directory / "launch.fence").exists())

    def test_consumed_fence_save_failure_writes_sidecar_logs_safe_traceback_and_never_popens(self):
        job_id, directory = self.make_keyed_job()
        real_save = jobs._save
        command_secret = "COMMAND_SECRET_CANARY"
        env_secret = "ENV_SECRET_CANARY"

        def fail_consumed_save(path, data):
            if Path(path).name == "metadata.json" and data.get("launch_fence_state") == "consumed":
                exc = PermissionError(13, f"blocked command={command_secret} env={env_secret}")
                exc.winerror = 5
                raise exc
            real_save(path, data)

        with patch.object(job_worker, "_save", side_effect=fail_consumed_save), \
             patch.object(job_worker.subprocess, "Popen") as popen:
            exit_code = job_worker._run_with_worker_log(directory)

        self.assertEqual(exit_code, 1)
        popen.assert_not_called()
        self.assertTrue((directory / "worker.claim").is_file())
        self.assertTrue((directory / "launch.fence").is_file())
        self.assertEqual(list(directory.glob(".worker.failure.*.tmp")), [])
        sidecar_text = (directory / "worker.failure.json").read_text(encoding="utf-8")
        sidecar = json.loads(sidecar_text)
        self.assertEqual(sidecar["stage"], "launch_fence_consumed_metadata_save")
        self.assertEqual(sidecar["exception_type"], "PermissionError")
        self.assertEqual(sidecar["errno"], 13)
        self.assertEqual(sidecar["winerror"], 5)
        self.assertTrue(sidecar["at_utc"].endswith("Z"))
        self.assertEqual(sidecar["worker_pid"], os.getpid())
        self.assertLessEqual(len(sidecar["message"]), 200)

        worker_log = (directory / "worker.log").read_text(encoding="utf-8")
        metadata_text = (directory / "metadata.json").read_text(encoding="utf-8")
        for secret in (command_secret, env_secret):
            self.assertNotIn(secret, sidecar_text)
            self.assertNotIn(secret, worker_log)
            self.assertNotIn(secret, metadata_text)
        self.assertIn("Traceback (most recent call last):", worker_log)

        with patch.object(jobs, "identity", return_value="missing"):
            result = jobs.job_status(job_id, self.runtime)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "worker_metadata_save_failed")
        self.assertEqual(result["error_detail"]["stage"], "launch_fence_consumed_metadata_save")
        self.assertNotIn(command_secret, json.dumps(result))
        self.assertNotIn(env_secret, json.dumps(result))

    def test_keyed_execution_error_keeps_unknown_code_and_safe_error_detail(self):
        job_id, directory = self.make_keyed_job()
        command_secret = "POpen_COMMAND_SECRET"
        env_secret = "POpen_ENV_SECRET"
        exc = OSError(13, f"cannot execute command={command_secret} env={env_secret}")
        exc.winerror = 5
        with patch.object(job_worker.subprocess, "Popen", side_effect=exc) as popen:
            exit_code = job_worker._run_with_worker_log(directory)

        self.assertEqual(exit_code, 0)  # keyed outcome is persisted as unknown
        popen.assert_called_once()
        metadata_text = (directory / "metadata.json").read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
        self.assertEqual(metadata["status"], "unknown")
        self.assertEqual(metadata["error_code"], "worker_execution_unknown")
        self.assertEqual(metadata["error_detail"]["exception_type"], "PermissionError")
        self.assertEqual(metadata["error_detail"]["errno"], 13)
        self.assertEqual(metadata["error_detail"]["winerror"], 5)
        worker_log = (directory / "worker.log").read_text(encoding="utf-8")
        for secret in (command_secret, env_secret):
            self.assertNotIn(secret, metadata_text)
            self.assertNotIn(secret, worker_log)
        self.assertIn("Traceback (most recent call last):", worker_log)
        self.assertFalse((directory / "worker.failure.json").exists())
