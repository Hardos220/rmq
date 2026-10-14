"""Runs one task inside its job object: starts the command, writes its output to the
log (rotating at a size limit) and records the exit code for the manager.

Executed as a plain script with `python -I`, so it uses the standard library only.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _hold_job(name):
    """Keep a handle to our own job so its name survives a manager restart."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenJobObjectW.restype = ctypes.c_void_p
    k32.OpenJobObjectW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    return k32.OpenJobObjectW(0x00100000 | 0x0004, False, name)  # SYNCHRONIZE | JOB_OBJECT_QUERY


def _prepare_venv(v, note, out):
    """A task from another PC brings its requirements: one environment per set of them, shared and kept."""
    d = v["dir"]
    ready = os.path.join(d, ".ready")
    if os.path.exists(ready):
        return True
    lock = d + ".lock"
    while True:  # another task may be installing the very same environment right now
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if os.path.exists(ready):
                return True
            try:
                if time.time() - os.path.getmtime(lock) > 3600:
                    os.remove(lock)
            except OSError:
                pass
            time.sleep(2)
    try:
        if os.path.exists(ready):
            return True
        note("preparing the Python environment for this task (once per set of requirements)")
        out.flush()
        python = v.get("python") or sys.executable
        steps = [[python, "-m", "venv", d],
                 [os.path.join(d, "Scripts", "python.exe"), "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "-r", v["requirements"]]]
        for cmd in steps:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
            if r.returncode != 0:
                note(f"could not prepare the environment: {' '.join(cmd[:4])} exited with {r.returncode}")
                return False
        with open(ready, "w", encoding="utf-8") as f:
            f.write(v["requirements"])
        return True
    finally:
        os.close(fd)
        try:
            os.remove(lock)
        except OSError:
            pass


def _resolve(argv):
    """Find argv[0] on the task's PATH (Windows would otherwise look next to this python.exe first). A path with
    folders counts from the task's folder: Windows does not even take ".venv/Scripts/python.exe" for a path."""
    exe = argv[0]
    if os.path.dirname(exe):
        return [os.path.abspath(exe)] + argv[1:]
    found = shutil.which(exe, path=os.environ.get("PATH", ""))
    return [found] + argv[1:] if found else argv


def main():
    task_dir = sys.argv[1]
    with open(os.path.join(task_dir, "spec.json"), encoding="utf-8") as f:
        spec = json.load(f)
    job = _hold_job(spec["job_name"]) if spec.get("job_name") else None  # noqa: F841 (kept open)
    log_path = os.path.join(task_dir, "output.log")
    max_bytes = int(spec.get("log_max_bytes", 256 << 20))
    out = open(log_path, "ab")

    def note(text):
        out.write(f"[rmq {time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n".encode("utf-8"))
        out.flush()

    note(f"task #{spec['id']} started, pid {os.getpid()}")
    if spec.get("venv"):
        if not _prepare_venv(spec["venv"], note, out):
            out.close()
            _write_json(os.path.join(task_dir, "exit.json"),
                        {"code": 1, "error": "не удалось установить зависимости задачи (подробности в логе)",
                         "ts": time.time()})
            return 1
        scripts = os.path.join(spec["venv"]["dir"], "Scripts")
        os.environ["PATH"] = scripts + os.pathsep + os.environ.get("PATH", "")
        os.environ["VIRTUAL_ENV"] = spec["venv"]["dir"]
    try:
        if spec.get("command"):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            proc = subprocess.Popen(f'"{comspec}" /d /s /c "{spec["command"]}"', stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            proc = subprocess.Popen(_resolve(spec["argv"]), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        note(f"failed to start: {e}")
        out.close()
        _write_json(os.path.join(task_dir, "exit.json"), {"code": 127, "error": str(e), "ts": time.time()})
        return 127

    written = os.path.getsize(log_path)
    while True:
        chunk = proc.stdout.read1(65536)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        written += len(chunk)
        if written > max_bytes:  # keep the current and the previous part only
            out.close()
            try:
                os.replace(log_path, os.path.join(task_dir, "output.1.log"))
                written = 0
            except OSError:  # someone is reading the log right now; try again a bit later
                written = max_bytes // 2
            out = open(log_path, "ab")
    code = proc.wait()
    note(f"exit code {code}")
    out.close()
    _write_json(os.path.join(task_dir, "exit.json"), {"code": code, "ts": time.time()})
    return code


if __name__ == "__main__":
    sys.exit(main())
