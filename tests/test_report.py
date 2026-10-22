import json

from rmq.report import Scrubber, changed_settings
from rmq.settings import DEFAULTS


def test_scrubber_hides_what_is_private():
    s = Scrubber(home=r"C:\Users\Ivan", user="Ivan", pc="DESKTOP-ABC12", names={"Пароварка": "ПК-1", "my-net": "<сеть>"},
                 words=["Тучка"])
    text = (r"started C:\Users\Ivan\proj\run.py and /c/Users/Ivan/proj, C:/Users/Ivan/x; user Ivan on DESKTOP-ABC12; "
            "no link to Пароварка in my-net (100.97.11.75, fd7a:115c:a1e0::1); dashboard http://127.0.0.1:8777/#token="
            "abcDEF123456 key 0123456789abcdef0123456789abcdef01234567; mail me@example.com; агент сказал тучка")
    out = s(text)
    for gone in ("Ivan", "DESKTOP-ABC12", "Пароварка", "my-net", "100.97", "fd7a", "abcDEF123456", "0123456789abcdef",
                 "me@example.com", "ТУЧКА".lower(), "Тучка"):
        assert gone.lower() not in out.lower(), gone
    assert r"%USERPROFILE%\proj\run.py" in out and "%USERPROFILE%/proj" in out
    assert "ПК-1" in out and "<сеть>" in out and "127.0.0.1:8777" in out and "<кодовое слово>" in out
    assert s.tree({"Ivan": ["DESKTOP-ABC12", 5]}) == {"<user>": ["<pc>", 5]}


def test_scrubber_keeps_ordinary_words_that_contain_a_name():
    s = Scrubber(user="Ann")
    assert s("Annotation for Ann") == "Annotation for <user>"


def test_report_settings_show_only_changes_and_never_the_code_word():
    cur = json.loads(json.dumps(DEFAULTS))
    cur["thermal"]["cpu_target_c"] = 70.0
    cur["agents"]["priority_code"] = "секрет"
    out = changed_settings(cur, DEFAULTS)
    assert out == {"thermal": {"cpu_target_c": 70.0}, "agents": {"priority_code": "<задано>"}}
    assert changed_settings(json.loads(json.dumps(DEFAULTS)), DEFAULTS) == {}
