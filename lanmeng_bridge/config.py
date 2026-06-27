"""配置加载 — 从 settings.yaml + credentials.yaml 合并"""

import os
from pathlib import Path

import yaml

# 默认路径
SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
CREDENTIALS_PATH = Path.home() / ".hermes" / "data" / "credentials.yaml"


def load_credentials() -> dict:
    """从 credentials.yaml 读取凭据（脱敏字段以 _credentials 注入 settings）"""
    path = os.environ.get("CREDENTIALS_PATH", str(CREDENTIALS_PATH))
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    """加载 settings.yaml，合并凭据"""
    path = os.environ.get("SETTINGS_PATH", str(SETTINGS_PATH))
    with open(path) as f:
        settings = yaml.safe_load(f) or {}

    # 注入凭据
    creds = load_credentials()
    settings["_credentials"] = creds

    # 展开 ~/ 路径
    db_path = settings.get("db", {}).get("path", "")
    if db_path.startswith("~/"):
        settings["db"]["path"] = str(Path.home() / db_path[2:])

    return settings
