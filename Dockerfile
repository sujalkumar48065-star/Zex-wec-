FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PANEL_ENABLE=1
ENV HBI_DISABLE=1
EXPOSE $PORT
CMD gunicorn render_app:app --workers 1 --threads 1 --timeout 120