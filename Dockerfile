FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY connectors ./connectors
COPY skills ./skills
RUN pip install --no-cache-dir .
RUN mkdir -p /srv/data
EXPOSE 8090
CMD ["uvicorn", "app.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8090"]
