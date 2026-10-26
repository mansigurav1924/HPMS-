from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ENV: str = "development"
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str
    SUPABASE_JWT_SECRET: str
    DATABASE_URL: str
    TURNSTILE_SECRET: str
    
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    
    HASH_PEPPER: str = "default-pepper"
    WEBHOOK_SECRET: str = "test-sig"
    EMAIL_PROVIDER: str = "stub"
    
    GOOGLE_MAPS_SERVER_KEY: str = ""

    # Comma-separated frontend origins allowed by CORS, e.g. https://hpms.vercel.app
    CORS_ORIGINS: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
