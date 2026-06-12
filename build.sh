#!/usr/bin/env bash
set -e

pip install -r requirements.txt

# Install Playwright's Chromium browser binary.
# We skip install-deps because it requires root (su) which is unavailable
# on Render's native Python runner. Render's Ubuntu 22.04 image already
# ships the system libraries Chromium needs.
playwright install chromium
