# prox5

prox5 is an anonymous Slack submission bot with per-user passkey encryption.

## Passkeys

Every user must open their DM with prox5 and run `/prox5-pass` before submitting. The user chooses and types their own passkey; prox5 does not generate one for them. Passkeys must contain 12–256 characters.

For each user, prox5:

- generates a random 128-bit KDF salt;
- derives a distinct 256-bit content key from the supplied passkey with scrypt;
- encrypts stored confession text and DM-channel references with authenticated Fernet encryption; and
- encrypts the supplied passkey at rest with the deployment `CONFESSIONS_KEY`.

The deployment key is now a key-encryption key: it wraps user passkeys, but it does not directly encrypt new submission content. Two users who choose the same passkey still receive different content keys because their salts differ.

Running `/prox5-pass` again asks for the current passkey and a replacement. `/prox5-rotate` allows a user to choose a replacement without entering the previous passkey; access to that user's Slack DM is the authorization for this recovery operation. In both cases, prox5 re-encrypts every record for that user in one database transaction. Existing records from versions before 1.4 are migrated from the legacy global encryption when that user first registers a passkey.

### Security boundary

This protects data stored in prox5's SQLite database with a user-specific key. Slack messages still pass through Slack and the bot as plaintext, protected in transit by Slack's TLS; a Slack bot cannot provide end-to-end encryption from inside the Slack client. Approved posts and moderator review messages are also visible in their respective Slack channels.

Slack Block Kit does not provide a password-masked input element, so the passkey is visible while the user types it into the modal and is received by Slack. Users should choose a unique prox5 passkey and must not reuse a password from another service.

Because prox5 must relay messages while users are offline, it must be able to recover each saved passkey. `CONFESSIONS_KEY` provides that wrapping layer and must be kept outside the database (for example, in a secrets manager or protected environment variable). A database-only leak does not expose plaintext passkeys or content, but compromise of both the database and `CONFESSIONS_KEY` can decrypt them.

## Setup

1. Create a Slack app from [`app/manifest.yaml`](app/manifest.yaml), then install it to the workspace.
2. Run `python3 setup.py` and provide the Slack tokens and channel IDs.
3. Install dependencies with `python3 -m pip install -r app/requirements.txt`.
4. Start the bot with `python3 app/app.py`.

Docker is also supported:

```sh
docker build -t prox5 .
docker run --env-file .env -v prox5-data:/app/data prox5
```

The required deployment variables are `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `CONFESSIONS_CHANNEL_ID`, `REVIEW_CHANNEL_ID`, and `CONFESSIONS_KEY`. `DB_PATH` defaults to `data/confessions.db`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
