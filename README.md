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

### Docker, configured interactively

You don't need to run `setup.py` yourself first. If the container starts
without its required config, it runs the same wizard for you:

```
docker build -t prox5 .
docker run -it --rm prox5
```

To keep those answers across container restarts/recreation, bind-mount an
`.env` file (create it empty first so Docker doesn't mount a directory
there instead):

```
touch .env
docker run -it --rm -v $(pwd)/.env:/app/.env prox5
```

Without a terminal attached (e.g. `docker run -d`), missing config makes
the container exit with an error instead of hanging on a prompt nobody can
answer — pass `--env-file .env` or `-e` flags for unattended/production use.
