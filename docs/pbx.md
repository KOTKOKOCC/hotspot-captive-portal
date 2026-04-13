# PBX integration

Подтверждение номера выполняется через АТС.

Backend сам не звонит в АТС.
АТС или внешняя интеграция должна отправлять запрос в:

- `POST /pbx-call`

Пример запроса:

```json
{
  "phone": "79001234567"
}
```

## Логика работы

1. Гость вводит номер на captive portal
2. Backend создаёт pending через `/radius-check`
3. Гость звонит на номер АТС
4. PBX получает CallerID
5. PBX вызывает `/pbx-call`
6. Backend переводит номер в `verified`
7. Следующий `/radius-check` пускает клиента

## Пример рабочей интеграции с FreePBX / Asterisk

Открыть файл:

```bash
nano /etc/asterisk/extensions_custom.conf
```

Добавить:

```asterisk
[from-pstn-custom-hotspot]
exten => s,1,NoOp(Hotspot verification call)
 same => n,Set(CALLNUM=${CALLERID(num)})
 same => n,NoOp(CallerID raw: ${CALLNUM})
 same => n,System(echo ${STRFTIME(${EPOCH},,%F %T)} CALLNUM=${CALLNUM} >> /tmp/hotspot_pbx.log)
 same => n,System(/usr/bin/curl -s -X POST http://IP_ADDRESS_BACKEND:8080/pbx-call -H 'Content-Type: application/json' -d '{"phone":"${CALLNUM}"}' >/dev/null 2>&1)
 same => n,Playback(vm-goodbye)
 same => n,Hangup()
```

`IP_ADDRESS_BACKEND` необходимо заменить адресом своего backend'а

Перезагрузить dialplan:

```bash
asterisk -rx "dialplan reload"
```

## Настройка в FreePBX GUI

### 1. Создать Custom Destination

Открыть:

- `Admin → Custom Destinations`

Создать destination:

**Target**
```text
from-pstn-custom-hotspot,s,1
```

**Description**
```text
Hotspot PBX Verify
```

**Notes**
Можно оставить пустым или указать:
```text
Верификация номера для Wi-Fi Hotspot
```

**Return**
```text
No
```

После этого нажать:
- `Submit`
- `Apply Config`

### 2. Подключить к входящему номеру

Открыть:

- `Connectivity → Inbound Routes`

Выбрать нужный входящий номер / DID и в поле destination указать созданный Custom Destination:

```text
Hotspot PBX Verify
```

## Что делает эта схема

Текущая рабочая логика такая:

1. номер введён на портале
2. backend создал pending
3. звонок пришёл в PBX
4. `/pbx-call` нашёл pending и перевёл номер в `verified`

## Примечания

- В примере backend доступен по адресу `http://IP_ADDRESS_BACKEND:8080` - неоходимо вставить адрес вашего backend'а
- При другой установке нужно заменить IP и порт на свои
- PBX должна передавать номер в формате, который backend сможет нормализовать до `79XXXXXXXXX`
