# wifi-monitor

Инструмент вардрайвинга с двойной функцией:
1. **Пассивный сбор данных** о Wi-Fi сетях с GPS-привязкой (через Kismet + gpsd)
2. **Проверка работоспособности** собственных точек доступа оператора

## Платформа
- Kali Linux x86-64 (VirtualBox / VMware)
- Python 3.10+

## Оборудование
- USB Wi-Fi: Alfa AWUS036AC (RTL8812AU) — monitor mode
- USB Wi-Fi: любой managed-адаптер
- USB GPS: u-blox → `/dev/ttyACM0`

## Установка
```bash
chmod +x scripts/install.sh
sudo scripts/install.sh
pip install -r requirements.txt
```

## Режимы работы
| Режим | Описание                        |
|-------|---------------------------------|
| 1     | Только мониторинг (Kismet+GPS)  |
| 2     | Только проверка точек оператора |
| 3     | Оба режима одновременно         |

## Конфигурация
- `config/settings.yaml` — основные параметры
- `config/ap_targets.yaml` — точки оператора (**права 600**, не коммитить)

## База данных
SQLite (WAL mode), таблицы: `networks`, `observations`, `ap_health`
