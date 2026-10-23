"""Offline stdio fixture. Never imports or invokes Codex or a model provider."""
import json
from pathlib import Path
import sys
import time
import subprocess


mode, spec_json, capture_path = sys.argv[1:]
spec = json.loads(spec_json)
capture = Path(capture_path)


def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def turn(status):
    return {"id": "turn-1", "status": status, "items": []}


def complete(status="completed"):
    if status == "completed":
        send({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 1,
              "item": {"type": "agentMessage", "id": "assistant-item", "text": "Offline fixture result."}}})
    send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": turn(status)}})
    if mode in {"late_invalid", "stop_late_invalid"}:
        time.sleep(.05)
        send({"id": 999, "result": {}})


for line in sys.stdin:
    frame = json.loads(line)
    with capture.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(frame) + "\n")
    method = frame.get("method")
    if method == "initialize":
        if mode == "invalid":
            print("not json", flush=True)
            continue
        if mode == "huge":
            print("x" * (1024 * 1024 + 1), flush=True)
            continue
        send({"id": frame["id"], "result": {"userAgent": "offline-fixture"}})
    elif method == "thread/start":
        result = {"thread": {"id": "thread-1"}, "model": spec["model"], "modelProvider": spec["model_provider"],
                  "cwd": spec["cwd"], "approvalPolicy": "never", "approvalsReviewer": "user",
                  "sandbox": {"type": "readOnly", "networkAccess": False}, "reasoningEffort": spec["effort"]}
        if mode.startswith("mismatch:"):
            key = mode.partition(":")[2]
            if key == "reasoningEffort_missing":
                result.pop("reasoningEffort")
            elif key == "networkAccess":
                result["sandbox"]["networkAccess"] = True
            elif key == "sandbox":
                result["sandbox"] = {"type": "workspaceWrite"}
            elif key == "sandboxextra":
                result["sandbox"]["writeAccess"] = True
            else:
                result[key] = None if key == "reasoningEffort" else "WRONG"
        if mode.startswith("missing:"):
            result.pop(mode.partition(":")[2])
        if mode == "network_omitted":
            result["sandbox"].pop("networkAccess")
        send({"method": "thread/started", "params": {"thread": result["thread"]}})
        send({"id": frame["id"], "result": result})
    elif method == "turn/start":
        send({"method": "turn/started", "params": {"threadId": "thread-1", "turn": turn("inProgress")}})
        send({"id": frame["id"], "result": {"turn": turn("inProgress")}})
        if mode == "crash":
            sys.exit(7)
        if mode == "auth":
            print("SECRET-STDERR-TOKEN", file=sys.stderr, flush=True)
            send({"id": "auth-1", "method": "account/chatgptAuthTokens/refresh",
                  "params": {"refreshToken": "SECRET-AUTH-TOKEN"}})
        elif mode == "attestation":
            send({"id": "auth-1", "method": "attestation/generate", "params": {"token": "SECRET-AUTH-TOKEN"}})
        elif mode == "benign_account":
            send({"method": "account/updated", "params": {"privateAccount": "SECRET-ACCOUNT-PARAMETER"}})
            send({"method": "account/rateLimits/updated", "params": {"privateLimit": "SECRET-RATELIMIT-PARAMETER"}})
            complete()
        elif mode == "requests":
            for number, request_method in enumerate([
                    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                    "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval",
                    "mcpServer/elicitation/request", "item/tool/requestUserInput", "item/tool/call", "unknown/request"]):
                send({"id": f"server-{number}", "method": request_method, "params": {}})
        elif mode == "orphan":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}))
            with capture.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"fixture_child_pid": child.pid}) + "\n")
            complete()
        elif mode in {"completed", "network_omitted", "force", "late_invalid"}:
            complete()
        elif mode == "duplicate_response":
            send({"id": frame["id"], "result": {"turn": turn("inProgress")}})
    elif method == "turn/steer":
        states = list((capture.parent / "steering" / "states").glob("*.json"))
        durable = any((value := json.loads(path.read_text())).get("state") == "sending" and value.get("rpc_id") == frame["id"] for path in states)
        with capture.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"fixture_durable_sending": durable}) + "\n")
        if mode == "steer_crash":
            sys.exit(9)
        if mode == "missing_steer":
            complete()
            continue
        if mode == "steer_error":
            send({"id": frame["id"], "error": {"code": -32000, "message": "Turn is not steerable"}})
        else:
            if mode == "late_steer":
                complete()
                time.sleep(.05)
            send({"id": frame["id"], "result": {"turnId": "wrong" if mode == "steer_wrong" else "turn-1"}})
            client_id = "unrelated" if mode == "steer_unrecorded" else frame["params"]["clientUserMessageId"]
            send({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "completedAtMs": 1,
                  "item": {"type": "userMessage", "id": "user-item", "clientId": client_id,
                           "content": frame["params"]["input"]}}})
        if mode != "late_steer":
            complete()
    elif method == "turn/interrupt":
        if mode == "stop_no_reply":
            continue
        send({"id": frame["id"], "result": {}})
        complete("interrupted")
    elif frame.get("id") == "auth-1":
        break
    elif mode == "requests" and frame.get("id") == "server-8":
        complete()

if mode == "force":
    time.sleep(60)
