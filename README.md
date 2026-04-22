# README

## 1. Виртуальное окружение

which python  
если не venv →  
source venv/bin/activate

---

## 2. Установка

python3 -m venv venv  
source venv/bin/activate  
pip install -r requirements.txt  
cp config.example.json config.json  

заполнить:
- telegram.api_id
- telegram.api_hash
- channels
- openrouter.api_key
- sender.bot_token
- sender.chat_id

---

## 3. Генерация постов

python get_posts.py

режимы:
python get_posts.py --mode yesterday  
python get_posts.py --mode date --date 2026-04-17  
python get_posts.py --mode range --start-date 2026-04-10 --end-date 2026-04-17  
python get_posts.py --mode days --days 7  

---

## 4. Отправка

python send_posts.py  
(1 пост за запуск)

---

## 5. Запуск не из директории

python /root/tg_post/get_posts.py \
  --config /root/tg_post/config.json \
  --mode yesterday

---

## 6. Cron

crontab -e

30 8 * * * /root/tg_post/run_generation.sh  
0 9,12,15,18,21 * * * /root/tg_post/run_sending.sh  

---

## 7. Структура

exports/  
archive_exports/  
generated_posts/  
sent_posts/  
failed_posts/  
logs/  
locks/  

---

## 8. Кнопка

"sender_cta": {
  "enabled": true,
  "text": "Записаться",
  "url": "https://t.me/username"
}

---

## 9. Alerts

"alerts": {
  "enabled": true,
  "bot_token": "...",
  "chat_ids": ["@user1", "@user2"]
}

---

## 10. Логи

tail -n 100 logs/export.log  
tail -n 100 logs/sender.log  
