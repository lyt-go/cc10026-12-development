"""归并字段来源表与逐列更正测试。

核心场景：主源的「税前额」和从源的「含税额」在模板中同名（别名「金额」），
核对格式化结果中的字段凭据和更正申请里实际引用的来源：

1. merge_with_field_lineage: 归并后逐列标注主从来源，精确到单格
2. build_field_source_table: 输出别名 -> 主/从源字段的逐列关联，重名列保留全部候选
3. format_with_lineage: 表头用模板别名，字段凭据把别名连回实际输入
4. request_correction: 歧义列/从源列拒绝，主源列保留原值和原文件身份
5. apply_correction + verify_correction: 落到单格、回执可再查确认关联列
"""

import os
import sys
import tempfile

# 设置测试环境
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 设置临时DATA_ROOT
temp_data = tempfile.mkdtemp(prefix='merge_lineage_test_')
os.environ['DATA_ROOT'] = temp_data


class TestMergeFieldLineage:
    """归并字段来源表与逐列更正"""

    def setup_method(self):
        """每个测试前初始化store和数据目录。"""
        from app.seed import SEED_ROWS
        from app.store import store as s

        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        import shutil
        if os.path.exists(temp_data):
            shutil.rmtree(temp_data, ignore_errors=True)

    # ---------- 测试夹具 ----------

    def _register_api(self, api_no: str, api_name: str):
        from app.services.api_registry import ApiRegistryService
        svc = ApiRegistryService()
        entry, missing = svc.create_entry({
            "API编号": api_no, "API名称": api_name, "接口路径": f"/api/{api_no}",
            "请求方法": "GET", "数据源": "orders_db", "返回格式": "json",
            "缓存策略": "default", "API版本": "v1",
        })
        assert entry is not None, f"API登记失败: {missing}"
        svc.run_action(entry["id"], "提交上线")
        entry, msg = svc.run_action(entry["id"], "确认上线")
        assert entry is not None, f"API上线失败: {msg}"

    def _make_rule(self):
        """归并规则：按id归并、主源优先、全保留。"""
        from app.services.data_merge import DataMergeService
        self._register_api("API-ORD-P", "订单主源API")
        self._register_api("API-ORD-S", "订单从源API")
        svc = DataMergeService()
        entry, missing = svc.create_entry({
            "规则编号": "MRG-TAX-001", "主API": "订单主源API", "从API": "订单从源API",
            "归并键": "id", "冲突策略": "主源优先", "去重策略": "全保留",
            "结果排序": "", "规则版本": "v1",
        })
        assert entry is not None, f"归并规则登记失败: {missing}"
        svc.run_action(entry["id"], "提交校验")
        _, msg = svc.run_action(entry["id"], "标记生效")
        assert svc.get_entry(entry["id"])["status"] == "已生效", f"规则生效失败: {msg}"
        return svc, entry["id"]

    def _make_template(self):
        """格式模板：主源税前额和从源含税额同名（别名「金额」）。"""
        from app.services.result_format import ResultFormatService
        svc = ResultFormatService()
        entry, missing = svc.create_entry({
            "模板编号": "FMT-TAX-001", "关联API": "订单归并API", "输出格式": "json",
            "字段映射": "id:单号,税前额:金额,含税额:金额,备注:备注",
            "嵌套展开": "否", "分页策略": "不分页", "排序字段": "", "模板版本": "v1",
        })
        assert entry is not None, f"模板登记失败: {missing}"
        svc.run_action(entry["id"], "提交校验")
        svc.run_action(entry["id"], "标记生效")
        assert svc.get_entry(entry["id"])["status"] == "已生效"
        return svc

    @staticmethod
    def _sources():
        source_a = [  # 主源：税前额
            {"id": "1", "税前额": 100, "备注": "主源单1"},
            {"id": "2", "税前额": 200, "备注": "主源单2"},
        ]
        source_b = [  # 从源：含税额
            {"id": "1", "含税额": 113},
            {"id": "2", "含税额": 226},
            {"id": "3", "含税额": 339},
        ]
        return source_a, source_b

    def _merged(self):
        svc, rule_id = self._make_rule()
        source_a, source_b = self._sources()
        merged = svc.merge_with_field_lineage(rule_id, source_a, source_b)
        return svc, rule_id, merged, source_a, source_b

    # ---------- 归并字段来源表 ----------

    def test_merge_marks_each_column_source(self):
        """归并后逐列标注主从来源，精确到单个来源行和列"""
        svc, rule_id, merged, source_a, source_b = self._merged()

        field_sources = merged["field_sources"]
        assert field_sources["税前额"] == {"roles": ["primary"], "conflict": False}
        assert field_sources["含税额"] == {"roles": ["secondary"], "conflict": False}
        # id 两源同名，列级标记冲突
        assert field_sources["id"]["conflict"] is True
        assert field_sources["id"]["roles"] == ["primary", "secondary"]

        # 合并字段后同一行同时带主从两列
        row1 = merged["rows"][merged["row_keys"].index("1")]
        assert row1["税前额"] == 100 and row1["含税额"] == 113

        # 单格来源：原文件身份 + 原字段 + 来源行 + 原值
        cell = merged["cell_sources"]["1"]["税前额"]
        assert cell["source_role"] == "primary"
        assert cell["source_file"] == "订单主源API"
        assert cell["source_row_key"] == "1"
        assert cell["original_value"] == 100
        cell_b = merged["cell_sources"]["1"]["含税额"]
        assert cell_b["source_file"] == "订单从源API"
        assert cell_b["original_value"] == 113

    def test_field_source_table_alias_conflict(self):
        """归并字段来源表：税前额/含税额同名「金额」，逐列关联全部候选"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额,备注:备注")

        by_alias = {c["alias"]: c for c in table}
        amount = by_alias["金额"]
        assert amount["conflict"] is True
        # 重名列的两个候选都保留原文件身份
        candidates = {(c["source_role"], c["source_file"], c["source_field"])
                      for c in amount["candidates"]}
        assert ("primary", "订单主源API", "税前额") in candidates
        assert ("secondary", "订单从源API", "含税额") in candidates
        # 生效来源按冲突策略解析
        assert amount["source_role"] == "primary"
        assert amount["source_field"] == "税前额"
        assert amount["resolved_by"] == "主源优先"
        # 无冲突列直接关联唯一来源
        assert by_alias["备注"]["conflict"] is False
        assert by_alias["备注"]["source_file"] == "订单主源API"

        # 来源表写入共享目录，供跨模块消费
        from app.services.data_manager import read_field_source_table
        persisted = read_field_source_table()
        assert any(c.get("alias") == "金额" for c in persisted.get("columns", []))

    def test_format_with_lineage_credentials(self):
        """格式化结果：表头用模板别名，字段凭据把别名连回实际输入"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        self._make_template()
        from app.services.result_format import ResultFormatService
        formatted = ResultFormatService().format_with_lineage("订单归并API", merged)

        assert formatted["headers"] == ["单号", "金额", "备注"]
        # 别名冲突时生效值取主源税前额
        row1 = next(r for r in formatted["rows"] if r["单号"] == "1")
        assert row1["金额"] == 100

        # 字段凭据：表头别名 -> 实际输入（主源文件+税前额），候选含从源含税额
        cred = formatted["field_credentials"]["金额"]
        assert cred["source_file"] == "订单主源API"
        assert cred["source_field"] == "税前额"
        assert cred["conflict"] is True
        assert any(c["source_file"] == "订单从源API" and c["source_field"] == "含税额"
                   for c in cred["candidates"])

        # 单元格凭据：从表头一路查到来源行
        cell = formatted["cell_credentials"]["1"]["金额"]
        assert cell["source_file"] == "订单主源API"
        assert cell["source_field"] == "税前额"
        assert cell["source_row_key"] == "1"
        assert cell["value"] == 100

    # ---------- 逐列更正 ----------

    def test_correction_ambiguous_alias_rejected(self):
        """来源仍有歧义的重名列不能直接触发原值更新"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额")

        result = svc.request_correction(table, merged, "金额", "1", 120)
        assert result["ok"] is False
        assert "歧义" in result["message"]
        # 两个来源都未被改动
        assert source_a[0]["税前额"] == 100
        assert source_b[0]["含税额"] == 113

    def test_correction_secondary_rejected(self):
        """复核人无权限改动从源列"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额")

        result = svc.request_correction(table, merged, "金额", "1", 120,
                                        source_role="secondary")
        assert result["ok"] is False
        assert "从源" in result["message"]
        assert source_b[0]["含税额"] == 113

    def test_correction_unknown_alias_rejected(self):
        """不在归并字段来源表中的别名无法发起更正"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额")

        result = svc.request_correction(table, merged, "不存在的列", "1", 120)
        assert result["ok"] is False
        assert "归并字段来源表" in result["message"]

    def test_correction_primary_full_flow(self):
        """主源列更正全链路：申请->落格->回执->再查确认关联列"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        self._make_template()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额,备注:备注")

        # 1. 复核人按列归属（主源）发起更正
        result = svc.request_correction(table, merged, "金额", "1", 120,
                                        source_role="primary", reviewer="复核人甲")
        assert result["ok"] is True, result.get("message")
        correction = result["correction"]
        # 申请实际引用主源列，重名列的原值和原文件身份都保留
        assert correction["source_file"] == "订单主源API"
        assert correction["source_field"] == "税前额"
        assert correction["source_row_key"] == "1"
        assert correction["original_value"] == 100
        assert correction["new_value"] == 120
        assert correction["status"] == "待确认"

        # 2. 确认更正：落到可验证的单个来源行和列
        applied = svc.apply_correction(correction, source_a, source_b)
        assert applied["ok"] is True, applied.get("message")
        receipt = applied["receipt"]
        assert receipt["status"] == "已更正"
        assert source_a[0]["税前额"] == 120      # 只改主源行1的税前额
        assert source_a[1]["税前额"] == 200      # 其余主源行不动
        assert source_b[0]["含税额"] == 113      # 从源不动
        # 回执保留原值和原文件身份
        assert receipt["verified_cell"]["original_value"] == 100
        assert receipt["verified_cell"]["source_file"] == "订单主源API"

        # 3. 收到回执后再次查询结果，确认关联的是哪列
        merged2 = svc.merge_with_field_lineage(rule_id, source_a, source_b)
        from app.services.result_format import ResultFormatService
        formatted2 = ResultFormatService().format_with_lineage("订单归并API", merged2)
        check = svc.verify_correction(receipt, formatted2)
        assert check["ok"] is True, check.get("message")
        assert check["credential"]["source_field"] == "税前额"
        assert check["cell"]["value"] == 120

        # 4. 更正记录持久化，可追溯
        from app.services.data_manager import list_corrections
        records = [r for r in list_corrections()
                   if r.get("correction_id") == correction["correction_id"]]
        assert len(records) == 1
        assert records[0]["status"] == "已更正"
        assert records[0]["original_value"] == 100

    def test_correction_single_source_column_needs_no_disambiguation(self):
        """唯一来源列无需指定归属即可更正"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额,备注:备注")

        result = svc.request_correction(table, merged, "备注", "2", "已复核")
        assert result["ok"] is True, result.get("message")
        correction = result["correction"]
        assert correction["source_role"] == "primary"
        assert correction["source_field"] == "备注"

        applied = svc.apply_correction(correction, source_a, source_b)
        assert applied["ok"] is True
        assert source_a[1]["备注"] == "已复核"

    def test_correction_stale_original_value_rejected(self):
        """落格前回读校验：原值已变更则拒绝执行"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额")

        result = svc.request_correction(table, merged, "金额", "1", 120,
                                        source_role="primary")
        assert result["ok"] is True
        # 申请后来源被他人改动
        source_a[0]["税前额"] = 999
        applied = svc.apply_correction(result["correction"], source_a, source_b)
        assert applied["ok"] is False
        assert "原值已变更" in applied["message"]
        assert source_a[0]["税前额"] == 999

    def test_correction_repeat_apply_rejected(self):
        """同一更正不能重复落格"""
        svc, rule_id, merged, source_a, source_b = self._merged()
        table = svc.build_field_source_table(merged, "税前额:金额,含税额:金额")

        result = svc.request_correction(table, merged, "金额", "1", 120,
                                        source_role="primary")
        applied = svc.apply_correction(result["correction"], source_a, source_b)
        assert applied["ok"] is True
        again = svc.apply_correction(applied["receipt"], source_a, source_b)
        assert again["ok"] is False
        assert "重复" in again["message"]
