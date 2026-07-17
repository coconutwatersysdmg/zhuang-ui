"""WCS 库存表 ``wcs_stock_box`` 的读写（MySQL / pymysql）。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymysql
from pymysql.cursors import DictCursor


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "zhuangdb"
    charset: str = "utf8mb4"


def load_database_config(raw: Optional[Dict] = None) -> DatabaseConfig:
    raw = raw or {}
    return DatabaseConfig(
        host=str(raw.get("host") or "localhost"),
        port=int(raw.get("port") or 3306),
        user=str(raw.get("user") or "root"),
        password=str(raw.get("password") or ""),
        database=str(raw.get("database") or "zhuangdb"),
        charset=str(raw.get("charset") or "utf8mb4"),
    )


def format_box_spec(length, width, height, box_type, weight=None) -> str:
    """存库格式：(length,width,height,box_type[,weight])。"""
    base = f"({float(length)},{float(width)},{float(height)},{box_type}"
    if weight is None:
        return base + ")"
    return base + f",{float(weight)})"


def parse_box_spec(spec: str) -> Dict:
    """解析 box_spec → length/width/height/box_type/weight。"""
    text = (spec or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 4:
        raise ValueError(f"box_spec 格式无效: {spec!r}")
    weight = float(parts[4]) if len(parts) >= 5 else 0.0
    return {
        "length": float(parts[0]),
        "width": float(parts[1]),
        "height": float(parts[2]),
        "box_type": str(parts[3]),
        "weight": weight,
    }


class WcsStockRepository:
    """``zhuangdb.wcs_stock_box`` 仓储。"""

    def __init__(self, config: DatabaseConfig):
        self._cfg = config

    def _connect(self):
        return pymysql.connect(
            host=self._cfg.host,
            port=self._cfg.port,
            user=self._cfg.user,
            password=self._cfg.password,
            database=self._cfg.database,
            charset=self._cfg.charset,
            cursorclass=DictCursor,
            autocommit=False,
        )

    @contextmanager
    def _cursor(self):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                yield conn, cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_new_stock_entries(self, entries: Sequence[Dict]) -> int:
        """按 product_code 去重插入；已存在则跳过。返回新插入行数。

        新行 ``up_to_standard='0'``（未达标）。
        """
        prepared: List[Tuple] = []
        seen_in_batch: set = set()
        for entry in entries:
            pc_raw = entry.get("product_code")
            if pc_raw is None or pc_raw == "":
                continue
            try:
                pc = int(pc_raw)
            except (TypeError, ValueError):
                continue
            if pc in seen_in_batch:
                continue
            seen_in_batch.add(pc)
            prepared.append((
                format_box_spec(
                    entry.get("length") or 0,
                    entry.get("width") or 0,
                    entry.get("height") or 0,
                    entry.get("box_type") or "",
                    entry.get("weight"),
                ),
                str(entry.get("case_type") or ""),
                int(entry.get("target_num") or 1),
                str(entry.get("order_id") or ""),
                str(
                    entry.get("case_group")
                    if entry.get("case_group") is not None
                    else "0"
                ),
                pc,
                int(entry.get("priority") or 0),
                "0",
            ))

        if not prepared:
            return 0

        codes = [row[5] for row in prepared]
        existing = self._existing_product_codes(codes)
        to_insert = [row for row in prepared if row[5] not in existing]
        if not to_insert:
            return 0

        sql = (
            "INSERT IGNORE INTO wcs_stock_box "
            "(box_spec, case_type, target_num, order_id, case_group, "
            "product_code, priority, up_to_standard) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        with self._cursor() as (_conn, cur):
            cur.executemany(sql, to_insert)

        after = self._existing_product_codes(codes)
        return max(0, len(after) - len(existing))

    def _existing_product_codes(self, codes: Sequence[int]) -> set:
        if not codes:
            return set()
        uniq = list({int(c) for c in codes})
        found: set = set()
        chunk = 500
        with self._cursor() as (_conn, cur):
            for i in range(0, len(uniq), chunk):
                part = uniq[i:i + chunk]
                placeholders = ",".join(["%s"] * len(part))
                cur.execute(
                    f"SELECT product_code FROM wcs_stock_box "
                    f"WHERE product_code IN ({placeholders})",
                    part,
                )
                for row in cur.fetchall():
                    found.add(int(row["product_code"]))
        return found

    def fetch_unmet_rows(self) -> List[Dict]:
        """读取当前所有未达标行（up_to_standard='0'）。"""
        with self._cursor() as (_conn, cur):
            cur.execute(
                "SELECT id, box_spec, case_type, target_num, order_id, "
                "case_group, product_code, priority, up_to_standard "
                "FROM wcs_stock_box WHERE up_to_standard = '0' "
                "ORDER BY id ASC"
            )
            return list(cur.fetchall())

    def mark_standard_by_product_codes(self, product_codes: Iterable) -> int:
        """将达标箱子的 up_to_standard 更新为 '1'。返回影响行数。"""
        codes = []
        for pc in product_codes:
            if pc is None or pc == "":
                continue
            try:
                codes.append(int(pc))
            except (TypeError, ValueError):
                continue
        codes = list({c for c in codes})
        if not codes:
            return 0
        updated = 0
        chunk = 500
        with self._cursor() as (_conn, cur):
            for i in range(0, len(codes), chunk):
                part = codes[i:i + chunk]
                placeholders = ",".join(["%s"] * len(part))
                cur.execute(
                    f"UPDATE wcs_stock_box SET up_to_standard = '1' "
                    f"WHERE product_code IN ({placeholders}) "
                    f"AND up_to_standard = '0'",
                    part,
                )
                updated += int(cur.rowcount or 0)
        return updated

    def rows_to_stock_entries(self, rows: Sequence[Dict]) -> List[Dict]:
        """DB 行 → 接口库存条目结构（供 stock_to_boxes）。"""
        entries: List[Dict] = []
        for row in rows:
            try:
                dims = parse_box_spec(row.get("box_spec") or "")
            except ValueError:
                continue
            entries.append({
                "length": dims["length"],
                "width": dims["width"],
                "height": dims["height"],
                "weight": dims["weight"],
                "box_type": dims["box_type"],
                "case_type": row.get("case_type") or "",
                "target_num": int(row.get("target_num") or 1),
                "order_id": row.get("order_id") or "",
                "case_group": row.get("case_group") or "0",
                "product_code": row.get("product_code"),
                "priority": row.get("priority") or 0,
            })
        return entries
