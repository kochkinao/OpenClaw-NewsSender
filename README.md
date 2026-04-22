# 📊 TG Post Automation

Скрипт для:
- выгрузки постов из Telegram-каналов  
- генерации аналитических постов через AI  
- автоматической публикации в канал  
- уведомлений об ошибках  

---

## ⚙️ Установка

```bash
git clone <repo>
cd tg_post

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
cp config.example.json config.json
```

---

## 🧩 Настройка `config.json`

Обязательно заполнить:

```json
telegram.api_id
telegram.api_hash
channels
openrouter.api_key
sender.bot_token
sender.chat_id
```

---

## 🚀 Генерация постов

```bash
python get_posts.py
```

### Режимы

```bash
# вчера
python get_posts.py --mode yesterday

# конкретная дата
python get_posts.py --mode date --date 2026-04-17

# диапазон
python get_posts.py --mode range --start-date 2026-04-10 --end-date 2026-04-17

# последние N дней
python get_posts.py --mode days --days 7
```

---

## 📤 Отправка постов

```bash
python send_posts.py
```

- отправляет **1 пост за запуск**
- используется в cron

---

## ⏱ Cron

```bash
crontab -e
```

```cron
30 8 * * * /root/tg_post/run_generation.sh
0 9,12,15,18,21 * * * /root/tg_post/run_sending.sh
```

---

## 📁 Структура

```
exports/              
archive_exports/      
generated_posts/      
sent_posts/           
failed_posts/         
logs/                 
locks/                
```

---

## 🔘 Кнопка CTA

```json
"sender_cta": {
  "enabled": true,
  "text": "Записаться",
  "url": "https://t.me/username"
}
```

---

## 🚨 Уведомления

```json
"alerts": {
  "enabled": true,
  "bot_token": "...",
  "chat_ids": ["@user1", "@user2"]
}
```

---

## 📊 Логи

```bash
tail -n 100 logs/export.log
tail -n 100 logs/sender.log
```
