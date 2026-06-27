"""物流映射 — YAML 配置加载 + 查表"""

from pathlib import Path
from typing import Optional

import yaml


class LogisticResolver:
    """物流公司映射解析器"""

    def __init__(self, yaml_path: Optional[str] = None):
        if yaml_path is None:
            yaml_path = str(
                Path(__file__).parent.parent.parent / "config" / "logistic.yaml"
            )
        with open(yaml_path) as f:
            self._config = yaml.safe_load(f) or {}
        self._mapping = self._config.get("logistic_mapping", {})
        self._fallback = self._config.get("fallback", {})

    def resolve(self, jky_logistic_code: str) -> dict:
        """吉客云物流编码 → {platform_code, platform_name}"""
        entry = self._mapping.get(jky_logistic_code)
        if entry:
            return entry
        return self._fallback

    @property
    def all_codes(self) -> list[str]:
        return list(self._mapping.keys())
