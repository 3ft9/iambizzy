import logging
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def persistent_key():
    if key := os.getenv("APP_SECRET"):
        return key
    path = DATA_DIR / "secret"
    if not path.exists():
        logging.getLogger(__name__).warning(
            "Generating a new encryption key at %s. Saved calendar credentials are unreadable "
            "unless this file persists. Set APP_SECRET when the data directory is not durable.",
            path,
        )
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=False) as temp:
            temp.write(secrets.token_urlsafe(48).encode())
        try:
            os.link(temp.name, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(temp.name)
    return path.read_text().strip()


SECRET_KEY = persistent_key()
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
origin = urlsplit(PUBLIC_URL)
if origin.scheme not in {"https", "http"} or not origin.hostname or origin.path:
    raise ImproperlyConfigured("PUBLIC_URL must be an absolute HTTP(S) origin")
ALLOWED_HOSTS = [origin.hostname, "localhost", "127.0.0.1", "[::1]"]
CSRF_TRUSTED_ORIGINS = [f"{origin.scheme}://{origin.netloc.lower()}"]  # browsers send lowercase
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = origin.scheme == "https"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "")
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "")
if bool(BASIC_AUTH_USER) != bool(BASIC_AUTH_PASSWORD):
    raise ImproperlyConfigured("Set both Basic Auth settings together")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "3600"))
WINDOW_MONTHS = int(os.getenv("WINDOW_MONTHS", "3"))
if min(POLL_SECONDS, WINDOW_MONTHS) < 1:
    raise ImproperlyConfigured("POLL_SECONDS and WINDOW_MONTHS must be positive")

DATABASES = {"default": dj_database_url.config(default=f"sqlite:///{DATA_DIR}/db.sqlite3")}
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    DATABASES["default"]["OPTIONS"] = {"timeout": 20}
STATIC_URL = "/static/"
WHITENOISE_USE_FINDERS = True
INSTALLED_APPS = [
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.messages",
    "bizzy",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "bizzy.security.BasicAuth",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.contrib.messages.context_processors.messages"]},
    }
]
ROOT_URLCONF = "bizzy.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
