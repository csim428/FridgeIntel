# FridgeIntel, hosted. Python runs here, not in the browser.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py fridge_api.py server.py ./

# Supabase credentials come from the environment. config.py is deliberately
# not copied: it is for local development only.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
