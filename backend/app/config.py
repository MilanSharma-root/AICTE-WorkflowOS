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
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
settings = Settings()
