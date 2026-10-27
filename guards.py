import re
from urllib.parse import urlparse

from config import ALLOWED_TARGET_HOSTS, CATEGORIES, TARGET_URL

MAX_PER_CATEGORY = 3
MAX_ROUNDS_ALLOWED = 3

# ---------- Guard 1: only attack hosts you allowed ----------
def _allowed_hosts():
    hosts = set(ALLOWED_TARGET_HOSTS)
    default_host = urlparse(TARGET_URL).hostname
    if default_host:
        hosts.add(default_host.lower())
    return hosts


def check_run_request(target_url, per_category, rounds, categories):
    """Validate what the web page asked for. Returns clean values or raises ValueError."""
    url = (target_url or TARGET_URL).strip()
    parts = urlparse(url)

    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The target URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("Do not put a username or password inside the URL.")
    if parts.hostname.lower() not in _allowed_hosts():
        raise ValueError(
            f"The host '{parts.hostname}' is not on the allow-list. "
            "Add it to ALLOWED_TARGET_HOSTS in your .env file."
        )

    per_category = max(1, min(MAX_PER_CATEGORY, int(per_category)))
    rounds = max(1, min(MAX_ROUNDS_ALLOWED, int(rounds)))

    categories = categories or list(CATEGORIES)
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise ValueError(f"Unknown categories: {unknown}")

    return url, per_category, rounds, categories


# ---------- Guard 2: hide private data before it reaches the browser ----------
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
PHONE = re.compile(r"\+?\d(?:[ -]?\d){9,13}")


def redact_pii(text):
    text = str(text or "")
    text = EMAIL.sub("[email]", text)
    text = CARD.sub("[card]", text)
    text = PHONE.sub("[phone]", text)
    return text