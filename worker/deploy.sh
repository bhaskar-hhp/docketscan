#!/usr/bin/env bash
# Deploy the DocketScan NIM proxy to Cloudflare Workers.
#
#   CLOUDFLARE_API_TOKEN=xxx NVIDIA_API_KEY=nvapi-xxx ./deploy.sh
#
# Token needs: Account → Workers Scripts:Edit, and Account → Account Settings:Read.
set -euo pipefail
cd "$(dirname "$0")"

: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN}"
: "${NVIDIA_API_KEY:?set NVIDIA_API_KEY}"

command -v npx >/dev/null || { echo "npx/node is required"; exit 1; }

echo "→ uploading secret NVIDIA_API_KEY"
printf '%s' "$NVIDIA_API_KEY" | npx --yes wrangler@latest secret put NVIDIA_API_KEY

echo "→ deploying worker"
npx --yes wrangler@latest deploy 2>&1 | tee /tmp/docketscan-deploy.log

URL=$(grep -Eo 'https://[a-z0-9.-]+\.workers\.dev' /tmp/docketscan-deploy.log | head -1 || true)
if [ -n "${URL:-}" ]; then
  echo
  echo "Worker URL: $URL"
  echo "Now set it in docs/config.js:"
  echo "  window.DOCKETSCAN_API = \"$URL\";"
else
  echo "Could not detect the URL — copy it from the output above."
fi
