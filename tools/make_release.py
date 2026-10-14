"""Builds a release of the manager: dist/rmq-<version>.zip and dist/SHA256SUMS.txt.

Only the owner of the GitHub repository publishes releases (README, «Обновления»):
    python tools/make_release.py
    gh release create v<version> dist/rmq-<version>.zip dist/SHA256SUMS.txt --title "RMQ <version>" --notes "..."
Installations find the newest release by themselves, check it against SHA256SUMS.txt and install it.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# what an installation needs; data/, .venv/, tests/, snapshots/ and dist/ never go in
FILES = ["rmq_cli.py", "requirements.txt", "install.cmd", "setup.cmd", "start.cmd", "start_hidden.cmd", "stop.cmd",
         "README.md", "AGENTS.md", "agent_rules.md", "THIRD_PARTY.md", "LICENSE", "NOTICE"]
DIRS = ["rmq", "bin", "tools"]
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
DEV_ONLY = {"tools/make_release.py", "tools/e2e_home.py"}  # the author's tools, not for an installation


def package_value(name: str) -> str:
    text = (ROOT / "rmq" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit(f"в rmq/__init__.py нет {name}")
    return m.group(1)


def check_tools() -> None:
    """The sensor binaries are not in git: make sure the ones packed are exactly the published ones."""
    for line in (ROOT / "tools" / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        want, name = line.split(maxsplit=1)
        path = ROOT / "tools" / name.strip()
        if not path.is_file():
            raise SystemExit(f"нет tools/{name.strip()}: положите файл из релиза автора (см. THIRD_PARTY.md)")
        if hashlib.sha256(path.read_bytes()).hexdigest() != want.lower():
            raise SystemExit(f"tools/{name.strip()}: контрольная сумма не совпадает с tools/SHA256SUMS.txt")


def git_dirty() -> str:
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except OSError:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def members():
    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            raise SystemExit(f"нет файла {name}")
        yield path
    for folder in DIRS:
        for path in sorted((ROOT / folder).rglob("*")):
            rel = path.relative_to(ROOT)
            if (path.is_file() and not SKIP_DIRS.intersection(rel.parts) and path.suffix not in SKIP_SUFFIXES
                    and rel.as_posix() not in DEV_ONLY):
                yield path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Собрать архив релиза в dist/")
    ap.add_argument("--allow-dirty", action="store_true", help="собрать, даже если есть незакоммиченные изменения")
    args = ap.parse_args()
    version, repo = package_value("__version__"), package_value("__repo__")
    if repo.startswith("OWNER/"):
        raise SystemExit("впишите свой репозиторий в __repo__ в rmq/__init__.py: из него установки берут обновления")
    dirty = git_dirty()
    if dirty and not args.allow_dirty:
        raise SystemExit("есть незакоммиченные изменения: закоммитьте их, чтобы релиз совпадал с репозиторием "
                         "(или --allow-dirty)\n" + dirty)
    check_tools()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"rmq-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in members():
            z.write(path, f"rmq-{version}/{path.relative_to(ROOT).as_posix()}")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {out.name}\n", encoding="utf-8", newline="\n")
    print(f"{out} ({out.stat().st_size / 2**20:.1f} МБ)\nsha256 {digest}\n\nОпубликовать:\n"
          f'gh release create v{version} dist/{out.name} dist/SHA256SUMS.txt --title "RMQ {version}" '
          f'--notes "что нового"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
