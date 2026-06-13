#!/usr/bin/env bash
set -e

pip install -r requirements.txt

# Install Playwright Chromium into the project directory so the path is
# predictable at runtime (avoids ~HOME mismatch between build and runtime).
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/pw-browsers
playwright install chromium
echo "Chromium installed at $PLAYWRIGHT_BROWSERS_PATH"
