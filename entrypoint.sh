#!/bin/sh
set -e

required="SLACK_BOT_TOKEN SLACK_APP_TOKEN CONFESSIONS_CHANNEL_ID REVIEW_CHANNEL_ID CONFESSIONS_KEY"

need_setup=0
for var in $required; do
    eval "value=\${$var}"
    if [ -z "$value" ]; then
        need_setup=1
    fi
done

if [ "$need_setup" = "1" ] && [ ! -f .env ]; then
    if [ -t 0 ]; then
        echo "No configuration found, launching interactive setup..."
        python setup.py
    else
        echo "Missing required Slack configuration and no terminal attached for interactive setup." >&2
        echo "Re-run with 'docker run -it', or provide via -e/--env-file: $required" >&2
        exit 1
    fi
fi

exec "$@"
