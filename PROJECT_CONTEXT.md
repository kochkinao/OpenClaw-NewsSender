# PROJECT CONTEXT

## Название
Telegram Content Pipeline

## Цель
Автоматически собирать новости из Telegram-каналов, превращать их в полезные финансовые посты, подбирать изображения и публиковать в Telegram-канал и stories.

## Основные компоненты

### 1. Data Collection
- Telethon
- user account
- выгрузка сообщений из исходных каналов

### 2. AI Layer
- OpenRouter
- модель `google/gemini-2.5-flash`
- генерация постов
- генерация image query

### 3. Image Layer
- Pixabay API
- поиск изображений
- локальное сохранение картинок
- метаданные в `*.meta.json`

### 4. Publish Layer
- Bot API для постов
- Telethon user account для stories

### 5. State / Reliability
- `state.json`
- retry
- alert-уведомления
- lock-файлы
- cleanup

## Бизнес-логика

- Тематика: финансовый канал
- Аудитория: трейдеры, брокеры, инвесторы
- Тон: экспертный, понятный, полезный
- Посты должны быть прикладными и интересными
- Первый пост дня может триггерить story
- На каждом посте может быть CTA-кнопка «Записаться»

## Текущая архитектура

1. `get_posts.py`
2. `send_posts.py`
3. `send_story.py`
4. `cleanup.py`
5. `send_test_alert.py`

## Конфиг

Используется единый `config.json`:
- `env`
- `telegram`
- `bots`
- `openrouter`
- `image_search`
- `image_storage`
- `content`
- `environments`
- `alerts`
- `paths`
- `runtime`
- `validation`
- `retention`
- `locks`

## Environments

Есть два окружения:
- `test`
- `prod`

Активное окружение задаётся в:

```json
"env": "test"
```

CLI может переопределить его через `--env`.

## Важно помнить

- истории публикуются через user account
- посты отправляются через bot
- картинки ищутся через Pixabay
- алерты шлются отдельным bot token
- cleanup чистит только старые архивы и логи
- generated-папки — это текущая рабочая очередь

## Как использовать этот файл

В новом чате просто вставь этот файл и напиши:

Работаем с этим проектом

После этого можно продолжать разработку без повторного объяснения архитектуры.
