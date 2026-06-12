"""
TEE/Ktimatologio KAEK Automation - Configuration
WARNING: This tool must only be used for legally authorized KAEK searches.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent


class Config:
    # Credentials - never printed in logs
    TEE_USERNAME: str = os.getenv("TEE_USERNAME", "")
    TEE_PASSWORD: str = os.getenv("TEE_PASSWORD", "")

    # Portal URLs - verify and update after inspecting the live portal
    TEE_SSO_URL: str = os.getenv(
        "TEE_SSO_URL",
        "https://sso.tee.gr/auth/realms/tee/protocol/openid-connect/auth"
    )
    KTIMATOLOGIO_PORTAL_URL: str = os.getenv(
        "KTIMATOLOGIO_PORTAL_URL",
        "https://www.ktimatologio.gr/"
    )
    MAP_BASE_URL: str = os.getenv(
        "MAP_BASE_URL",
        "https://maps.ktimatologio.gr/"
    )

    # Human-like pacing (seconds)
    MIN_DELAY: float = float(os.getenv("MIN_DELAY", "2.0"))
    MAX_DELAY: float = float(os.getenv("MAX_DELAY", "5.0"))
    PAGE_LOAD_TIMEOUT: int = int(os.getenv("PAGE_LOAD_TIMEOUT", "30000"))  # ms
    ELEMENT_TIMEOUT: int = int(os.getenv("ELEMENT_TIMEOUT", "15000"))       # ms

    # Browser
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    BROWSER_TYPE: str = os.getenv("BROWSER_TYPE", "chromium")  # chromium|firefox|webkit

    # Retry
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "2"))

    # Paths
    DOWNLOADS_DIR: Path = BASE_DIR / "downloads"
    ERRORS_DIR: Path = BASE_DIR / "errors"
    LOGS_DIR: Path = BASE_DIR / "logs"
    DB_PATH: Path = BASE_DIR / "kaek_automation.db"

    # Server
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8080"))

    # Optional LLM for Greek text classification
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_ENABLED: bool = bool(os.getenv("ANTHROPIC_API_KEY", ""))

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing required settings."""
        missing = []
        if not cls.TEE_USERNAME:
            missing.append("TEE_USERNAME")
        if not cls.TEE_PASSWORD:
            missing.append("TEE_PASSWORD")
        return missing

    @classmethod
    def ensure_dirs(cls) -> None:
        for d in (cls.DOWNLOADS_DIR, cls.ERRORS_DIR, cls.LOGS_DIR):
            d.mkdir(parents=True, exist_ok=True)
