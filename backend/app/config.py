from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AICTE WorkflowOS API"
    app_env: str = "development"
    database_url: str = "sqlite:///./aicte_workflowos.db"
    cors_origins: str = "http://localhost:5173"
    secret_key: str = "CHANGE_THIS_SECRET_FOR_PRODUCTION"
    token_hours: int = 8
    upload_dir: str = "storage/documents"
    max_upload_mb: int = 10

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_runtime_security(self):
        if self.token_hours < 1 or self.token_hours > 24 * 30:
            raise ValueError("TOKEN_HOURS must be between 1 and 720")
        if self.max_upload_mb < 1 or self.max_upload_mb > 100:
            raise ValueError("MAX_UPLOAD_MB must be between 1 and 100")
        if self.app_env.strip().lower() in {"prod", "production"}:
            if self.secret_key == "CHANGE_THIS_SECRET_FOR_PRODUCTION" or len(self.secret_key) < 32:
                raise ValueError("Production requires a custom SECRET_KEY with at least 32 characters")
            if "*" in self.cors_origins:
                raise ValueError("Wildcard CORS origins are not allowed in production")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
