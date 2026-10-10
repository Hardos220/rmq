"""Installs a downloaded update while the manager is stopped, then starts it again.

Run by the manager as a separate process from a copy in data/update, so it never overwrites itself:
    python -I apply.py <install folder> <unpacked release> <data folder> <manager pid> <version>
Standard library only. data/ and .venv/ are never touched; files of the old version that the new one no
longer has are removed from rmq/ only.
"""
import ctypes
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time

KEEP = {"data", ".venv", ".git", "snapshots", "dist"}


def wait_for_exit(pid: int, timeout: float = 90) -> None:
    k32 = ctypes.WinDLL("kernel32")
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return
    try:
        k32.WaitForSingleObject(h, int(timeout * 1000))
    finally:
        k32.CloseHandle(h)


def copy_tree(src: str, dst: str) -> None:
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d not in KEEP]
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for f in filenames:
            s, d = os.path.join(dirpath, f), os.path.join(target, f)
            for attempt in range(10):  # a file may still be locked for a moment after the manager exits
                try:
                    shutil.copy2(s, d)
                    break
                except PermissionError:
                    time.sleep(1)
            else:
                raise RuntimeError(f"не удалось заменить {d}")


def drop_stale(src: str, dst: str) -> None:
    new_pkg, old_pkg = os.path.join(src, "rmq"), os.path.join(dst, "rmq")
    for dirpath, _dirnames, filenames in os.walk(old_pkg):
        rel = os.path.relpath(dirpath, old_pkg)
        if "__pycache__" in rel.split(os.sep):
            continue
        for f in filenames:
            if not os.path.exists(os.path.join(new_pkg, rel, f)):
                try:
                    os.remove(os.path.join(dirpath, f))
                except OSError:
                    pass


def main() -> int:
    install, stage, home, pid, version = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
    mark = os.path.join(home, "update", "last.json")
    result = {"version": version, "ts": time.time(), "ok": False, "error": ""}
    try:
        wait_for_exit(pid)
        req_old, req_new = os.path.join(install, "requirements.txt"), os.path.join(stage, "requirements.txt")
        req_changed = not (os.path.exists(req_old) and os.path.exists(req_new) and filecmp.cmp(req_old, req_new, False))
        copy_tree(stage, install)
        drop_stale(stage, install)
        venv_python = os.path.join(install, ".venv", "Scripts", "python.exe")
        if req_changed and os.path.exists(venv_python):
            subprocess.run([venv_python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                            os.path.join(install, "requirements.txt")], creationflags=0x08000000, timeout=900)
        result["ok"] = True
    except Exception as e:  # report it on the next start; the old files that were not replaced keep working
        result["error"] = str(e)
    finally:
        with open(mark, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        pyw = os.path.join(install, ".venv", "Scripts", "pythonw.exe")
        if os.path.exists(pyw):
            subprocess.Popen([pyw, os.path.join(install, "rmq_cli.py"), "serve"], cwd=install,
                             creationflags=0x00000008 | 0x00000200, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        shutil.rmtree(stage, ignore_errors=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
