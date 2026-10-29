import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

OFFICIAL_URL = "https://api.deepseek.com"
DEFAULT_CONFIG = Path("/home/dsh/.config/myself-agent/deepseek.env")
DEFAULT_IMAGE = "python@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60"


@dataclass(frozen=True)
class Settings:
    api_key: SecretStr = field(repr=False)
    model: str = "deepseek-flash"
    base_url: str = OFFICIAL_URL
    proxy: str = "http://127.0.0.1:7890"
    image: str = DEFAULT_IMAGE
    run_root: Path = Path("/data/myself-agent/runs")
    max_repairs: int = 3
    timeout_seconds: int = 20


def load_settings(path: Path = DEFAULT_CONFIG, **overrides) -> Settings:
    if path.is_symlink():
        raise ValueError("Credential file must not be a symlink")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError("Credential file must be owned by the current user with mode 600")
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    if values.get("DEEPSEEK_BASE_URL") != OFFICIAL_URL:
        raise ValueError("Only the configured DeepSeek official endpoint is allowed")
    if values.get("DEEPSEEK_MODEL") != "deepseek-flash":
        raise ValueError("Expected the user-selected deepseek-flash model")
    if not values.get("DEEPSEEK_API_KEY"):
        raise ValueError("Missing model credential")
    return Settings(
        api_key=SecretStr(values["DEEPSEEK_API_KEY"]),
        base_url=OFFICIAL_URL,
        model=values["DEEPSEEK_MODEL"],
        **overrides,
    )
