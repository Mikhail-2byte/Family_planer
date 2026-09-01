"""Клиент моделей и защитный разбор их ответа (этап 3a, цепочка — 12).

Наружу не выпускает исключений: при любом сбое — сети, отказе провайдера,
пустом ключе — возвращает `Answer` с непустым `reason`. Бот обязан работать
без LLM, запасной путь на `dateparser` подключён на шаге 3b.1.

Почему причина, а не `None` (этап 12). До него двенадцать разных бед
схлопывались в один `None`, и человек в чате читал одну и ту же строку
«Разобрал без ИИ» — что при пустом ключе, что при исчерпанном лимите, что при
401 от провайдера. 01.09.2026 на этом нельзя было понять, сломан ли бот вообще.

Почему цепочка, и почему она межвендорная (этап 12). У бесплатной модели
OpenRouter провайдер обычно ровно один: у `minimax/minimax-m3:free` это
GMICloud, и его 401 (`is_byok=false` — ключ самого OpenRouter к провайдеру, а
не наш) убил разбор целиком. Смысл списка не в качестве моделей, а в
**независимости отказов**, и разные вендоры дают её надёжнее, чем разные
провайдеры внутри одного OpenRouter: у последних общий фронт, общий биллинг и
общая страница статуса.

Запись цепочки — `вендор:модель`; без префикса подразумевается `openrouter`,
чтобы старые `.env` и исторические строки `parse.log` читались как раньше.

Вендоры отличаются не только адресом и ключом, но и телом запроса: OpenRouter
принимает `provider.data_collection=deny` (шаг 3a.0), а Groq на это же поле
отвечает `400 property 'provider' is unsupported`. Отсюда `_Endpoint.extra` —
добавка к payload, своя у каждого.

Почему разбор ответа отдельно и с оговорками: ни `minimax/minimax-m3:free`, ни
`openai/gpt-oss-120b` не заявляют `structured_outputs` — строгой гарантии схемы
нет, только `response_format`. На отборе модели один ответ из восьми пришёл
завёрнутым в markdown-блок, так что обёртка вокруг JSON — не теоретический
случай.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace

import httpx

from bot.config import settings
from bot.services import http_retry

log = logging.getLogger(__name__)

OPENROUTER = "openrouter"
GROQ = "groq"
VENDORS = frozenset({OPENROUTER, GROQ})
DEFAULT_VENDOR = OPENROUTER

URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Политика повторов общая с расшифровкой голоса — см. `http_retry`. Раньше цикл
# и константы existed здесь и там построчно одинаковыми, и разошлись бы молча.
# Имена оставлены модулю: на них смотрят тесты и читатель, пришедший сюда
TIMEOUT = http_retry.TIMEOUT
ATTEMPTS = http_retry.ATTEMPTS
BACKOFF = http_retry.BACKOFF
RETRY_CODES = http_retry.RETRY_CODES

# Потолок ожидания на ВСЮ цепочку, а не на модель, и равен потолку одиночного
# вызова намеренно: человек в чате ждёт ровно столько же, сколько ждал до
# этапа 12 — инвариант «ожидание внешнего вызова ограничено сверху» цепочка не
# отменяет. Размен, который отсюда следует, принят сознательно: быстрый отказ
# (401, 429, 5xx приходят за секунды — как 01.09.2026) успевает перебрать все
# модели, а молчание первой съедает бюджет целиком, и до запасных дело не
# дойдёт. Лечить это удлинением ожидания хуже, чем не лечить
CHAIN_BUDGET = http_retry.TOTAL_BUDGET

# Меньше этого остатка следующую модель не начинаем: успеть она не успеет, а
# человеку добавит ожидания на пустом месте
MIN_SLICE = 20.0

# Причины, по которым разбора не случилось. Простые строки, а не `Enum`:
# перечислений в проекте нет ни одного (наборы значений заданы кортежами, см.
# `parsing.KINDS`), а строка вдобавок ложится в JSON `parse.log` как есть и
# читается там глазами через полгода
UNAVAILABLE = "unavailable"  # сеть, таймаут, отказ провайдера, вся цепочка
BAD_JSON = "bad_json"  # модель ответила, но не JSON
NO_KEY = "no_key"  # ни у одного звена цепочки нет ключа
NO_MODEL = "no_model"  # цепочка пуста
# Единственная причина, которую ставит не этот модуль, а `capture`: до вызова
# мы при исчерпанной квоте не доходим. Живёт здесь, чтобы список причин был один
QUOTA = "quota"

# Проба связи для `/ai`. Своя, а не `parsing.build_system`: тому нужны сессия,
# семья и списки, а проверять надо транспорт. `json_object` в пробе обязателен —
# именно на нём валятся модели, у которых он заявлен, но не работает
PROBE_SYSTEM = 'Ты отвечаешь только JSON. Ответь ровно {"ok": 1} и ничего больше.'
PROBE_USER = "проверка связи"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Answer:
    """Итог обращения к модели: либо разбор, либо причина отказа.

    `__bool__` не определён намеренно: `http_retry.with_retries` возит этот
    объект как результат и проверяет его через `is not None`. Собственная
    истинность, ложная при пустом разборе, однажды превратила бы «модель
    ответила пустым объектом» в «модель не ответила».
    """

    data: dict | None = None
    reason: str = ""  # пусто ровно тогда, когда разбор удался
    model: str = ""  # фактически ответившая (или последняя пробованная)
    tried: tuple[str, ...] = ()  # вся цепочка в порядке перебора
    detail: str = ""  # текст провайдера; только для лога и `/ai`, не для чата
    calls: int = 0  # сколько запросов реально ушло — на них потрачена квота
    elapsed: float = 0.0  # секунды, для `/ai`

    @property
    def ok(self) -> bool:
        return self.data is not None


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """Куда идти за вендором и чем дополнить тело запроса."""

    vendor: str
    url: str
    key: str
    proxy: str
    extra: dict = field(default_factory=dict)


def endpoint(vendor: str) -> _Endpoint | None:
    """Адрес, ключ и добавка к payload. Неизвестный вендор — `None`.

    Читается на каждый вызов, а не складывается в таблицу на импорте:
    `settings` — синглтон, и тесты подменяют в нём поля.
    """
    if vendor == GROQ:
        # Ключ и прокси у аккаунта одни на расшифровку и на разбор; `STT_*`
        # просто появились раньше. Вторая строка с тем же секретом развела бы
        # их со временем
        return _Endpoint(
            GROQ,
            GROQ_URL,
            settings.groq_key or settings.stt_key,
            settings.groq_proxy or settings.stt_proxy,
        )
    if vendor == OPENROUTER:
        return _Endpoint(
            OPENROUTER,
            URL,
            settings.openrouter_key,
            settings.openrouter_proxy,
            # Дублирует настройку аккаунта (шаг 3a.0): приватность не должна
            # зависеть от галки, которую можно вернуть или потерять со сменой
            # ключа. Groq это поле не принимает вовсе — см. докстроку модуля
            {"provider": {"data_collection": "deny"}},
        )
    return None


def split_entry(entry: str) -> tuple[str, str]:
    """`groq:openai/gpt-oss-120b` → `("groq", "openai/gpt-oss-120b")`.

    Без префикса — `openrouter`: так читаются и старые `.env`, и строки
    `parse.log`, написанные до появления второго вендора.

    Префиксом считается часть до первого двоеточия, **если в ней нет слэша**.
    Это не придирчивость: у моделей OpenRouter двоеточие есть в самом имени —
    `minimax/minimax-m3:free`. Разрезав по первому попавшемуся, мы получили бы
    вендора «minimax/minimax-m3» и модель «free», то есть сломали бы строку,
    работающую с этапа 3a. Слэш и разводит эти два случая: имя модели там —
    всегда `организация/модель`, а имя вендора — одно слово.

    Неизвестное слово в префиксе возвращается как есть, а не подменяется на
    `openrouter`. Иначе опечатка (`gorq:`, `Groq:`) молча превращалась бы в имя
    несуществующей модели OpenRouter: звено тратило бы запрос, получало 404 и
    уходило дальше — то есть цепочка работала бы, а `.env` врал. Пусть лучше
    `endpoint` не найдёт такого вендора и звено будет пропущено со внятной
    строкой в логе.
    """
    vendor, sep, model = entry.partition(":")
    if not sep or "/" in vendor:
        return DEFAULT_VENDOR, entry.strip()
    return vendor.strip().lower(), model.strip()


def model_chain() -> list[str]:
    """Цепочка моделей в порядке перебора, без пустых и повторов.

    `LLM_MODELS` — основной источник. `OPENROUTER_MODEL` осталась как
    совместимость: она лежит в `.env` с этапа 3a, и молча перестать её читать
    значило бы выключить разбор у всех, кто не заглянул в `.env.example`.
    """
    raw = settings.llm_models or settings.openrouter_model
    chain: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def _links(chain: list[str]) -> list[tuple[str, str, _Endpoint]]:
    """Звенья, к которым есть смысл идти: известный вендор и непустой ключ.

    Отсев делается **до** перебора, а не по ходу, и это не украшение. От длины
    пригодного списка зависит, кто получит полную политику повторов; считая по
    исходной цепочке, мы отнимали бы попытку у последней живой модели ровно
    тогда, когда уходить с неё некуда. По этому же списку отчитывается `/ai`:
    называть «пробовал» модель, к которой не обращались, — врать в команде,
    заведённой ради правды.
    """
    links: list[tuple[str, str, _Endpoint]] = []
    for entry in chain:
        vendor, model = split_entry(entry)
        point = endpoint(vendor)
        if point is None:
            # Опечатка в `.env` не должна выключать разбор целиком
            log.warning("Неизвестный вендор в цепочке: %s", entry)
            continue
        if not point.key:
            # Ключа нет — звено пропускаем, а не роняем цепочку: одного
            # настроенного вендора хватает, чтобы бот разбирал
            log.warning("Ключ %s не задан — пропускаем %s", vendor, entry)
            continue
        links.append((entry, model, point))
    return links


async def ask(system: str, user: str, *, models: list[str] | None = None) -> Answer:
    """Спросить модель (и запасные) и вернуть разбор либо причину отказа."""
    chain = models if models is not None else model_chain()
    if not chain:
        log.warning("LLM_MODELS пуст — разбор текста выключен")
        return Answer(reason=NO_MODEL)

    links = _links(chain)
    if not links:
        # Раньше пустой ключ был единственной веткой вообще без строки лога —
        # на вопрос «почему разбор молчит» не отвечало ничего
        log.warning("Ни у одного звена цепочки нет ключа — разбор выключен")
        return Answer(reason=NO_KEY, tried=tuple(chain))

    started = time.monotonic()
    outcome = Answer(reason=UNAVAILABLE)
    spent = 0
    try:
        for number, (entry, model, point) in enumerate(links):
            left = CHAIN_BUDGET - (time.monotonic() - started)
            if number and left < MIN_SLICE:
                log.warning("Цепочка: на %s не осталось времени (%.0f с)", entry, left)
                break
            # Последнему звену достаётся полная политика повторов, всем
            # остальным — на попытку меньше: пока есть куда уйти, третий заход
            # в моргнувшего провайдера стоит человеку паузы и не даёт ничего
            attempts = ATTEMPTS if number == len(links) - 1 else ATTEMPTS - 1
            outcome = await _one_model(
                point,
                system,
                user,
                entry,
                model,
                attempts=attempts,
                budget=min(left, http_retry.TOTAL_BUDGET),
            )
            spent += outcome.calls
            if outcome.ok:
                break
            log.warning(
                "Модель %s не ответила (%s: %.120s) — идём к следующей",
                entry,
                outcome.reason,
                outcome.detail,
            )
    except Exception:
        # Контракт «наружу исключений не выпускает» абсолютен, поэтому ловим
        # весь блок, а не только конструктор клиента. `CancelledError` сюда не
        # попадёт: он наследуется от `BaseException`, а не от `Exception`
        log.warning("Не удалось обратиться к модели", exc_info=True)
        outcome = Answer(reason=UNAVAILABLE, detail="сбой до ответа")

    return replace(
        outcome,
        tried=tuple(entry for entry, _, _ in links),
        calls=spent,
        elapsed=time.monotonic() - started,
    )


async def _one_model(
    point: _Endpoint,
    system: str,
    user: str,
    entry: str,
    model: str,
    *,
    attempts: int,
    budget: float,
) -> Answer:
    """Одна модель со всеми её повторами.

    Клиент на модель, а не на цепочку: у вендоров разные прокси. Живой
    `AsyncClient` на модуль пришлось бы закрывать на остановке бота, а запросов
    здесь единицы в день.

    Конструктор стоит внутри `try` в `ask`: до этапа 12 он был снаружи, и
    кривой прокси выпускал исключение наружу вопреки докстроке модуля — хендлер
    падал до карточки, а упавший апдейт теряется навсегда.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        **point.extra,
    }

    # Потратить мы могли несколько попыток, а `with_retries` отдаёт только
    # последнюю — поэтому счётчик снаружи цикла. Список, а не число: замыкание
    # должно уметь его править
    tally = [0]
    async with httpx.AsyncClient(proxy=point.proxy or None) as client:
        outcome = await http_retry.with_retries(
            lambda: _try_once(client, point, payload, tally),
            what=entry,
            attempts=attempts,
            budget=budget,
        )
    if outcome is None:  # попытки или бюджет исчерпаны
        return Answer(
            reason=UNAVAILABLE, model=entry, calls=tally[0], detail="нет ответа"
        )
    return replace(outcome, model=entry, calls=tally[0])


async def _try_once(
    client: httpx.AsyncClient, point: _Endpoint, payload: dict, tally: list[int]
) -> Answer | None:
    """Итог по этой модели, либо `None` — «имеет смысл повторить».

    Контракт с `http_retry` тот же, что был до этапа 12; изменилось только то,
    что окончательный исход теперь несёт причину, а не пустую строку.
    """
    try:
        response = await client.post(
            point.url,
            headers={"Authorization": f"Bearer {point.key}"},
            json=payload,
            timeout=TIMEOUT,
        )
    except Exception:
        # Попытку не считаем: до провайдера мы могли не дойти вовсе (DNS, прокси,
        # оборванная сеть). Засчитывать её значило бы при лежащем интернете
        # выесть суточный лимит, не отправив ни одного запроса
        log.warning("%s недоступен", point.vendor, exc_info=True)
        return None

    # Любой HTTP-ответ — потраченный запрос аккаунта, включая 401 и 429. До
    # этапа 12 считались только удачные разборы, и защита от лимита не
    # срабатывала ровно в сценарии 429, ради которого написана
    tally[0] += 1

    if response.status_code in RETRY_CODES:
        log.warning("%s ответил %s — повторим", point.vendor, response.status_code)
        return None
    if response.status_code != 200:
        log.error(
            "%s отказал: %s %.200s",
            point.vendor,
            response.status_code,
            response.text,
        )
        # Повторять этой моделью бессмысленно, но у цепочки есть следующая:
        # 401 бывает свойством провайдера, а не нашего ключа
        return Answer(
            reason=UNAVAILABLE,
            detail=f"{response.status_code} {response.text[:160]}",
        )

    try:
        body = response.json()
    except ValueError:
        log.error("%s вернул не JSON: %.200s", point.vendor, response.text)
        return Answer(reason=UNAVAILABLE, detail="ответ не JSON")

    # Ошибка провайдера приезжает и с кодом 200 — с полем error в теле
    if body.get("error"):
        log.warning("Провайдер отказал: %.200s", str(body["error"]))
        return None

    choices = body.get("choices") or []
    if not choices:
        log.error("%s вернул ответ без choices: %.200s", point.vendor, str(body))
        return Answer(reason=UNAVAILABLE, detail="ответ без choices")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        # Рассуждающие модели умеют обрываться, оставив content пустым
        log.warning("Пустой ответ, finish_reason=%s", choices[0].get("finish_reason"))
        return None

    # Разбор JSON здесь, а не этажом выше, — и в этом смысл цепочки: модель,
    # ответившая мусором вместо JSON, передаёт ход следующей, а не роняет разбор
    parsed = extract_json(content)
    if parsed is None:
        log.warning("Модель вернула не JSON: %.200s", content)
        return Answer(reason=BAD_JSON, detail=content[:160])
    return Answer(data=parsed)


def extract_json(raw: str) -> dict | None:
    """Достать объект JSON из ответа модели. Мусор — `None`, не исключение.

    Три случая с отбора модели: чистый JSON; JSON внутри markdown-обёртки;
    текст, в котором JSON — только часть.
    """
    if not raw:
        return None
    text = raw.strip()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        value = json.loads(text)
    except ValueError:
        pass
    else:
        # Разобралось — вердикт окончательный. Если это не объект (например
        # голый массив items), вырезать из него внутренний `{...}` нельзя:
        # получился бы первый элемент, выданный за весь ответ. Честнее отказать
        # и уйти на запасной разбор, чем тихо подменить смысл
        return value if isinstance(value, dict) else None

    # Не разобралось целиком — последняя попытка: от первой скобки до
    # последней. Помогает, когда модель приписала «Вот разбор:» или хвост
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None
