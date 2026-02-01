from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    bot_token: str = Field(validation_alias="BOT_TOKEN")
    openai_api_key: str = Field(validation_alias="OPENAI_API_KEY")

    openai_text_model: str = Field(default="gpt-5.2", validation_alias="OPENAI_TEXT_MODEL")
    openai_vision_model: str = Field(default="gpt-5.2", validation_alias="OPENAI_VISION_MODEL")
    openai_transcribe_model: str = Field(default="gpt-4o-mini-transcribe", validation_alias="OPENAI_TRANSCRIBE_MODEL")
    # Model for heavy JSON plans (can be overridden in .env)
    openai_plan_model: str = Field(default="gpt-5.2", validation_alias="OPENAI_PLAN_MODEL")
    # Fast/cheap primary model for plans; fallback to openai_plan_model if quality/JSON fails
    openai_plan_model_fast: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_PLAN_MODEL_FAST")
    # Extra fallback model for plans (useful when some models return empty content)
    openai_plan_model_fallback: str = Field(default="gpt-4o", validation_alias="OPENAI_PLAN_MODEL_FALLBACK")
    # Plan-specific timeout (seconds). Keep lower than global to avoid 2-3 minute waits.
    openai_plan_timeout_s: int = Field(default=30, validation_alias="OPENAI_PLAN_TIMEOUT_S")
    # Hard timeout for OpenAI requests (seconds) to avoid "hangs"
    openai_timeout_s: int = Field(default=45, validation_alias="OPENAI_TIMEOUT_S")

    db_path: str = Field(default="data/botfit.sqlite3", validation_alias="DB_PATH")
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")

    default_country: str = Field(default="CZ", validation_alias="DEFAULT_COUNTRY")
    default_stores: str = Field(default="Lidl,Kaufland,Albert", validation_alias="DEFAULT_STORES")

    ffmpeg_path: str | None = Field(default=None, validation_alias="FFMPEG_PATH")

    off_base_url: str = Field(default="https://world.openfoodfacts.org", validation_alias="OFF_BASE_URL")
    off_country: str = Field(default="CZ", validation_alias="OFF_COUNTRY")
    off_page_size: int = Field(default=5, validation_alias="OFF_PAGE_SIZE")


settings = Settings()

