FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

# Зависимости отдельным слоем — код меняется чаще, чем список пакетов.
# Ставим из слепка, а не из диапазонов: образ пересобирается когда придётся, и
# `aiogram>=3.31,<4` втянул бы тот минор, который вышел к этому дню. Чем
# обновлять — сказано в шапке requirements.lock
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY alembic.ini ./
COPY alembic ./alembic
COPY bot ./bot

# data/ монтируется томом; владелец нужен, чтобы non-root мог писать БД
RUN useradd -m -u 1000 planner && mkdir -p /app/data && chown -R planner:planner /app
USER planner

# Миграции накатываются при каждом старте: одна база, один процесс, откатов нет
CMD ["sh", "-c", "alembic upgrade head && python -m bot"]
