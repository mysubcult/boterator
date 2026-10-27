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
  Результат у самого порога спрашивается ещё 2 раза, берётся медиана. Автору при отказе уходит `author_note`.
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

Тестов в репозитории нет. Проверять в одноразовом контейнере образа `boterator-app` с сетью `boterator_default`
и отдельной базой (`create database boterator_test` в контейнере postgres), живые заявки не трогать.
Коммиты — от `mysubcult <mysubcult1@gmail.com>`.
