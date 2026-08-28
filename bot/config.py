from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str
    db_path: Path = Path("data/family.db")

    tz_default: str = "Europe/Moscow"
    digest_time: str = "08:00"
    tick_seconds: int = 60

    # Пороги догонки пропущенного (см. PLAN.md — «Логика догонки»)
    late_silent_min: int = 10
    late_summary_hours: int = 12

    # Живая панель дня (этап 2п). Дебаунс склеивает серию правок в одно
    # редактирование; порог — после скольких сообщений панель уехала вверх
    # настолько, что проще выпустить новую, чем править невидимую.
    # Дебаунс дробный намеренно: тест ставит 0.01 и не ждёт секунду на прогон
    panel_max_messages: int = 20
    panel_debounce_seconds: float = 2.0

    # Ежедневный бэкап (этап 6): сколько копий держать в `data/backups/`.
    # 0 = ежедневный снимок выключен, но `/backup` работает — он в каталог
    # не заглядывает вовсе
    backup_keep: int = 7

    openrouter_key: str = ""
    openrouter_model: str = ""

    # Порог автосохранения разбора без карточки подтверждения (шаг 3b.6).
    # 0 = выключено, и это значение по умолчанию: инвариант «ничего не
    # сохраняется молча» снимается только осознанно, когда по `parse.log`
    # станет видно, что модель на высокой уверенности не ошибается
    autosave_confidence: float = 0.0

    # Расшифровка голосовых (этап 5). Отдельный вендор и отдельный ключ:
    # Whisper-совместимый эндпоинт Groq принимает ogg/opus Telegram как есть,
    # поэтому ffmpeg проекту не нужен, а его суточная квота не пересекается
    # с лимитом OpenRouter, на котором держится текстовый разбор
    stt_key: str = ""
    stt_model: str = "whisper-large-v3-turbo"
    stt_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"

    # Потолок длины голосового и срок жизни приглашения по кнопке «🎤 Голосом»
    voice_max_seconds: int = 120
    voice_window_seconds: int = 300

    # Пустое значение = работаем напрямую, без прокси
    telegram_proxy: str = ""
    openrouter_proxy: str = ""
    stt_proxy: str = ""

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()  # type: ignore[call-arg]
