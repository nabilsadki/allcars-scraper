#!/bin/sh
# Compile la feuille Tailwind en local (le build Docker fait la meme chose).
#
#   ./build-css.sh
#
# Necessite Node.js (npx). Sinon, Rebuild l'image :
#   docker compose build web
set -e
cd "$(dirname "$0")"

if ! command -v npx >/dev/null 2>&1; then
    echo "npx introuvable : installe Node.js, ou utilise : docker compose build web" >&2
    exit 1
fi

if [ ! -d node_modules/@tailwindcss/cli ]; then
    echo "installation de @tailwindcss/cli..."
    npm install --no-audit --no-fund --no-save @tailwindcss/cli@4 >/dev/null
fi

npx @tailwindcss/cli -i tailwind/input.css -o static/tailwind.css --minify
echo "static/tailwind.css : $(wc -c < static/tailwind.css) octets"
