# prox5
prox5 submit bot!

## Setup, Docker
0. Download. Run `docker pull ghcr.io/part-avocado/prox5:latest`

A. Create the Slack app first

The bot needs a Slack app configured with socket mode.
1. Go to https://api.slack.com/apps → Create New App → From an app manifest.
2. Pick your workspace, paste in the contents of app/manifest.yaml (already defines the bot scopes, events, and enables socket mode).
3. After creation:
  - Install the app to your workspace -> copy the Bot User OAuth Token (xoxb-...) -> this is SLACK_BOT_TOKEN.
  - Under Basic Information -> App-Level Tokens, generate a token with the connections:write scope (xapp-...) → this is SLACK_APP_TOKEN.
  - Create/pick two channels: one for approved prox5 submissions, one private for moderators. Grab their channel IDs (right-click channel → View channel details → copy ID at the bottom) → CONFESSIONS_CHANNEL_ID and REVIEW_CHANNEL_ID.
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
docker run -d --name prox5 --env-file .env -v prox5-data:/app/data prox5
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
  -v prox5-data:/app/data \
  prox5
```

The `-v prox5-data:/app/data` volume is where the sqlite database (prox5 submission history, moderation state, and the submission counter) lives. Without it, that data — including the submission count — resets every time the container is recreated.

## Identity protection

A submitter's Slack ID is stored encrypted (`CONFESSIONS_KEY`, a Fernet key)
in the `user_enc` column. **The bot itself never decrypts it, for any
reason.** There is no button, slash command, admin role, or Slack action
that reveals a submitter's identity — moderators only ever see Approve and
Reject. That column is write-only from the bot's perspective.

The only way to recover an identity is `app/deanon.py`, a separate CLI
tool with no Slack or network surface at all. It has to be run directly on
the host/container with shell access, e.g.:

```
docker exec -it prox5 python3 deanon.py <confession-id>
```

It's intentionally slow and manual, not a shortcut:

- It takes an internal database `id`, not a public post number — you have
  to open the sqlite DB yourself (`sqlite3 data/confessions.db`) to find it.
- It prints a fresh one-time code you must retype exactly; the code changes
  every run, so it can't be scripted or replayed from shell history.
- It requires typing a non-empty reason for the lookup.
- It then makes you sit through a mandatory countdown (`DEANON_DELAY_SECONDS`,
  default `20`) before showing the result.
- Every attempt — success, wrong code, empty reason, or abort — is appended
  to `REPORTS_LOG_PATH` (default `data/reports.log`), created `0600` in a
  `0700` directory. That file is the only place a Slack ID is ever written
  in the clear, and reading it requires shell/root access to the host.

This assumes whoever has shell/root access to the host is already trusted
and secured — the friction here exists to stop casual or in-Slack
deanonymization, not to defend against a compromised host.
