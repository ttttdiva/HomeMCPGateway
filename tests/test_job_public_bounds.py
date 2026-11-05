import json
from unittest.mock import patch

from home_mcp_gateway import jobs
from home_mcp_gateway.work_dispatcher_codex import GatewayJobAdapter


def test_large_metadata_is_bounded_and_audit_and_retry_are_intact(tmp_path):
    command = ["synthetic", "a" * 10000]
    git = {"status": "x" * 5200000, "head": "abc123", "branch": "main",
           "dirty": True, "repository_root": "D:/synthetic"}
    binding = {"nested": "y" * 10000}

    def fake_launch(directory, _env, metadata):
        jobs._save(directory / "metadata.json", {**metadata, "status": "exited", "exit_code": 0})

    with patch("home_mcp_gateway.git_qa.git_status", return_value=git), \
         patch("home_mcp_gateway.windows_job_launch.launch", side_effect=fake_launch), \
         patch.object(jobs, "_dispatcher_binding_valid", return_value=True):
        kwargs = dict(cwd=str(tmp_path), runtime_dir=str(tmp_path), idempotency_key="synthetic-key",
                      dispatcher_binding=binding)
        first = jobs.job_start(command, **kwargs)
        retry = jobs.job_start(command, **kwargs)

    job_id = first["job_id"]
    path = tmp_path / "jobs" / job_id / "metadata.json"
    before = path.read_bytes()
    original = json.loads(before)
    assert original["git_at_start"] == git
    assert original["command"] == command
    assert original["dispatcher_binding"] == binding
    assert first["status"] == retry["status"] == "exited"
    assert first["job_id"] == retry["job_id"]
    for result in (first, retry, jobs.job_status(job_id, str(tmp_path)),
                   jobs.job_wait(job_id, 0, str(tmp_path)), jobs.job_tail(job_id, runtime_dir=str(tmp_path))):
        assert len(json.dumps(result).encode()) < 20000
        assert set(result["metadata_truncated_fields"]) >= {"git_at_start", "command", "dispatcher_binding"}
        assert result["git_at_start"]["status"]["truncated"] is True
        for key in ("head", "branch", "dirty", "repository_root"):
            assert result["git_at_start"][key] == git[key]
    restored = GatewayJobAdapter(str(tmp_path)).status(job_id)
    assert restored["command"] == command
    assert restored["dispatcher_binding"] == binding
    assert path.read_bytes() == before

    # Multi-job listing uses the same projection without changing audit records.
    second_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    second_dir = tmp_path / "jobs" / second_id
    second_dir.mkdir()
    jobs._save(second_dir / "metadata.json", {**original, "job_id": second_id})
    listing = jobs.job_list(str(tmp_path))
    assert len(listing["jobs"]) == 2
    assert listing["jobs_omitted"] == 0
    assert len(json.dumps(listing).encode()) < 40000
    assert path.read_bytes() == before


def test_stop_without_worker_preserves_full_audit_metadata(tmp_path):
    job_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    directory = tmp_path / "jobs" / job_id
    directory.mkdir(parents=True)
    original = {"job_id": job_id, "status": "running", "pid": 123, "process_create_time": 1,
                "worker_pid": 456, "worker_create_time": 2, "started_at": 1, "finished_at": None,
                "command": ["x" * 10000], "dispatcher_binding": {"data": "y" * 10000},
                "git_at_start": {"head": "abc", "branch": "main", "dirty": True,
                                 "repository_root": "D:/synthetic", "status": "z" * 5200000}}
    path = directory / "metadata.json"
    jobs._save(path, original)
    with patch.object(jobs, "identity", side_effect=["alive", "missing", "missing"]), \
         patch.object(jobs, "_terminate_tree", return_value={"identity": "alive", "affected_pids": [123]}) as terminate:
        result = jobs.job_stop(job_id, runtime_dir=str(tmp_path))
    terminate.assert_called_once_with(123, 1, True)
    stored = json.loads(path.read_bytes())
    for key in ("command", "dispatcher_binding", "git_at_start"):
        assert stored[key] == original[key]
    assert stored["status"] == result["status"] == "stopped"
    assert stored["finished_at"] == result["finished_at"]
    assert len(json.dumps(result).encode()) < 20000
    assert result["git_at_start"]["status"]["truncated"] is True


def test_job_list_reports_omitted_jobs(tmp_path):
    for n in range(80):
        directory = tmp_path / "jobs" / f"{n:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        directory.mkdir(parents=True)
        (directory / "metadata.json").touch()
    def status(job_id, runtime_dir):
        return {"job_id": job_id, "started_at": 1, "status": "exited", "summary": "x" * 8000}
    with patch.object(jobs, "job_status", side_effect=status):
        result = jobs.job_list(str(tmp_path))
    assert result["jobs_omitted"] > 0
    assert len(result["jobs"]) + result["jobs_omitted"] == 80
    assert len(json.dumps(result).encode()) < 530000
