"""SKU 映射查询 + 缺失告警"""

from typing import Optional, Tuple

from ..storage.db import get_connection


class SkuResolver:
    """SKU 映射解析器"""

    def resolve(self, platform_sku_no: str) -> Optional[str]:
        """查 sku_mapping 表，返回 jky_goods_no；None = 缺映射"""
        conn = get_connection()
        row = conn.execute(
            "SELECT jky_goods_no FROM sku_mapping WHERE platform_sku_no = ?",
            (platform_sku_no,),
        ).fetchone()
        if row:
            return row["jky_goods_no"]
        return None

    def resolve_by_barcode(self, barcode: str) -> Optional[Tuple[str, str]]:
        """通过条码反查，返回 (platform_sku_no, jky_goods_no) or None"""
        conn = get_connection()
        row = conn.execute(
            "SELECT platform_sku_no, jky_goods_no FROM sku_mapping WHERE platform_barcode = ?",
            (barcode,),
        ).fetchone()
        if row:
            return (row["platform_sku_no"], row["jky_goods_no"])
        return None

    def is_in_jky_product_cache(self, barcode: str) -> bool:
        """检查 barcode 是否已在 jky_product_cache 中（区分缺映射类型）"""
        conn = get_connection()
        row = conn.execute(
            "SELECT 1 FROM jky_product_cache WHERE jky_barcode = ?",
            (barcode,),
        ).fetchone()
        return row is not None

    def get_missing_type(self, platform_sku_no: str, barcode: Optional[str] = None) -> str:
        """判断 SKU 缺映射原因

        Returns:
            "jky_not_found" — 吉客云商品库没有此 SKU
            "mapping_missing" — 吉客云有但 sku_mapping 表未关联
        """
        if barcode and self.is_in_jky_product_cache(barcode):
            return "mapping_missing"
        return "jky_not_found"
