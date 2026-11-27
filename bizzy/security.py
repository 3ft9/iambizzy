import base64
import binascii
import hashlib
import secrets

from cryptography.fernet import Fernet
from django.conf import settings
from django.http import HttpResponse


def cipher():
    key = hashlib.sha256(("calendar-credentials/" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value):
    return cipher().encrypt(value.encode()).decode()


def unseal(value):
    return cipher().decrypt(value.encode()).decode()


class BasicAuth:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Answered before the Host check and Basic Auth so an orchestrator can probe by IP.
        if request.path == "/healthz":
            return HttpResponse("ok")
        if settings.BASIC_AUTH_USER:
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            try:
                supplied = base64.b64decode(token, validate=True)
            except (ValueError, binascii.Error):
                supplied = b""
            expected = f"{settings.BASIC_AUTH_USER}:{settings.BASIC_AUTH_PASSWORD}".encode()
            if scheme.lower() != "basic" or not secrets.compare_digest(supplied, expected):
                return HttpResponse(
                    status=401, headers={"WWW-Authenticate": 'Basic realm="iAmBizzy"'}
                )
        return self.get_response(request)
