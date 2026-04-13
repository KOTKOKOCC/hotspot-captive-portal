COLUMN_LABELS = {
    "id": "ID",
    "guest_id": "ID гостя",
    "phone": "Номер телефона",
    "mac": "MAC-адрес",
    "ip": "IP-адрес",
    "nas_id": "Устройство доступа",
    "hotel": "Отель",
    "ssid": "Wi-Fi сеть",
    "vlan_id": "VLAN",
    "event_type": "Событие",
    "event_time": "Время события",
    "details": "Описание",
    "created_at": "Создано",
    "updated_at": "Обновлено",
    "first_verified_at": "Первая верификация",
    "first_hotel": "Первый отель",
    "auth_method": "Метод авторизации",
    "status": "Статус",
    "started_at": "Начало сессии",
    "ended_at": "Конец сессии",
    "expires_at": "Действует до",
    "callerid_raw": "CallerID",
    "source_ip": "IP источника",
    "result": "Результат",
    "hotel_name": "Отель",
    "ssid_name": "Wi-Fi сеть",
    "subnet_cidr": "Подсеть",
    "mikrotik_interface": "Интерфейс MikroTik",
    "hotspot_server": "Hotspot server",
    "is_active": "Активна",
    "acct_session_id": "ID RADIUS-сессии",
    "last_seen_at": "Последняя активность",
    "terminate_cause": "Причина завершения",
    "acct_session_time": "Длительность, сек",
    "last_auth_at": "Последний допуск",
    "device_name": "Имя устройства",
    }


EVENT_LABELS = {
    "pending_created": "Создана заявка на подтверждение",
    "call_verified": "Номер подтверждён звонком",
    "call_no_pending": "Звонок без активной заявки",
    "call_expired_pending": "Звонок по просроченной заявке",
    "radius_accept_new_session": "Открыта новая сессия",
    "radius_accept_existing_session": "Повторный вход в активную сессию",
    "radius_reject_invalid_phone": "Отклонено: неверный формат номера",
    "radius_reject_pending_waiting_call": "Ожидание звонка",
    "radius_reject_pending_expired": "Отклонено: заявка просрочена",
    "radius_reject_device_limit": "Отклонено: превышен лимит устройств",
}


STATUS_LABELS = {
    "active": "Активна",
    "pending": "Ожидание",
    "verified": "Подтверждена",
    "expired": "Истекла",
    "closed": "Закрыта",
    "blocked": "Заблокирована",
}

RESULT_LABELS = {
    "matched_pending": "Заявка найдена",
    "no_pending": "Заявка не найдена",
    "expired_pending": "Заявка просрочена",
}


AUTH_METHOD_LABELS = {
    "call": "Звонок",
}


TERMINATE_CAUSE_LABELS = {
    "Lost-Service": "Потеря соединения",
    "Lost Service": "Потеря соединения",
    "Lost-Carrier": "Потеря линка",
    "User-Request": "Отключено пользователем",
    "Admin-Reset": "Сброс администратором",
    "Session-Timeout": "Истечение времени сессии",
    "Idle-Timeout": "Таймаут неактивности",
    "Legacy-Cleanup": "Закрыто служебной очисткой",
    "Retest-Cleanup": "Закрыто служебно",
}