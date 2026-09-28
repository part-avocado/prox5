FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY setup.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV RUNNING_IN_CONTAINER=1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "app.py"]
