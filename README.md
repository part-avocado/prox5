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
  -e ADMIN_USER_IDS=U0123456789,U0987654321 \
  -e REPORT_PASSPHRASE=<a-long-random-secret> \
  -v prox5-data:/app/data \
  prox5
```

The `-v prox5-data:/app/data` volume is where the sqlite database (prox5 submission history, moderation state, and the submission counter) lives. Without it, that data — including the submission count — resets every time the container is recreated.

## Identity protection ("Reject & Report")

A submitter's Slack ID is stored encrypted (`CONFESSIONS_KEY`, a Fernet key)
and is **never** decrypted or shown anywhere in Slack — not in the review
channel, not to moderators, not even to the person who files a report.

The "Reject & Report" button is the only thing that can ever decrypt an
identity, and it's deliberately hard to use:

- `ADMIN_USER_IDS` — comma-separated Slack user IDs allowed to even attempt
  a report. Everyone else is bounced immediately and the attempt is logged.
  Only whoever has shell/file access to edit the bot's `.env` can grant this.
- `REPORT_PASSPHRASE` — a separate shared secret that must be typed into the
  report modal on top of being an admin. Distribute it out-of-band (not over
  Slack) to the people who should actually be able to file reports, and
  rotate it by editing `.env` — which again requires host access.
- `REPORT_COOLDOWN_SECONDS` (default `60`) — rate-limits how often even an
  authorized admin can file a report, to prevent rapid bulk deanonymization.

On a successful report, the decrypted identity, message, and reason are
appended to a local log file (`REPORTS_LOG_PATH`, default
`data/reports.log`), created with `0600` permissions in a `0700` directory.
Nothing is posted to Slack besides a generic "rejected & reported"
notice. Reading that file — the only place a Slack ID is ever written in
the clear — requires shell/root access to the host running the bot.
Every attempt to report (granted or denied) is also written there for
auditing.
