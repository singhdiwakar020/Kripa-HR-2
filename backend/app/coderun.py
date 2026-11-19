"""Runs a candidate's Python code safely, inside a throwaway Docker container.

Why Docker: the code comes from strangers, so it must never run directly on this computer. Each run gets a fresh
container with no network, a small memory and CPU share, a process limit, a read-only file system, no extra privileges
and a hard time limit, and the container is deleted afterwards.

Needs Docker Desktop running and the image pulled once:  docker pull python:3.12-slim
When Docker is not available, available() is False and coding answers are left for HR to mark by hand.
Settings (backend/.env, all optional): EXAM_PYTHON_IMAGE, EXAM_RUN_TIMEOUT (seconds, default 5), EXAM_RUN_PARALLEL (default 3).
"""
import os
import subprocess
import threading
import time
import uuid

IMAGE = lambda: (os.getenv("EXAM_PYTHON_IMAGE") or "python:3.12-slim").strip()  # noqa: E731
_gate = None
_ok = {"at": 0.0, "value": False}
MAX_OUT = 20_000

# Runs inside the container. Reads {"code", "input"} from stdin, then runs the code with the given input as its stdin.
WRAPPER = (
    "import sys, json, io\n"
    "d = json.loads(sys.stdin.read())\n"
    "sys.stdin = io.StringIO(d['input'])\n"
    "exec(compile(d['code'], 'main.py', 'exec'), {'__name__': '__main__'})\n"
)


def _timeout() -> float:
    try:
        return float(os.getenv("EXAM_RUN_TIMEOUT", "5"))
    except ValueError:
        return 5.0


def available() -> bool:
    """True when Docker answers and the Python image is present. Checked at most every 30 seconds."""
    if time.time() - _ok["at"] < 30:
        return _ok["value"]
    ok = False
    try:
        r = subprocess.run(["docker", "image", "inspect", IMAGE()], capture_output=True, timeout=8)
        ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _ok.update(at=time.time(), value=ok)
    return ok


def run_python(code: str, stdin: str = "") -> dict:
    """Run one program. Returns {"ok", "stdout", "stderr", "timeout"}; ok is False if it crashed, timed out or Docker failed."""
    global _gate
    if _gate is None:
        _gate = threading.Semaphore(int(os.getenv("EXAM_RUN_PARALLEL", "3") or 3))
    import json
    name = "exam-" + uuid.uuid4().hex[:12]
    cmd = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", "--memory", "128m", "--memory-swap", "128m",
           "--cpus", "0.5", "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:size=16m", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--user", "65534:65534", "-e", "PYTHONDONTWRITEBYTECODE=1",
           IMAGE(), "python", "-I", "-c", WRAPPER]
    payload = json.dumps({"code": code or "", "input": stdin or ""})
    with _gate:
        try:
            r = subprocess.run(cmd, input=payload.encode("utf-8"), capture_output=True, timeout=_timeout() + 3)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            return {"ok": False, "stdout": "", "stderr": "Time limit exceeded", "timeout": True}
        except OSError as e:
            return {"ok": False, "stdout": "", "stderr": f"Code runner unavailable: {e}", "timeout": False}
    out = r.stdout.decode("utf-8", "replace")[:MAX_OUT]
    err = r.stderr.decode("utf-8", "replace")[:MAX_OUT]
    if r.returncode in (125, 126, 127):  # docker itself failed
        return {"ok": False, "stdout": "", "stderr": "Code runner unavailable", "timeout": False}
    if r.returncode == 137:
        return {"ok": False, "stdout": out, "stderr": "Memory or time limit exceeded", "timeout": True}
    return {"ok": r.returncode == 0, "stdout": out, "stderr": err, "timeout": False}


def same_output(got: str, expected: str) -> bool:
    """Compare like most judges do: ignore trailing spaces on each line and blank lines at the end."""
    norm = lambda t: "\n".join(line.rstrip() for line in (t or "").replace("\r\n", "\n").strip().split("\n"))  # noqa: E731
    return norm(got) == norm(expected)


def run_cases(code: str, cases: list[dict]) -> list[dict]:
    """Run the code once per test case, a few at a time. Returns [{"passed", "got", "error", "timeout"}] in the same order."""
    results: list[dict | None] = [None] * len(cases)

    def one(i, case):
        r = run_python(code, str(case.get("input", "")))
        passed = r["ok"] and same_output(r["stdout"], str(case.get("expected", "")))
        results[i] = {"passed": bool(passed), "got": r["stdout"][:2000], "error": (r["stderr"] or "")[-1500:] if not r["ok"] else "",
                      "timeout": r["timeout"]}

    threads = [threading.Thread(target=one, args=(i, c)) for i, c in enumerate(cases)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r or {"passed": False, "got": "", "error": "Not run", "timeout": False} for r in results]
