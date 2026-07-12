# Инструкция по деплою на Timeweb VPS

**IP сервера:** `<ВАШ_IP>`  
**Путь проекта:** `/opt/projects/musbooking`

## Обновление бота (проект уже развернут)

### 1. Подключитесь к VPS
```bash
ssh root@<ВАШ_IP>
```

### 2. Обновите файл main.py
**С локальной машины:**
```bash
scp main.py root@<ВАШ_IP>:/opt/projects/musbooking/
```

### 3. Перезапустите бота
**На VPS:**
```bash
systemctl restart musbooking
```

### 4. Проверьте логи
```bash
journalctl -u musbooking -f
```

---

## Первичная установка (если нужно с нуля)

## 1. Подключитесь к VPS
```bash
ssh root@<ВАШ_IP>
```

## 2. Создайте директорию проекта
```bash
mkdir -p /opt/projects/musbooking
cd /opt/projects/musbooking
```

## 3. Загрузите файлы на VPS

**С локальной машины выполните:**
```bash
scp main.py root@ваш_ip:/opt/projects/musbooking/
scp requirements.txt root@ваш_ip:/opt/projects/musbooking/
scp .env root@ваш_ip:/opt/projects/musbooking/
```

## 4. Установите Python и зависимости (на VPS)
```bash
cd /opt/projects/musbooking

# Установка Python 3.12 (если нет)
apt update
apt install -y python3.12 python3.12-venv python3-pip

# Создание виртуального окружения
python3.12 -m venv .venv

# Активация виртуального окружения
source .venv/bin/activate

# Установка зависимостей
pip install -r requirements.txt
```

## 5. Настройте systemd service
```bash
cat > /etc/systemd/system/musbooking.service << 'EOF'
[Unit]
Description=MusBooking Telegram Bot
After=network.target

[Service]
User=root
WorkingDirectory=/opt/projects/musbooking
EnvironmentFile=/opt/projects/musbooking/.env
ExecStart=/opt/projects/musbooking/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

## 6. Запустите бота
```bash
systemctl daemon-reload
systemctl enable musbooking
systemctl start musbooking
```

## 7. Проверьте статус и логи
```bash
# Статус бота
systemctl status musbooking

# Просмотр логов в реальном времени
journalctl -u musbooking -f

# Последние 100 строк логов
journalctl -u musbooking -n 100
```

## Управление ботом

**Перезапуск:**
```bash
systemctl restart musbooking
```

**Остановка:**
```bash
systemctl stop musbooking
```

**Обновление кода:**
```bash
# С локальной машины
scp main.py root@<ВАШ_IP>:/opt/projects/musbooking/

# На сервере
systemctl restart musbooking
journalctl -u musbooking -f
```

## Полезные команды

**Статус бота:**
```bash
systemctl status musbooking
```

**Просмотр логов в реальном времени:**
```bash
journalctl -u musbooking -f
```

**Последние 50 строк логов:**
```bash
journalctl -u musbooking -n 50
```

**Очистить историю виденных слотов (все слоты станут "новыми"):**
```bash
ssh root@<ВАШ_IP> "rm /opt/projects/musbooking/seen_slots.json && systemctl restart musbooking"
```

**Просмотреть файлы проекта:**
```bash
ssh root@<ВАШ_IP> "ls -la /opt/projects/musbooking/"
```

**Проверить .env файл:**
```bash
ssh root@<ВАШ_IP> "cat /opt/projects/musbooking/.env"
```

**Проверить seen_slots.json:**
```bash
ssh root@<ВАШ_IP> "cat /opt/projects/musbooking/seen_slots.json"
```

**Проверить assignments.json (расписание):**
```bash
ssh root@<ВАШ_IP> "cat /opt/projects/musbooking/assignments.json"
```
