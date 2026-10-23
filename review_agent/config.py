"""Application configuration; secrets stay out of reports and logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MAX_SOURCE_BYTES = 48_000
MAX_SOURCE_LINES = 800
MAX_FOCUS_CHARS = 1_000


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout: float = 45
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        try:
            timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "45"))
        except ValueError:
            raise ValueError("DEEPSEEK_TIMEOUT 必须是数字（秒）。") from None
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", cls.base_url).strip().rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", cls.model).strip(),
            timeout=timeout,
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("请在侧栏或 .env 中设置 DEEPSEEK_API_KEY，或切换为离线演示。")
        url = urlparse(self.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("API 地址必须是有效的 HTTPS 基础地址，不含账号、查询或片段。")
        if not self.model or not 5 <= self.timeout <= 120 or not 0 <= self.max_retries <= 3:
            raise ValueError("请检查模型名、超时（5～120 秒）及重试次数（0～3）。")
