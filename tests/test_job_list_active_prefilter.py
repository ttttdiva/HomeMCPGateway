import json
from unittest.mock import patch

from home_mcp_gateway import jobs


def write_job(tmp_path, number, status):
    job_id = f"{number:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = tmp_path / "jobs" / job_id / "metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"job_id": job_id, "status": status, "started_at": number}), encoding="utf-8")
    return job_id, path


def test_active_prefilter_validates_candidates_and_reports_errors(tmp_path):
    terminal = [write_job(tmp_path, n, status)[0] for n, status in enumerate(("exited", "failed", "stopped"), 1)]
    running, _ = write_job(tmp_path, 4, "running")
    lost, _ = write_job(tmp_path, 5, "running")
    changed, _ = write_job(tmp_path, 6, "starting")
    unknown, _ = write_job(tmp_path, 7, "unknown")
    _, malformed = write_job(tmp_path, 8, "running")
    malformed.write_text("{", encoding="utf-8")
    invalid = tmp_path / "jobs" / "invalid" / "metadata.json"
    invalid.parent.mkdir()
    invalid.write_text('{"status":"exited"}', encoding="utf-8")

    def status(job_id, runtime_dir):
        assert job_id not in terminal
        state = {running: "running", lost: "lost", changed: "exited", unknown: "unknown"}[job_id]
        return {"job_id": job_id, "status": state, "started_at": int(job_id[:8], 16)}

    with patch.object(jobs, "job_status", side_effect=status) as check:
        result = jobs.job_list(str(tmp_path), active_only=True)
    assert [item["job_id"] for item in result["jobs"]] == [running]
    assert {call.args[0] for call in check.call_args_list} == {running, lost, changed, unknown}
    assert {error["path"] for error in result["errors"]} == {str(malformed), str(invalid)}
    assert result["jobs_omitted"] == 0


def test_unfiltered_listing_still_calls_status_for_terminal_jobs_and_sorts(tmp_path):
    first, _ = write_job(tmp_path, 1, "exited")
    second, _ = write_job(tmp_path, 2, "running")
    def status(job_id, runtime_dir):
        return {"job_id": job_id, "status": "exited", "started_at": int(job_id[:8], 16)}
    with patch.object(jobs, "job_status", side_effect=status) as check:
        result = jobs.job_list(str(tmp_path), active_only=False)
    assert [item["job_id"] for item in result["jobs"]] == [second, first]
    assert {call.args[0] for call in check.call_args_list} == {first, second}
    assert result["errors"] == []
    assert result["jobs_omitted"] == 0


def test_active_candidates_use_real_identity_validation(tmp_path):
    running, running_path = write_job(tmp_path, 1, "running")
    lost, _ = write_job(tmp_path, 2, "running")
    metadata = json.loads(running_path.read_text(encoding="utf-8"))
    metadata["pid"] = 123
    running_path.write_text(json.dumps(metadata), encoding="utf-8")
    with patch.object(jobs, "identity", side_effect=lambda pid, _: "alive" if pid == 123 else "missing"):
        result = jobs.job_list(str(tmp_path), active_only=True)
    assert [item["job_id"] for item in result["jobs"]] == [running]
    assert lost not in [item["job_id"] for item in result["jobs"]]
