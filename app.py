"""
TEE KAEK Automation — local web app entry point.

WARNING: This tool must only be used for legally authorized KAEK searches.
         Never commit your .env file. Never share credentials.

Usage:
    pip install -r requirements.txt
    playwright install
    python app.py
"""
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import Config
from database.db import init_db
from api.routes import router
from auth import BasicAuthMiddleware

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Config.LOGS_DIR / "app.log" if Config.LOGS_DIR.exists() else "app.log"),
    ],
)
# Never log credentials
for sensitive in ("TEE_USERNAME", "TEE_PASSWORD", "ANTHROPIC_API_KEY"):
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TEE KAEK Automation",
    description="Local automation tool for authorized TEE/Ktimatologio KAEK research",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(BasicAuthMiddleware)
app.include_router(router)

# Serve static files (frontend)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(static_dir / "index.html"))


# ── Startup / Shutdown ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    Config.ensure_dirs()
    init_db()
    missing = Config.validate()
    if missing:
        logger.warning(
            "Missing environment variables: %s — edit .env before processing",
            missing,
        )
    else:
        logger.info("Configuration OK. TEE credentials loaded.")
    logger.info(
        "App running at http://%s:%s — ONLY USE FOR AUTHORIZED KAEK SEARCHES",
        Config.HOST, Config.PORT,
    )


@app.on_event("shutdown")
async def shutdown():
    from automation.browser_manager import stop_browser
    await stop_browser()
    logger.info("App shutdown complete")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  TEE KAEK Αυτοματοποίηση")
    print("  ΠΡΟΣΟΧΗ: Χρησιμοποιείτε ΜΟΝΟ για εξουσιοδοτημένες αναζητήσεις!")
    print("=" * 65)
    print(f"\n  → Άνοιγμα στο: http://{Config.HOST}:{Config.PORT}\n")

    uvicorn.run(
        "app:app",
        host=Config.HOST,
        port=Config.PORT,
        reload=False,
        log_level="info",
    )
