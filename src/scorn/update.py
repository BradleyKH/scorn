import json
import re
import urllib.request

import scorn

LATEST_URL = "https://api.github.com/repos/BradleyKH/scorn/releases/latest"


def latest_release(timeout: float = 3.0) -> dict | None:
    """The newest GitHub release as {"version", "url"}, or None for any failure, including no release yet."""
    request = urllib.request.Request(
        LATEST_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"scorn/{scorn.__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
        return {"version": str(data["tag_name"]).removeprefix("v"), "url": str(data["html_url"])}
    except Exception:
        return None


def _parse(version: str) -> tuple[int, ...] | None:
    version = version.strip().removeprefix("v")
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(part) for part in version.split("."))


def is_newer(latest: str, current: str) -> bool:
    """True only when both versions are plain dotted numbers and latest is higher; dev and pre-release builds never nag."""
    a, b = _parse(latest), _parse(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))
