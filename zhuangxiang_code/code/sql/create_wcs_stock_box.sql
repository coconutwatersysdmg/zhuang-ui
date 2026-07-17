-- 接口库存箱子表（对应接口1 JSON 的 data[] 条目）
-- 库名：zhuangdb（请先 USE zhuangdb; 再执行，或直接整文件执行）
-- 引擎：MySQL 5.7+ / MariaDB
--
-- 路径：zhuangxiang_code/code/sql/create_wcs_stock_box.sql
-- 若表已存在，本脚本会 DROP 再建（旧数据清空）。

USE zhuangdb;

DROP TABLE IF EXISTS `wcs_stock_box`;

CREATE TABLE `wcs_stock_box` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  `box_spec` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '箱子规格 (length,width,height,box_type[,weight])',
  `case_type` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT '' COMMENT '托盘型号 case_type',
  `target_num` int NULL DEFAULT 1 COMMENT '数量 target_num',
  `order_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT '' COMMENT '订单号 order_id',
  `case_group` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT '0' COMMENT '拼箱组号 case_group',
  `product_code` bigint NOT NULL COMMENT '产品编码 product_code（一箱一码，唯一）',
  `priority` int NULL DEFAULT 0 COMMENT '优先级 priority',
  `up_to_standard` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT '0' COMMENT '1达标 0未达标',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uk_product_code` (`product_code`) USING BTREE,
  KEY `idx_order_id` (`order_id`) USING BTREE,
  KEY `idx_case_type` (`case_type`) USING BTREE,
  KEY `idx_up_to_standard` (`up_to_standard`) USING BTREE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='WCS接口库存箱子（由接口1 JSON 落库）'
  ROW_FORMAT=Dynamic;

-- ---------------------------------------------------------------------------
-- 插入示例：
-- INSERT INTO wcs_stock_box
--   (box_spec, case_type, target_num, order_id, case_group, product_code, priority, up_to_standard)
-- VALUES
--   ('(350,530,240,YZX424,3.25)', 'MH423C', 1, 'PAIN26290MZ07S', '0', 30061250, 0, '0');
-- ---------------------------------------------------------------------------
