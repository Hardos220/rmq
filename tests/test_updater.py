from rmq import update_apply
from rmq.updater import is_newer, parse_version


def test_versions_compare_as_numbers():
    assert parse_version("v1.10.0") == (1, 10, 0) and parse_version("1.2") == (1, 2, 0)
    assert is_newer("v1.10.0", "1.9.3") and is_newer("1.1.0", "1.0.0")
    assert not is_newer("1.0.0", "1.0.0") and not is_newer("0.9", "1.0.0")


def test_an_update_replaces_the_program_but_never_the_data(tmp_path):
    install, stage = tmp_path / "install", tmp_path / "stage"
    for rel, text in {"rmq/__init__.py": "old", "rmq/gone.py": "old module", "data/rmq.db": "my data",
                      ".venv/x.txt": "env", "README.md": "old readme"}.items():
        (install / rel).parent.mkdir(parents=True, exist_ok=True)
        (install / rel).write_text(text, encoding="utf-8")
    for rel, text in {"rmq/__init__.py": "new", "rmq/net.py": "new module", "README.md": "new readme",
                      "data/rmq.db": "must not arrive"}.items():
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).write_text(text, encoding="utf-8")
    update_apply.copy_tree(str(stage), str(install))
    update_apply.drop_stale(str(stage), str(install))
    read = lambda rel: (install / rel).read_text(encoding="utf-8")  # noqa: E731
    assert read("rmq/__init__.py") == "new" and read("rmq/net.py") == "new module" and read("README.md") == "new readme"
    assert read("data/rmq.db") == "my data" and read(".venv/x.txt") == "env"
    assert not (install / "rmq" / "gone.py").exists()


class FakeDB:
    def kv_get(self, key):
        return None

    def event(self, kind, text):
        pass


class FakeManager:
    def __init__(self, home):
        self.home, self.db, self.open_dashboard_once = home, FakeDB(), False


def release(version):
    return {"version": version, "zip_url": "z", "sums_url": "s", "notes": "", "url": "u"}


def test_the_button_on_a_developer_copy_only_learns_the_latest_version(tmp_path, monkeypatch):
    from rmq import updater
    monkeypatch.setattr(updater, "is_dev_checkout", lambda: True)
    monkeypatch.setattr(updater, "latest_release", lambda: release("9.9.9"))
    monkeypatch.setattr(updater.Updater, "install", lambda self, rel: (_ for _ in ()).throw(AssertionError("installed")))
    up = updater.Updater(FakeManager(tmp_path))
    st = up.check_and_install()
    assert st["state"] == "dev" and st["latest"] == "9.9.9" and st["checked_at"]


def test_the_button_on_an_installation_installs_a_newer_release(tmp_path, monkeypatch):
    from rmq import install, updater
    monkeypatch.setattr(updater, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(install, "ensure_shortcut", lambda home: None)  # never the real desktop in tests
    monkeypatch.setattr(updater, "latest_release", lambda: release("9.9.9"))
    got = []
    monkeypatch.setattr(updater.Updater, "install", lambda self, rel: got.append(rel["version"]))
    up = updater.Updater(FakeManager(tmp_path))
    up.check_and_install()
    assert got == ["9.9.9"]
    monkeypatch.setattr(updater, "latest_release", lambda: release(updater.__version__))
    got.clear()
    assert up.check_and_install()["state"] == "idle" and not got
