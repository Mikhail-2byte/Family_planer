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

    openrouter_key: str = ""
    openrouter_model: str = ""
    openrouter_model_cheap: str = ""

    # Пустое значение = работаем напрямую, без прокси
    telegram_proxy: str = ""
    openrouter_proxy: str = ""

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()  # type: ignore[call-arg]
