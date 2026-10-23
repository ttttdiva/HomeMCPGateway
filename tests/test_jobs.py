import json
import asyncio
import concurrent.futures
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import psutil
from home_mcp_gateway import jobs


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
        job = jobs.job_start([sys.executable, "-c", "import os; print(os.environ['QA_CUSTOM_VALUE'])"],
                             env={"QA_CUSTOM_VALUE": "日本語 value"}, runtime_dir=self.runtime)
        self.ids.append(job["job_id"])
        self.assertEqual(jobs.job_wait(job["job_id"], 10, self.runtime)["exit_code"], 0)
        self.assertIn("日本語 value", jobs.job_tail(job["job_id"], runtime_dir=self.runtime)["stdout"])
        self.assertFalse((jobs._directory(job["job_id"], self.runtime) / "startup.env.dpapi").exists())

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
        with patch.object(Path, 'read_text', side_effect=[PermissionError('sharing'), '{"status":"exited"}']) as read, \
             patch.object(jobs.time, 'sleep') as sleep:
            self.assertEqual(jobs._read(Path('metadata.json'))['status'], 'exited')
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(.025)

    def test_metadata_read_persistent_permission_error_is_not_hidden(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('denied')) as read, \
             patch.object(jobs.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                jobs._read(Path('metadata.json'))
        self.assertEqual(read.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

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
