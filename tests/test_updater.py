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
