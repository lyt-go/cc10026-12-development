"""结果格式化领域服务：管理查询结果的输出格式、字段映射和分页配置。

跨模块数据流：
- 消费 api_registry 快照 → 关联API自动选择格式模板
- 消费 data_filter 快照 → 过滤后数据按模板格式化
- 消费 field_mask 快照 → 脱敏后数据按模板输出
- 消费 data_merge 归并字段来源表 → 输出别名连回实际输入
- 被 export_task 消费 → 格式化结果写入导出文件
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ResultFormatService(BaseModuleService):
    MODULE = "result_format"
    ENTITY = "格式模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板编号", "关联API", "输出格式", "字段映射", "嵌套展开", "分页策略", "排序字段", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "关联API", "输出格式"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    def format_results(self, api_name: str, data_rows: list[dict[str, Any]],
                       format_type: str = "json") -> Any:
        """对查询结果按模板格式化。"""
        rows = store.rows(self.MODULE)
        template = None
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                template = row
                break

        if not template:
            return data_rows

        output_format = template.get("输出格式", format_type)
        field_mapping = template.get("字段映射", "")
        sort_field = template.get("排序字段", "")

        # 字段映射
        if field_mapping:
            mapped = []
            for row in data_rows:
                mapped_row = {}
                for orig, new in [pair.split(":") for pair in field_mapping.split(",") if ":" in pair]:
                    mapped_row[new.strip()] = row.get(orig.strip(), "")
                mapped.append(mapped_row if mapped_row else row)
            data_rows = mapped

        # 排序
        if sort_field and sort_field in (data_rows[0] if data_rows else {}):
            data_rows = sorted(data_rows, key=lambda r: str(r.get(sort_field, "")))

        if output_format == "csv":
            return self._to_csv(data_rows)
        elif output_format == "nested":
            return {"nested": True, "total": len(data_rows), "items": data_rows}
        return data_rows

    def format_with_lineage(self, api_name: str, merged: dict[str, Any]) -> dict[str, Any]:
        """按模板格式化归并结果，并用归并字段来源表把输出别名连回实际输入。

        表头使用模板别名；每列输出字段凭据（生效来源+全部候选，含原文件
        身份），每行每列输出单元格凭据（来源行键和取值），确认后的更正
        操作能从表头一路查到要改的原字段和行。
        """
        template = None
        for row in store.rows(self.MODULE):
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                template = row
                break

        field_mapping = template.get("字段映射", "") if template else ""
        if not field_mapping:
            # 无模板时按原字段名直出，来源表同样逐列生成
            fields: list[str] = []
            for merged_row in merged.get("rows", []):
                for field in merged_row:
                    if field not in fields:
                        fields.append(field)
            field_mapping = ",".join(f"{field}:{field}" for field in fields)

        from app.services import service_for
        merge_svc = service_for("data_merge")
        table = merge_svc.build_field_source_table(merged, field_mapping)
        columns = {column["alias"]: column for column in table}
        headers = [column["alias"] for column in table]

        rows_out = []
        cell_credentials: dict[str, dict[str, Any]] = {}
        cell_sources = merged.get("cell_sources", {})
        for merged_row, row_key in zip(merged.get("rows", []), merged.get("row_keys", [])):
            out_row = {}
            row_credentials = {}
            row_cells = cell_sources.get(str(row_key), {})
            for alias in headers:
                column = columns[alias]
                source_field = column.get("source_field")
                value = merged_row.get(source_field, "") if source_field else ""
                out_row[alias] = value
                cell = row_cells.get(source_field, {}) if source_field else {}
                row_credentials[alias] = {
                    "source_file": column.get("source_file"),
                    "source_field": source_field,
                    "source_row_key": cell.get("source_row_key", str(row_key)),
                    "value": value,
                }
            rows_out.append(out_row)
            cell_credentials[str(row_key)] = row_credentials

        return {
            "api_name": api_name,
            "template_no": template.get("模板编号") if template else None,
            "headers": headers,
            "rows": rows_out,
            "field_source_table": table,
            "field_credentials": columns,
            "cell_credentials": cell_credentials,
        }

    def _to_csv(self, data_rows: list[dict[str, Any]]) -> str:
        if not data_rows:
            return ""
        keys = list(data_rows[0].keys())
        lines = [",".join(keys)]
        for row in data_rows:
            lines.append(",".join(str(row.get(k, "")) for k in keys))
        return "\n".join(lines)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        format_list = []
        for row in rows:
            format_list.append({
                "id": row.get("id"),
                "template_no": row.get("模板编号"),
                "api": row.get("关联API"),
                "output_format": row.get("输出格式"),
                "field_mapping": row.get("字段映射"),
                "nesting": row.get("嵌套展开"),
                "pagination": row.get("分页策略"),
                "sort": row.get("排序字段"),
                "version": row.get("模板版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(format_list),
            "templates": format_list,
            "active_count": sum(1 for r in format_list if r.get("status") == "已生效"),
        }