"""Human-readable (Russian) explanations of task states and wait reasons."""
from __future__ import annotations

STATE_NAMES = {
    "remote": "в сети",
    "queued": "в очереди",
    "running": "выполняется",
    "suspended": "на паузе",
    "done": "готово",
    "failed": "ошибка",
    "cancelled": "отменена",
    "timeout": "лимит времени",
    "lost": "потеряна",
}

_REASONS = {
    "paused": "Пауза всех задач (режим «Пауза»)",
    "heat": "Перегрев: задачи агентов заморожены, пока ПК не остынет",
    "user": "Приостановлено вручную",
    "resume": "Ждёт возобновления",
    "game": "Игровой режим: задачи на GPU ждут",
    "gpu_user": "Видеокарта занята вами",
    "ram": "Оперативная память отдана вашим приложениям",
    "vram": "Видеопамять отдана вашим приложениям",
    "commit": "Почти закончилась виртуальная память (файл подкачки)",
    "time_limit": "Превышен жёсткий лимит времени",
    "agent_disabled": "Агент отключён",
    "cancelled": "Отменена",
    "lost": "процесс задачи пропал, пока менеджер был остановлен",
    "shutdown": "Менеджер был выключен — задача продолжит после его запуска",
    "disk": "Мало места на диске: задача ждёт, пока его освободят",
    "hung": "Зависла: долго не было признаков работы — заморожена, пока её не продолжат (rmq resume) или не отменят",
    "net_closed": "Владелец закрыл свой ПК для сети: задача продолжит, когда он снова откроет его",
}

_LACK = {"cpu": "CPU", "ram": "ОЗУ", "vram": "VRAM"}


def fmt_minutes(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{max(1, m)} мин"


def _num(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") if abs(x) < 100 else f"{x:.0f}"


def describe(code: str | None, detail: dict | None = None) -> str:
    detail = detail or {}
    if not code:
        return ""
    if code.startswith("fair:"):
        return f"Уступила ресурсы задаче #{code[5:]} (честная очередь между агентами)"
    if code.startswith("requeued:"):
        return "Перезапущена с начала: " + describe(code[9:]).lower()
    if code.startswith("stopping:"):
        return "Сохраняет чекпоинт и останавливается (её попросили через RMQ_STOP_FILE): " + describe(code[9:]).lower()
    if code == "stopping":
        ids = detail.get("for", [])
        names = ", ".join(f"#{i}" for i in ids)
        if len(ids) > 1:
            return f"Ждёт, пока задачи {names} сохранят чекпоинт и освободят ресурсы"
        return f"Ждёт, пока задача {names} сохранит чекпоинт и освободит ресурсы"
    if code == "no_fit":
        need, free = detail.get("need", {}), detail.get("free", {})
        parts = []
        for r in detail.get("lacking", []):
            if r == "commit":
                parts.append("мало места в файле подкачки")
            else:
                unit = " яд." if r == "cpu" else " ГБ"
                parts.append(f"{_LACK[r]}: нужно {_num(need.get(r, 0))}{unit}, свободно "
                             f"{_num(max(0.0, free.get(r, 0)))}{unit}")
        return "Не хватает ресурсов — " + "; ".join(parts) if parts else "Не хватает ресурсов"
    if code == "agent_limit":
        return f"У агента уже {detail.get('max_running', '?')} активных задач (его лимит)"
    if code == "fair_share":
        return (f"Агент уже занимает {detail.get('share', 0):.0%} пула при честной доле "
                f"{detail.get('fair', 0):.0%}; сначала задача #{detail.get('for', '?')}")
    if code == "reserved":
        return f"Ресурсы копятся для давно ждущей задачи #{detail.get('for', '?')}"
    return _REASONS.get(code, code)
