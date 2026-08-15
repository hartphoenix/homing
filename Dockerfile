# Build and runtime are deliberately pinned to a Debian/Python release. Update
# this tag only with a reviewed dependency/security update and rebuild the image.
FROM python:3.13.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

WORKDIR /opt/app
COPY pyproject.toml manage.py ./
COPY config ./config
COPY accounts ./accounts
COPY projects ./projects
RUN python -m pip install --no-cache-dir .

COPY --chown=app:app . .
RUN mkdir -p /opt/app/staticfiles \
 && DJANGO_SECRET_KEY=container-build-only DJANGO_DEBUG=0 python manage.py collectstatic --noinput \
 && chown -R app:app /opt/app/staticfiles

USER app
EXPOSE 8000

# compose supplies the migration-lock wrapper and its gunicorn arguments.
ENTRYPOINT []
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", "--timeout", "60", "config.wsgi:application"]
