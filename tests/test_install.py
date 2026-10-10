from rmq import install


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
