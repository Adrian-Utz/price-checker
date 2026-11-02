#!/bin/sh
set -e

# Create the session secret on first start. An existing secret_key (or PRICE_CHECKER_SECRET) is never replaced.
if [ -z "$PRICE_CHECKER_SECRET" ]; then
    python main.py init-secret >/dev/null
fi

exec "$@"
