"""
配置管理模块
- 存储上游中转站地址（重启后保留）
- 使用 .env 文件持久化
"""
import json
import os
from pathlib import Path

# 配置文件路径（挂载到 volume 实现持久化）
DATA_DIR = Path(__file__).parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_UPSTREAM_URL = "https://api.deepseek.com/v1"


class AppConfig:
    """单例配置管理器"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._upstream_url = DEFAULT_UPSTREAM_URL
        self._load()

    def _load(self):
        """从文件加载配置"""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self._upstream_url = data.get("upstream_url", DEFAULT_UPSTREAM_URL)
            except (json.JSONDecodeError, OSError):
                self._upstream_url = DEFAULT_UPSTREAM_URL

    def _save(self):
        """保存配置到文件"""
        CONFIG_FILE.write_text(
            json.dumps({"upstream_url": self._upstream_url}, indent=2),
            encoding="utf-8",
        )

    @property
    def upstream_url(self) -> str:
        return self._upstream_url

    @upstream_url.setter
    def upstream_url(self, url: str):
        url = url.rstrip("/")
        self._upstream_url = url
        self._save()

    def as_dict(self) -> dict:
        return {"upstream_url": self._upstream_url}