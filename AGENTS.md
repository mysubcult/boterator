# Boterator — модерация заявок в Telegram-каналы

Форк `worried-networking/boterator`. Мастер-бот `@Boteratormainbot` регистрирует ботов-приёмников; каждый принимает
заявки в личке, пересылает их в чат модераторов, собирает голоса и публикует одобренное в канал.

## Где что

- Сервер: `/opt/boterator` — git-клон этого репозитория (ветка `master`), docker compose: `app` + `postgres`.
  `.env` и `docker-compose.yml` — только на сервере (в `.gitignore`), `backups/` — локальные бэкапы.
- Боты (настройки — `registered_bots.settings` JSONB, меняются через `/settings` в личке бота-приёмника):

| Бот | Канал | Язык |
|---|---|---|
| `@showerthoughts_robot` (337970525) | `@showerthoughts` | en |
| `@Poetry_submit_bot` (5758384054) | `@PoetSanctuary` | en |
| `@mildlyinteresting_robot` (422727809) | `@mildlyinteresting` | en |

  Чат модераторов у всех один. Владелец — `MASTER_OWNER_ID` из `.env`.

## ИИ

- Провайдер — DeepSeek (`AI_PROVIDER=deepseek`, ключ `DEEPSEEK_API_KEY` в `.env`; баланс проверяет общий
  мониторинг сервера). Модель `deepseek-flash` видит фото; у видео/GIF — кадр-заставку.
  Режим размышлений `DEEPSEEK_REASONING_EFFORT=high`.
- Промпты — в БД (`settings.ai.system_prompt`), копии в `agent-docs/prompt-*.txt`. В промпте — только канал и что
  значат оценки для него; формат ответа (JSON) бот дописывает сам (`ai.RUBRIC_FORMAT`). Промпт со старой строкой
  `Verdict: … | publish chance NN%` работает по-старому (процент от модели).
- Оценка: модель ставит `fit`, `quality`, `clarity` от 0 до 3, флаги `spam`/`unsafe`/`off_topic` и
  `authenticity_risk` low/medium/high. Решает код (`ai.rubric_decision`): флаг, high или оценка < 2 → reject;
  сумма в % ≥ `auto_publish_min_score` (75 = 7 из 9) → publish; на балл ниже → review («на грани», для модераторов).
  Результат у самого порога спрашивается ещё 2 раза, берётся медиана.
- Для трёх приёмников в `registered_bots.settings.ai.required_language` задано `en`.
  Проверяется язык основного текста, подписи и текста произведения на изображении.
  Результат `non_english` закрывает заявку сразу после проверки. Автору отправляются
  статус и редакционная причина. Оценки и разбор доступны в чате модераторов.
- Автопубликация: через `vote_timeout` (1 ч) пост без голосов «против» публикуется, если вердикт publish.
  Иначе — отказ (`ai_reject` / `timeout`).
- Если ИИ не ответил — пост не отклоняется по таймауту, анализ повторяется раз в 30 мин до 48 ч; владельцу приходит
  health alert в личку от бота-приёмника.

## Деплой

```bash
cd /opt/boterator && git pull && docker compose up -d --build app && docker logs --tail 30 boterator-app-1
```

Перед правкой настроек ботов в БД — дамп: `docker exec boterator-postgres-1 pg_dump -U boterator -d boterator
-t registered_bots -t ai_prompt_history > backups/<что>-<дата>/registered_bots.sql`.

## Проверка

Тесты запускаются командой `python -m unittest discover -s tests -v`. Для интеграционных
тестов использовать одноразовый PostgreSQL с отдельными базами `boterator_moderation_test`
и `boterator_reddit_test_20261117`, задав URL через `MODERATION_TEST_DATABASE_URL`
и `REDDIT_TEST_DATABASE_URL`. Telegram в тестах подменяется.
Коммиты — от `mysubcult <mysubcult1@gmail.com>`.

## Reddit RSS — с 17.11.2026

С 17 ноября публичные RSS больше не отдают записи для всех трёх лент. На 19 ноября
у владельца нет одобренного доступа к Reddit Data API. Права отправителя Telegram
подтверждены. Подробности и порядок подключения доступного источника — в `agent-docs/reddit-feeds.md`.

- Отдельный Compose-сервис `reddit`, контейнер `boterator-reddit-1`, в этом же
  проекте; код `boterator_app/reddit_feed.py`, настройки `reddit-feeds.json`,
  подключение `docker-compose.reddit.yml`. Основной `app` при запуске не перезапускался.
- Отправитель `@Reddit_poster_robot` — администратор `@mildlyinteresting`,
  `@showerthoughts`, `@todayilearned`; токен `/root/.secrets/reddit-publisher.env` (600).
- Решение владельца: публичный RSS, без заявки на Reddit API; три старых апплета
  IFTTT владелец подтвердил выключенными. При первом успешном чтении старая лента
  становится baseline, далее опрос раз в 15 минут, публикация всех новых ID.
  Ленты: Mildly — top/week, Shower — top/week, TIL — hot. Дневной квоты
  и случайных задержек нет. По решению владельца приоритет — более строгий недельный отбор. TIL: `[Source]`
  сразу в конце текста, превью включено. При смене источника — новая baseline без
  рассылки архива. Примерный объём 5–15/сутки — пожелание, не гарантия.
- Полные галереи из RSS пока не подтверждены: неполные отмечаются `held`, не отправляются
  одной миниатюрой. 403/429 от Reddit возможны. Управление, статусы, ограничения,
  тесты и откат — `agent-docs/reddit-feeds.md`. При пересоздании worker требуется
  `REDDIT_PUBLISH_ENABLED=1`; без этого отправка не включится.
