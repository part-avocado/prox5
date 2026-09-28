# prox5
Confession bot!

## Setup, Docker
Use [Docker](https://www.docker.com/) to setup. 

Run: 
```
docker build -t prox5 .
docker run -it --rm prox5
```
then, 

```
touch .env
docker run -it --rm -v $(pwd)/.env:/app/.env prox5
```

Without a terminal attached (e.g. `docker run -d`), missing config makes
the container exit with an error instead of hanging on a prompt nobody can
answer — pass `--env-file .env` or `-e` flags for unattended/production use.
