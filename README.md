# prox5
Confession bot!

## Setup, Docker


A. Create the Slack app first

The bot needs a Slack app configured with socket mode.
1. Go to https://api.slack.com/apps → Create New App → From an app manifest.
2. Pick your workspace, paste in the contents of app/manifest.yaml (already defines the bot scopes, events, and enables socket mode).
3. After creation:
  - Install the app to your workspace -> copy the Bot User OAuth Token (xoxb-...) -> this is SLACK_BOT_TOKEN.
  - Under Basic Information -> App-Level Tokens, generate a token with the connections:write scope (xapp-...) → this is SLACK_APP_TOKEN.
  - Create/pick two channels: one for approved confessions, one private for moderators. Grab their channel IDs (right-click channel → View channel details → copy ID at the bottom) → CONFESSIONS_CHANNEL_ID and REVIEW_CHANNEL_ID.
  - Invite the bot to both channels.

4. Build the image
```
docker build -t prox5 .
```
6. Configure and run

Interactive Setup:
```
touch .env
docker run -it --rm -v $(pwd)/.env:/app/.env prox5
```

Production Running 
```
docker run -d --name prox5 --env-file .env prox5
```
or with explicit -e flags:

```
docker run -d --name prox5 \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e SLACK_APP_TOKEN=xapp-... \
  -e CONFESSIONS_CHANNEL_ID=C0123456789 \
  -e REVIEW_CHANNEL_ID=C0123456789 \
  -e CONFESSIONS_KEY=<fernet-key> \
  -e ALLOW_BROADCASTS_FROM_OP=0 \
  prox5
```
