# MikroTik integration

Для работы приложения нужен MikroTik Hotspot / RADIUS.

Backend использует два endpoint:

- `POST /radius-check`
- `POST /radius-accounting`

## `/radius-check`

Используется для проверки, можно ли пустить клиента.

Пример запроса:

```json
{
  "username": "79001234567",
  "mac": "AA:BB:CC:DD:EE:01",
  "ip": "10.32.1.10",
  "nas_id": "hotspot.test",
  "hotel": "TestHotel",
  "ssid": "MIRACLEON"
}
```

## `/radius-accounting`

Используется для учёта сессий.

Поддерживаются события:
- `Start`
- `Interim-Update`
- `Stop`
