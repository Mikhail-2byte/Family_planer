# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Язык проекта — русский: комментарии, докстринги, тексты бота и документация пишутся по-русски.

## Документы, которые задают работу

- `PLAN.md` — архитектура и **обоснования** решений (почему SQLite, почему не APScheduler,
  как бот ведёт себя в группе, риски). Читать, когда непонятно «почему так».
- `TASKS.md` — пошаговый чеклист реализации, 11 этапов, текущий прогресс и критерии
  готовности каждого пункта. **Отмечать выполненное здесь** (`[ ]` / `[~]` / `[x]`)
  и обновлять «Текущий этап» и таблицу прогресса.

Написано на сейчас: этапы 0, 1 и 2 — конфиг, модели, миграция, точка входа,
авторегистрация семьи, `timeutil`, `repo`, `texts`, `keyboards`, просмотр
(`handlers/views.py`), мастер `/new` (`handlers/new_entry.py`), фоновый тикер
(`services/ticker.py`), догонка, дайджест (`services/digest.py`), разбор дат
(`services/nlp_fallback.py`) и `/remind` (`handlers/remind.py`).
Пункты стоят как `[~]`: код и тесты есть, живых проверок в группе не было.

Ещё не написано: `services/panel.py` (этап 2п), `llm.py`, `voice.py`,
`handlers/capture.py`, `handlers/lists.py`.

## Команды

```bash
# Разработка без Docker (Windows, venv лежит в .venv)
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/alembic upgrade head      # создаёт data/family.db
.venv/Scripts/python -m bot             # long polling
run.cmd                                 # то же одной командой: миграции + запуск

.venv/Scripts/python -m pytest                                       # все тесты
.venv/Scripts/python -m pytest tests/test_timeutil.py -k week        # часть тестов
.venv/Scripts/alembic revision --autogenerate -m "описание"          # новая миграция
.venv/Scripts/alembic downgrade base                                 # откат

# Docker (боевой запуск на VPS)
docker compose up -d --build
docker compose logs -f
docker compose restart bot      # хватает после правки кода: bot/ и alembic/ смонтированы
```

Всё запускается **из корня проекта**: `bot/config.py` создаёт `Settings()` на импорте,
а `.env` ищется относительно текущей директории. Из другого каталога падает даже
`pytest` — `ValidationError: bot_token Field required`.

Пересборка образа нужна только при изменении `requirements.txt` или `Dockerfile`.
Миграции накатываются автоматически при каждом старте контейнера (`CMD` в `Dockerfile`).
Линтера в проекте нет.

## Архитектура

Telegram-бот (aiogram 3.x, long polling) для одного семейного группового чата.
SQLite через SQLAlchemy 2.0 async + aiosqlite, миграции — Alembic.

**Поток апдейта:** `bot/__main__.py` → `FamilyMiddleware` (outer middleware на `dp.update`)
→ роутеры из `bot/handlers/__init__.py`.

`FamilyMiddleware` (`bot/middlewares.py`) — центральное место, через которое проходит всё:
открывает `AsyncSession` на апдейт и кладёт в `data` ключи `session`, `family`, `member`.
Хендлеры получают их как аргументы и **не создают сессии сами**. В личке кладётся только
`session` — `family`/`member` отсутствуют, поэтому у групповых хендлеров с аргументом
`family: Family` обязателен фильтр `IN_GROUP`, иначе aiogram упадёт на резолве аргумента
в приватном чате.

`bot/filters.py` — `IN_GROUP` (для сообщений) и `IN_GROUP_CB` (для колбэков: у
`CallbackQuery` чат лежит внутри `message`). Групповые роутеры вешают их разом:
`router.message.filter(IN_GROUP)` + `router.callback_query.filter(IN_GROUP_CB)`.
Там же `IN_PRIVATE`: на нём в `admin.py` висит хендлер `private_chat` — ловушка,
отвечающая `PRIVATE_CHAT` на **любое** сообщение в личке. Она стоит в первом
роутере и без других фильтров, поэтому безопасна ровно до тех пор, пока у
`views`, `remind` и `new_entry` на `message` висит `IN_GROUP`: снимете фильтр —
хендлер молча уйдёт в отказ. Страховка — `test_group_routers_never_run_in_private`.

`bot/db/repo.py` — все запросы к БД. Принимает `AsyncSession` первым аргументом и ничего
не знает про aiogram; это делает его тестируемым напрямую (`tests/conftest.py`).

`bot/texts.py` — все русские строки **и единый рендер записей**: `entry_line`
(строка списка), `entry_card` (карточка), `day_header`, `week_header`. Форматировать
записи и даты в хендлерах нельзя — только звать эти функции.

`parse_mode="HTML"` включён по умолчанию, поэтому **любой** текст, введённый человеком,
идёт через `_escape`. Внутри `entry_line`/`entry_card` это уже сделано; для остальных
подстановок в `texts.py` есть функции-обёртки — `search_header`, `search_empty`,
`family_header`, `family_member`, `pong`. Подставлять `.format(...)` в шаблон прямо
из хендлера нельзя: имя участника и название чата задаёт пользователь, и `<` в них
превращает отправку в `TelegramBadRequest: can't parse entities`.

Новый роутер: создать модуль в `bot/handlers/`, объявить `router = Router()`,
добавить в список `routers` в `bot/handlers/__init__.py`. **Порядок в списке значим:**
`views` идёт раньше `new_entry`, иначе команда, набранная посреди `/new`, будет
проглочена FSM-хендлером как текст записи. Обратная сторона этого порядка —
`drop_wizard_state` (`bot/middlewares.py`): внутренний middleware, который вешается
в том же `__init__.py` на все роутеры **кроме мастера** и обрывает зависшее
состояние `/new`, когда сообщение перехватил кто-то другой. Новый роутер, стоящий
раньше `new_entry`, тоже должен его получить.

Фоновые сервисы (`services/ticker.py`, `services/digest.py`) идут мимо диспетчера,
а значит и мимо `FamilyMiddleware` — они открывают `Session()` сами и берут таймзону
из `families.tz` вручную. `Session` импортируется **по имени на уровне модуля**
(`from bot.db.session import Session`), иначе тест не сможет подменить фабрику
через `monkeypatch.setattr(ticker, "Session", session_maker)`.

Отправка в чат — только через `services/sending.py`: там политика ошибок Telegram
(выгнали → гасим, сломанный текст → сдаёмся сразу, сеть и флуд → ретрай на следующем
тике). Отдельный модуль, потому что политика нужна и тикеру, и дайджесту, а держать
её в `ticker.py` значило бы закольцевать импорты.

`digest.build_day` — единственное место, где день превращается в текст. Его зовёт и
утренняя сводка, и `/today`.

## Инварианты, которые легко нарушить

- **Никакого `/start`.** Семья и участники заводятся сами по первому же апдейту из группы
  (`get_or_create_family` / `get_or_create_member`). Не добавлять команду-инициализацию.
  Кнопка START в личке шлёт `/start`, и он попадает в общую ловушку `private_chat`,
  которая отвечает «работаю только в группе» и ничего не инициализирует.
- **Всё время в БД — naive UTC** (SQLite таймзон не знает). Таймзона семьи (`Family.tz`)
  применяется только на границе ввода и вывода — через `bot/services/timeutil.py`.
  Модуль чистый: ни БД, ни aiogram внутри.
- **У повторяющегося напоминания отработку помечает `fire_at`, а не `sent_at`.**
  Разовое закрывается через `sent_at = now`; повторяющееся получает новый `fire_at`
  в будущем, а `sent_at` остаётся пустым навсегда. Благодаря этому выборка тикера
  одна на оба случая. Не «чинить», проставляя `sent_at` повторяющимся, — оно
  замолчит навсегда.
- **`rrule` считается в локальном времени семьи**, якорь — текущий `fire_at`
  напоминания. Взять за якорь «сейчас» значит потерять время суток серии, а считать
  в UTC — уехать на час при переводе часов.
- **`bot/db/sqlite.py` импортируется ради побочного эффекта** (`# noqa: F401` в `repo.py`):
  он вешает на класс `Engine` регистрацию SQLite-функции `lower_unicode`. Встроенный
  `lower()` умеет только ASCII, без этого поиск по кириллице не работает. Там же
  выставляются `PRAGMA journal_mode=WAL` и `busy_timeout` — без них тикер и хендлер,
  пишущие одновременно, ловят `database is locked`. Не удалять «неиспользуемый» импорт.
- **`allowed_updates` вычисляется из зарегистрированных хендлеров**
  (`dp.resolve_used_update_types()`). Новый тип апдейта не начнёт приходить, пока на него
  нет хендлера, — симптом «Telegram молчит» без единой ошибки в логе.
- **На старте `delete_webhook(drop_pending_updates=True)`** — сообщения, пришедшие пока
  бот лежал, теряются намеренно. Догонка (этап 2) восстанавливает **напоминания из БД**,
  а не пропущенные сообщения.
- **FSM-состояния живут в памяти** (`MemoryStorage` по умолчанию): рестарт бота обрывает
  незавершённый `/new`. Осознанно; менять — только вместе с выбором хранилища.
- **Ролей и прав нет.** Бот доверяет всем участникам чата — осознанное упрощение.
  Изоляция только по `family_id`: колбэк из чужого чата не должен трогать чужую запись
  (см. `complete_entry`).
- **`edited_message` намеренно не обрабатывается.** Правка записи — через кнопку в карточке.
- **Миграция группы в супергруппу** меняет `chat_id`; она обрабатывается в middleware
  (`migrate_to_chat_id` / `migrate_from_chat_id`) — не ломать эту ветку.
- **Прокси заложены с этапа 0**: `TELEGRAM_PROXY` и `OPENROUTER_PROXY`, пустое значение =
  ходим напрямую. Все новые исходящие HTTP-клиенты должны уважать свой прокси.
  Прокси **обязателен** там, где `api.telegram.org` заблокирован: `aiohttp`, на
  котором работает aiogram, переменные `HTTP_PROXY`/`HTTPS_PROXY` игнорирует
  (`trust_env` по умолчанию выключен), поэтому системный прокси сам собой не
  подхватится — в отличие от `httpx`, который его уважает. Отсюда обманчивая
  картина: ручная проверка токена через `httpx` проходит, а бот молчит.
  Для `AiohttpSession(proxy=...)` нужен пакет `aiohttp-socks` (в
  `requirements.txt`) — без него aiogram отказывается создавать сессию вовсе.
- **Ничего не сохраняется молча.** Разбор текста всегда даёт карточку подтверждения.
- `.env`, `data/`, `token.json`, `parse.log` — никогда в git.

## Данные

Пять таблиц (`bot/db/models.py`): `families`, `members`, `lists`, `entries`, `reminders`.
`entries` — одна таблица на все типы записей, тип в поле `kind`
(`task` / `note` / `event` / `shopping`). `gcal_map` появится на этапе 7.

У `Entry` два внешних ключа на `members` (`author_id`, `done_by`), поэтому связи
`author` / `closer` объявлены с явным `foreign_keys` и `lazy="selectin"` — иначе
async-сессия упадёт на ленивой подгрузке автора при рендере строки.

`alembic/env.py` берёт URL из `bot.config.settings.db_url`, а не из `alembic.ini` —
один источник правды. Автогенерация настроена с `render_as_batch=True` (требование SQLite).

## Тесты

`pytest.ini`: `asyncio_mode = strict` — каждому async-тесту нужен `@pytest.mark.asyncio`,
async-фикстуре — `@pytest_asyncio.fixture`. Общие фикстуры лежат в `tests/conftest.py`:
`session_maker` (фабрика на общей БД в памяти), `session`, `family`, `anya`.
`session_maker` нужен там, где сессию открывает не тест, а сам код: например,
`FamilyMiddleware` зовёт `Session()` сам, и в тесте его подменяют через `monkeypatch`.

`tests/test_regressions.py` — по тесту на каждый баг, найденный ревизией этапов 0–1.
Тесты там написаны так, чтобы падать на коде «до правки»; если один из них станет
зелёным при откате фикса, он потерял смысл.

Схема в тестах строится через `Base.metadata.create_all` на `sqlite+aiosqlite:///:memory:`,
**миграции не участвуют**. Правка модели без новой ревизии Alembic даёт зелёные тесты и
падение на боевом старте — после изменения `models.py` всегда делать `revision --autogenerate`.

## Внешние зависимости и настройка

`BOT_TOKEN` от @BotFather обязателен. В BotFather нужно `/setprivacy → Disable`,
**после чего удалить бота из группы и добавить заново** — иначе он не увидит обычных
сообщений. Историю чата до своего добавления бот не видит в принципе.

LLM-разбор (этап 3a) идёт через OpenRouter; при пустом `OPENROUTER_KEY` бот должен
работать без него. Полный список переменных — в `.env.example`.

## Полезные скиллы окружения

`telegram-bot` (заготовки aiogram 3.x) и `docker` — оба уже применялись на этапе 0.
