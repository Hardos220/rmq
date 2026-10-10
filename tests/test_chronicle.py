from rmq.chronicle import segments, totals


def ev(i, ts, kind, msg=""):
    return {"id": i, "ts": ts, "kind": kind, "message": msg, "task_id": 1}


def test_timeline_of_a_task_with_a_freeze_and_a_restart():
    task = {"submitted_at": 0, "started_at": 10, "finished_at": 400}
    events = [ev(1, 0, "submit"), ev(2, 10, "start"), ev(3, 100, "suspend", "Пауза: Игровой режим: задачи на GPU ждут"),
              ev(4, 160, "resume"), ev(5, 200, "requeue", "Перезапуск позже: Оперативная память отдана вашим приложениям"),
              ev(6, 230, "start", "Запущена (перезапуск №1)"), ev(7, 400, "done", "Завершена успешно")]
    segs, marks = segments(task, events, now=1000)
    assert [x[2] for x in segs] == ["wait", "run", "frozen", "run", "wait", "run"]
    assert segs[2][3] == "Игровой режим: задачи на GPU ждут"
    assert segs[4][3] == "Оперативная память отдана вашим приложениям"
    assert segs[-1][:2] == [230, 400]
    assert [m[1] for m in marks] == ["restart", "done"]
    assert totals(segs) == {"run": 90 + 40 + 170, "wait": 10 + 30, "frozen": 60}


def test_a_running_task_reaches_now_and_hangs_are_marked():
    events = [ev(1, 0, "submit"), ev(2, 5, "start"), ev(3, 40, "hung", "Зависла"), ev(4, 40, "suspend", "Заморожена")]
    segs, marks = segments({"submitted_at": 0, "started_at": 5, "finished_at": None}, events, now=50)
    assert segs == [[0, 5, "wait", ""], [5, 40, "run", ""], [40, 50, "frozen", "Заморожена"]]
    assert marks == [[40, "hung", "Зависла"]]


def test_a_task_with_a_pruned_log_falls_back_to_its_timestamps():
    segs, _ = segments({"submitted_at": 0, "started_at": 20, "finished_at": 90}, [], now=500)
    assert segs == [[0, 20, "wait", ""], [20, 90, "run", ""]]
    segs, _ = segments({"submitted_at": 0, "started_at": None, "finished_at": None}, [], now=30)
    assert segs == [[0, 30, "wait", ""]]
