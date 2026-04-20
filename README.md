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

Backend для captive portal с интеграцией FreeRADIUS и MikroTik Hotspot.

Система состоит из двух частей:

1. backend на FastAPI
2. FreeRADIUS, который:
   - отправляет запросы авторизации в backend через `/radius-check`
   - отправляет accounting-события в backend через `/radius-accounting`

---

# Текущее состояние

Сейчас проект требует ручной настройки FreeRADIUS и systemd.  
Полная автоматическая установка в одну команду пока не завершена.

Рабочая схема:

- backend слушает `127.0.0.1:8000`
- FreeRADIUS использует `rest` для `/radius-check`
- FreeRADIUS использует `hotspot_accounting_forward` для `/radius-accounting`
- MikroTik использует этот сервер как RADIUS для Hotspot

---

# Требования

- Ubuntu 22.04
- root-доступ
- MikroTik Hotspot
- сетевой доступ между MikroTik и сервером

---

# 1. Установка системных пакетов

```bash
apt update
apt install -y git python3 python3-venv sqlite3 curl freeradius freeradius-utils freeradius-rest
```

---

# 2. Клонирование и установка репозитория

```git clone https://github.com/KOTKOKOCC/hotspot-captive-portal.git
cd hotspot-captive-portal
bash setup.sh
```

# 3. Проверка ручного запуска backend

```cd /opt/hotspot-captive-portal
source .venv/bin/activate
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

```
curl http://127.0.0.1:8000/admin/login
```

# 4. Настройка systemd для backend

Создать unit:

```
nano /etc/systemd/system/hotspot-captive-portal.service
```

Содержимое:

```
[Unit]
Description=Hotspot Auth Portal
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hotspot-captive-portal
ExecStart=/opt/hotspot-captive-portal/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```


Применить:

```
systemctl daemon-reload
systemctl enable hotspot-captive-portal
systemctl start hotspot-captive-portal
```

# 5. Настройка FreeRADIUS rest


1. Включить модуль:
```
ln -s /etc/freeradius/3.0/mods-available/rest /etc/freeradius/3.0/mods-enabled/rest
```

2. Настроить:
   Открыть:
   ```
    nano /etc/freeradius/3.0/mods-enabled/rest
   ```
   и провести к виду:
   ```
    rest {
        connect_uri = "http://127.0.0.1:8000"
    
        authorize {
            uri = "${..connect_uri}/radius-check"
            method = "post"
            body = "json"
            data = '{"username":"%{User-Name}","mac":"%{Calling-Station-Id}","ip":"%{Framed-IP-Address}","nas_id":"%{NAS-Identifier}","ssid":"%{Called-Station-Id}","hotel":"%{NAS-Identifier}"}'
        }
    }
   ```

3. Открыть:
   

```
  nano /etc/freeradius/3.0/sites-enabled/default
```

и в блоке authorize{} закоментировать `files` и довабить `rest`


т.е. примерно дожно быть так:

```suffix
    eap {
        ok = return
    }
    # files
    rest
    -sql
    -ldap
    expiration
    logintime
    pap
```

# 6. Насктройка accaunting forward

В репозитории есть модуль hotspot_accounting_forward и форвардер radius_accounting_forward.py.

Установка:

```bash deploy/freeradius/install_radius_forward.sh
```

после этого в `/etc/freeradius/3.0/sites-enabled/default` в `accounting {}` должа появится строка `hotspot_accounting_forward`

Пример рабочего фрагмента

```
accounting {
    detail
    unix
    -sql
    exec
    attr_filter.accounting_response
    hotspot_accounting_forward
}
```

# 7. Добавление Mikrotik в FreeRADIUS

Открыть:

```
nano /etc/freeradius/3.0/clients.conf
```

Добавить клиента:

```
client mikrotik-hotspot {
    ipaddr = 192.168.88.1
    secret = password
    shortname = mikrotik-hotspot
    nas_type = other
    require_message_authenticator = yes
}
```

Где `ipaddr` и `secret` - берем из mikrotik.

# 8. Запуск FreeRADIUS

Обычный режим:

```
systemctl restart freeradius
systemctl enable freeradius
systemctl status freeradius --no-pager
```

Режим отладки:

```
systemctl stop freeradius
freeradius -X
```
# Настройка Mikrotik

Базовый минимум:

/radius add service=hotspot address=[IP адрес backend] secret=[пароль] authentication-port=1812 accounting-port=1813
/ip hotspot profile set [find default=yes] use-radius=yes

Ожидаемо:

* use-radius=yes
* адрес RADIUS совпадает с сервером
* secret совпадает с clients.conf

## Интеграции

Для работы нужно подключить:
- MikroTik Hotspot / RADIUS
- АТС для подтверждения звонка

Подробнее:
- [MikroTik integration](docs/mikrotik.md)
- [PBX integration](docs/pbx.md)

## Вход в админку
http://[ip addr сервера]:8000/admin/login
