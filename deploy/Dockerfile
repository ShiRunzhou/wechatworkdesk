FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY workdesk ./workdesk
COPY tools ./tools
COPY docs ./docs
RUN pip install --no-cache-dir '.[cloud]' && useradd --uid 10001 --no-create-home workdesk
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WCWD_SECRET_KEY_FILE=/run/wcwd/model.key
CMD ["python", "-m", "workdesk", "--data", "/var/lib/wechatworkdesk", "serve", "--website-config", "/run/wcwd/website.json"]
