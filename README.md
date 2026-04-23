# 📊 Telegram Content Pipeline

Автоматизированная система для:

- сбора постов из Telegram-каналов  
- генерации аналитических постов через AI  
- подбора релевантных изображений  
- публикации в Telegram-канал  
- публикации stories  
- мониторинга, логирования и очистки данных  

---

## 🚀 Возможности

- 🤖 Генерация постов через OpenRouter (Gemini Flash)
- 📰 Анализ нескольких Telegram-каналов
- 🖼 Автоматический подбор изображений для коротких постов (Pixabay)
- 🖼 Один короткий photo-пост в день по умолчанию — третий
- 📤 Публикация постов с кнопкой CTA
- 📲 Публикация stories сразу после первого поста с кликабельной карточкой опубликованного поста
- 🔁 Retry при ошибках
- 🚨 Telegram alerts при сбоях
- 🗂 Архивация и очистка данных
- 🔒 Защита от двойных запусков (lock-файлы)
- 🧪 test / prod окружения

---

## 🧠 Архитектура

Telegram → get_posts.py → AI → Pixabay для коротких постов → .md посты
                                      ↓  
                               send_posts.py  
                                      ↓  
                              Telegram канал + story через message_id поста
                                      ↓  
                              Telegram Stories  

---

## 📁 Структура проекта

exports/                 # сырые JSON выгрузки  
archive_exports/         # архив выгрузок  
generated_posts/         # сгенерированные посты (.md)  
generated_media/         # изображения  

sent_posts/              # отправленные посты  
sent_media/  

failed_posts/            # ошибки  
failed_media/  

raw_ai_responses/     # сырые ответы AI  

logs/  
locks/  
state.json  

---

## ⚙️ Основные скрипты

### get_posts.py
- сбор сообщений  
- генерация постов  
- подбор изображений только для коротких постов

### send_posts.py
- отправка постов  
- CTA-кнопка  
- сохранение Telegram message_id опубликованного поста
- обработка ошибок  
- триггер story  

### send_story.py
- вспомогательный ручной запуск story
- основной поток публикует story напрямую из `send_posts.py`

### cleanup.py
- очистка старых данных  

### send_test_alert.py
- тест уведомлений  

### healthcheck.py
- проверка системы  

---

## 🧩 Конфигурация

config.json

"env": "test"

environments:
- test
- prod

---

## 🤖 AI

OpenRouter → google/gemini-2.5-flash  

---

## 🖼 Изображения

Pixabay API  

---

## 📤 Публикация

- Bot API → посты  
- Telethon → stories с media area на опубликованный пост

---

## 🔘 CTA

Настраивается в config  

---

## 🚨 Alerts

Отправка ошибок в Telegram  

---

## 🔁 Retry

Автоматические повторы  

---

## 🔒 Lock

Защита от двойного запуска  

---

## 🧹 Cleanup

Очистка архивов  

---

## 🛠 Установка

python3 -m venv venv  
source venv/bin/activate  
pip install -r requirements.txt  
cp config.example.json config.json  
chmod +x *.sh  
python healthcheck.py  

---

## ▶️ Запуск

python get_posts.py  
python send_posts.py  
python cleanup.py  

---

## ⏱ Cron

30 8 * * * run_generation.sh  
0 9,12,15,18,21 * * * run_sending.sh  
15 3 * * 0 run_cleanup.sh  

---

## ⚠️ Важно

- stories только через user account  
- bot не умеет stories  
- для story нужен `message_id` опубликованного поста; фон можно задать через `content.stories.background_image_path` / `background_image_url`, иначе будет создан простой фон автоматически
- для видимой story-карточки нужен `pillow`; без него story останется технически кликабельной, но фон будет простым
- длинные посты публикуются текстом без изображения, чтобы не дробить текст из-за caption
- третий пост дня переписывается через AI в короткий формат до `1000` символов и получает картинку, если Pixabay нашел релевантное изображение
- тестируй в env=test  

---

## 📌 Итог

Полностью автоматизированная система генерации и публикации контента.
