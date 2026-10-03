"""Fail-closed validation of managed dashboard authority for ledger browsers."""

import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit


STATUS_PATH = "/api/fab/operator-session/status"
CLOCK_SKEW_SECONDS = 5


def _read_status(url, credential, body):
    destination = urlsplit(url)
    connection_class = http.client.HTTPSConnection if destination.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(destination.hostname, destination.port, timeout=1)
    deadline = time.monotonic() + 3
    timer = None
    try:
        # Direct connection: no environment proxies, .netrc or redirect handling.
        connection.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        transport = connection.sock

        def interrupt_response():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Inactivity timeouts alone do not stop slowly streamed headers or bodies.
        timer = threading.Timer(remaining, interrupt_response)
        timer.daemon = True
        timer.start()
        transport.settimeout(min(2, remaining))
        connection.request("POST", destination.path, body=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {credential}", "Content-Type": "application/json",
            "Accept-Encoding": "identity", "Connection": "close",
        })
        with connection.getresponse() as response:
            if response.status != 200 or response.getheader("Content-Encoding", "identity").lower() != "identity":
                return None
            content = response.read(4_097)
            if len(content) > 4_096 or time.monotonic() >= deadline:
                return None
            return json.loads(content)
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        connection.close()


def parent_validation_url(config):
    return config.get("fab_operator_session_validation_url") or config.get("operations_operator_session_validation_url") or ""


def requires_parent_session(config):
    return ((config.get("fab_deployment_profile") or config.get("operations_deployment_profile")) == "vm" or bool(parent_validation_url(config))
            or bool(config.get("fab_operator_access_token") or config.get("fab_operator_access_token_file")))


def valid_validation_url(value, profile="local"):
    try:
        if not isinstance(value, str) or not value or re.search(r"[\s\\]", value):
            return False
        url = urlsplit(value)
        return bool(url.hostname and not url.username and not url.password
                    and url.path == STATUS_PATH and not url.query and not url.fragment
                    and url.port != 0 and (url.scheme == "https" or (url.scheme == "http"
                    and (url.hostname in {"127.0.0.1", "localhost", "::1"}
                         or (profile == "vm" and url.hostname == "web")))))
    except ValueError:
        return False


def parent_session_active(parent, config, token, *, nonce=None, ticket_expiry=None):
    now = int(time.time())
    if (not token or not isinstance(parent, dict)
            or not isinstance(parent.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32}", parent["id"])
            or type(parent.get("exp")) is not int
            or not now < parent["exp"] <= now + 900 + CLOCK_SKEW_SECONDS):
        return False
    url = parent_validation_url(config)
    profile = config.get("fab_deployment_profile") or config.get("operations_deployment_profile") or "local"
    if not valid_validation_url(url, profile):
        return False
    credential = base64.urlsafe_b64encode(hmac.new(
        token.encode(), b"fab-managed-parent-status:v1", hashlib.sha256,
    ).digest()).decode().rstrip("=")
    body = {"id": parent["id"]}
    if nonce is not None:
        if (not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", nonce)
                or type(ticket_expiry) is not int or not now < ticket_expiry <= min(now + 45 + CLOCK_SKEW_SECONDS, parent["exp"])):
            return False
        body.update(nonce=nonce, exp=ticket_expiry)
    try:
        status = _read_status(url, credential, body)
        return (isinstance(status, dict) and status.get("active") is True
                and type(status.get("exp")) is int and status["exp"] == parent["exp"]
                and int(time.time()) < status["exp"]
                and (nonce is None or int(time.time()) < ticket_expiry))
    except (http.client.HTTPException, OSError, ValueError):
        return False
