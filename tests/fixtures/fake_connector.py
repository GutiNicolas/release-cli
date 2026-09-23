"""Fake connector for tests: `python fake_connector.py <behavior> <action>`, protocol 1 on stdin/stdout.

Every call is appended to $FAKE_LOG as one JSON line: {"behavior", "action", "request"}.
"""

import json
import os
import sys
import time

behavior, action = sys.argv[1], sys.argv[-1]
req = json.load(sys.stdin)
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"behavior": behavior, "action": action, "request": req}) + "\n")


def reply(obj, code=0):
    sys.stdout.write(json.dumps(obj))
    sys.exit(code)


def echo():
    reply({"protocol": 1, "ok": True, "result": {"answers": req.get("answers"), "prior": sorted(req["prior"]), "by": req["connector"]}})


if action == "questions":
    if behavior == "broken-json":
        sys.stdout.write("{not json")
        sys.exit(0)
    if behavior == "bad-protocol":
        reply({"protocol": 2, "questions": []})
    if behavior == "questions-exit":
        print("questions exploded", file=sys.stderr)
        sys.exit(4)
    if behavior == "slow-questions":
        time.sleep(10)
    if behavior == "not-applies":
        reply({"protocol": 1, "applies": False, "questions": []})
    if behavior == "types":
        reply(
            {
                "protocol": 1,
                "questions": [
                    {"id": "go", "type": "bool", "prompt": "Go?", "default": False},
                    {"id": "env", "type": "choice", "prompt": "Env", "options": ["qa", "dev"], "default": "qa"},
                    {"id": "key", "type": "text", "prompt": "Project key"},
                ],
            }
        )
    if behavior == "ok":
        reply({"protocol": 1, "questions": [{"id": "go", "type": "bool", "prompt": "Go?", "default": True}]})
    reply({"protocol": 1, "questions": [], "timeout_seconds": 1 if behavior == "slow-run" else 60})

if behavior == "fail":
    print("build FAILURE: tests failed", file=sys.stderr)
    reply({"protocol": 1, "ok": False, "error": "FAILURE", "result": {"status": "FAILURE"}})
if behavior == "missing-fields":
    reply({"protocol": 1, "ok": True})
if behavior == "exit-nonzero":
    print("kaput", file=sys.stderr)
    sys.exit(3)
if behavior == "interrupt":
    if req.get("resume_job_id"):
        reply({"protocol": 1, "ok": True, "result": {"polled": req["resume_job_id"], "status": "SUCCESS"}})
    reply({"protocol": 1, "ok": False, "status": "interrupted", "job_id": "job-1", "error": "poll timeout", "result": {}}, code=130)
if behavior == "slow-run":
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        reply({"protocol": 1, "ok": False, "status": "interrupted", "job_id": "job-slow", "result": {}}, code=130)
echo()
