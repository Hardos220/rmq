import subprocess

from rmq import install
from rmq.server import content_type


def test_agent_rules_block_is_added_once_and_removed_cleanly(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    own = "# Мои правила\n\nВсегда отвечай кратко.\n"
    (claude / "CLAUDE.md").write_text(own, encoding="utf-8")
    monkeypatch.setattr(install, "AGENT_FILES", [("Claude Code", claude, "CLAUDE.md"),
                                                 ("Codex CLI", tmp_path / ".codex", "AGENTS.md")])

    install.install_agent_rules()
    install.install_agent_rules()  # re-install replaces the block instead of adding a second one
    text = (claude / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith(own.strip())
    assert text.count("<!-- rmq:begin") == 1 and text.count(install.END) == 1
    assert "rmq guide" in text and "{RMQ_SH}" not in text
    assert not (tmp_path / ".codex").exists()  # tools that are not installed are left alone

    install.install_agent_rules(remove=True)
    assert (claude / "CLAUDE.md").read_text(encoding="utf-8").strip() == own.strip()


def test_rules_only_file_is_deleted_on_uninstall(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr(install, "AGENT_FILES", [("Claude Code", claude, "CLAUDE.md")])
    install.install_agent_rules()
    assert (claude / "CLAUDE.md").exists()
    install.install_agent_rules(remove=True)
    assert not (claude / "CLAUDE.md").exists()


def test_guide_has_real_paths():
    guide = install.render_guide()
    assert "{RMQ_SH}" not in guide and "{URL}" not in guide
    assert "/bin/rmq" in guide and "rmq.cmd" in guide


def test_dashboard_files_have_their_own_types_whatever_the_registry_says():
    # on some PCs the registry maps .js to text/plain, and the browser then refuses to run the dashboard
    assert content_type("app.js") == "text/javascript; charset=utf-8"
    assert content_type("style.css").startswith("text/css") and content_type("index.html").startswith("text/html")
    assert content_type("rmq.ico") == "image/x-icon" and content_type("x.bin") == "application/octet-stream"


def fake_install(tmp_path, monkeypatch):
    root, desk = tmp_path / "rmq", tmp_path / "desk"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
    desk.mkdir()
    monkeypatch.setattr(install, "ROOT", root)
    monkeypatch.setattr(install, "desktop_folder", lambda: desk)
    return root, desk


def test_a_shortcut_that_powershell_cannot_make_becomes_a_launcher_file(tmp_path, monkeypatch):
    root, desk = fake_install(tmp_path, monkeypatch)
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "blocked\n"))
    msg = install.install_shortcut(tmp_path / "data")
    launcher = desk / install.LAUNCHER
    assert "blocked" in msg and launcher.exists() and not (desk / install.SHORTCUT).exists()
    text = launcher.read_bytes().decode("utf-8")
    assert text.startswith("@echo off\r\nchcp 65001") and "rmq_cli.py" in text and "launch" in text
    assert (tmp_path / "data" / "shortcut.txt").exists()

    def made(*a, **k):  # PowerShell works now: the real shortcut replaces the launcher
        (desk / install.SHORTCUT).write_bytes(b"lnk")
        return subprocess.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(install.subprocess, "run", made)
    assert install.install_shortcut(tmp_path / "data").startswith("ярлык создан")
    assert not launcher.exists()


def test_the_shortcut_is_made_once_and_a_deleted_one_is_not_brought_back(tmp_path, monkeypatch):
    root, desk = fake_install(tmp_path, monkeypatch)
    calls = []

    def made(*a, **k):
        calls.append(1)
        (desk / install.SHORTCUT).write_bytes(b"lnk")
        return subprocess.CompletedProcess(a, 0, "", "")
    monkeypatch.setattr(install.subprocess, "run", made)
    home = tmp_path / "data"
    assert install.ensure_shortcut(home).startswith("ярлык создан") and calls == [1]
    (desk / install.SHORTCUT).unlink()                    # the owner deleted it
    assert install.ensure_shortcut(home) is None and calls == [1]
    other = tmp_path / "data2"                            # an installation that already has a shortcut
    (desk / install.SHORTCUT).write_bytes(b"lnk")
    assert install.ensure_shortcut(other) is None and calls == [1] and (other / "shortcut.txt").exists()
