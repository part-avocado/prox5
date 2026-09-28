# prox5
It's a confession bot!

## Setup

Run the interactive setup wizard to configure your Slack tokens, channel IDs,
and encryption key:

```
python setup.py
```

This writes a `.env` file at the repo root. Then either:

```
pip install -r app/requirements.txt && python app/app.py
```

or, with Docker:

```
docker build -t prox5 . && docker run --env-file .env prox5
```
