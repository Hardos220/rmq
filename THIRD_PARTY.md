# Сторонние компоненты

В архив релиза (папка `tools/`) без изменений входят два сторонних компонента. Они нужны только для датчика
температуры процессора и ставятся отдельно, по желанию ([tools/README.md](tools/README.md)). В git-репозитории их нет:
файлы берутся из официальных релизов авторов и проверяются по `tools/SHA256SUMS.txt`.

| Компонент | Версия | Лицензия | Исходный код |
|---|---|---|---|
| LibreHardwareMonitor | 0.9.6 | [MPL-2.0](https://www.mozilla.org/MPL/2.0/) | https://github.com/LibreHardwareMonitor/LibreHardwareMonitor |
| PawnIO (драйвер и его установщик, подписаны автором) | 2.2.0 | GPL-2.0-or-later с исключением для программ, которые работают с драйвером только через IOCTL | https://github.com/namazso/PawnIO, релизы: https://github.com/namazso/PawnIO.Setup |

Менеджер общается с ними только через локальный веб-сервер LibreHardwareMonitor, а LibreHardwareMonitor с драйвером —
через его интерфейс IOCTL.

При установке из PyPI ставится пакет [psutil](https://github.com/giampaolo/psutil) (BSD-3-Clause).
