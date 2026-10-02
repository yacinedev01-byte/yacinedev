FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget git unzip binutils file \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (playwright install --with-deps chromium || true)
COPY app.py agent_cmds.py ./

ENV SHELL_WORKROOT=/tmp/yd_sandbox
EXPOSE 8080
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "--timeout", "150", "app:app"]
