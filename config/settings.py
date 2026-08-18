"""Django settings with a safe SQLite fallback for local development."""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
configured_allowed_hosts = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
# Gunicorn's container-local health probe uses loopback, while Caddy supplies
# the public host for real requests. Keeping these explicit local names avoids
# weakening host validation with a wildcard.
ALLOWED_HOSTS = list(dict.fromkeys(configured_allowed_hosts + ["localhost", "127.0.0.1", "[::1]"]))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "projects",
    "api",
    "tracker",
    "agentkit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("postgres"):
    import urllib.parse

    parsed = urllib.parse.urlparse(db_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {"connect_timeout": 5},
        }
    }
else:
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    }

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "tracker:project-list"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# Keep this explicit: adding a host to ALLOWED_HOSTS does not make it a
# trusted form origin. Strip harmless whitespace in host-managed env files,
# but never infer or wildcard additional origins.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
CSRF_FAILURE_VIEW = "tracker.csrf.failure"
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

ALLOW_PUBLIC_SIGNUP = env_bool("ALLOW_PUBLIC_SIGNUP", False)
# Exchanging an email/password for a bearer token. Off by default: the agent
# kit pairs through an approval code instead, so nothing needs to ask a person
# for their password. Set to 1 only to support a client predating the kit.
ALLOW_PASSWORD_TOKEN_EXCHANGE = env_bool("ALLOW_PASSWORD_TOKEN_EXCHANGE", False)
AGENT_TOKEN_DEFAULT_DAYS = int(os.environ.get("AGENT_TOKEN_DEFAULT_DAYS", "90"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
if not PUBLIC_BASE_URL and os.environ.get("APP_DOMAIN"):
    PUBLIC_BASE_URL = f"https://{os.environ['APP_DOMAIN']}"

# Keep local previews from ever sending real mail.  Production can use any
# ordinary SMTP provider (including Resend) without an application-specific
# integration.  Setting EMAIL_FILE_PATH selects Django's file backend, which
# is useful for a persistent local/staging mailbox.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND") or (
    "django.core.mail.backends.smtp.EmailBackend"
    if os.environ.get("EMAIL_HOST")
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
if os.environ.get("EMAIL_FILE_PATH"):
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = os.environ["EMAIL_FILE_PATH"]
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Homing <notifications@localhost>")
PASSWORD_RESET_TIMEOUT = int(os.environ.get("PASSWORD_RESET_TIMEOUT", "900"))
