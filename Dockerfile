FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better Docker layer caching - only
# reinstalls when requirements.txt actually changes, not on every
# code edit)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the application code
COPY . .

# The SQLite database lives here - see fly.toml for the persistent
# volume mount that keeps this directory's contents across deploys
# and restarts (without it, the database resets every time the
# container restarts, since containers are otherwise ephemeral).
RUN mkdir -p /app/backend/database

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
