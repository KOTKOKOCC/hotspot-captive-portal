# Hotspot Captive Portal

Backend и админ-панель для гостевого Wi-Fi:
- авторизация по номеру телефона
- подтверждение через звонок
- учёт сессий и RADIUS accounting
- аудит
- поиск
- карточка клиента
- выгрузки XLSX/ZIP
- синхронизация имён устройств из MikroTik DHCP leases

## Требования

- Ubuntu 22.04+ или другая Linux/macOS система
- Python 3.10+
- доступ к MikroTik API, если нужен sync device name

## Быстрый старт

```bash
git clone <repo_url>
cd hotspot-captive-portal
bash setup.sh

## Интеграции

Для работы нужно подключить:
- MikroTik Hotspot / RADIUS
- АТС для подтверждения звонка

Подробнее:
- `docs/mikrotik.md`
- `docs/pbx.md`

## Вход в админку

Открыть:
- `/admin/login`
