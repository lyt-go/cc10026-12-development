"""数据归并领域服务：管理多API数据源的归并、冲突处理和去重策略。

跨模块数据流：
- 消费 api_registry 快照 -> 归并源API核验
- 消费 data_filter 快照 -> 各源数据归并前先过滤
- 被 export_task 消费 -> 归并后数据导出
- 归并结果写入共享快照
- 产出归并字段来源表 -> result_format 把输出别名连回实际输入
- 更正回执写入共享目录 -> 复核人按列归属追溯原字段和行
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataMergeService(BaseModuleService):
    MODULE = "data_merge"
    ENTITY = "归并规则"
    OPERATION_TERM = "去重"
    LIST_FIELDS = ["规则编号", "主API", "从API", "归并键", "冲突策略", "去重策略", "结果排序", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "主API", "从API"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # 复核人更正权限：可修改主源，不能改动从源
    REVIEWER_PERMISSIONS = {"primary": True, "secondary": False}

    def merge_sources(self, rule_id: int, source_a_rows: list[dict[str, Any]],
                      source_b_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按归并规则合并两个数据源。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None or rule.get("status") != "已生效":
            return source_a_rows + source_b_rows

        merge_key = rule.get("归并键", "id")
        conflict_policy = rule.get("冲突策略", "主源优先")
        dedup_policy = rule.get("去重策略", "全保留")

        merged = {}
        for row in source_a_rows:
            key = str(row.get(merge_key, ""))
            merged[f"a_{key}"] = {"_source": "primary", **row}

        for row in source_b_rows:
            key = str(row.get(merge_key, ""))
            pk = f"a_{key}"
            if pk in merged:
                if conflict_policy == "主源优先":
                    continue
                elif conflict_policy == "从源覆盖":
                    merged[pk] = {"_source": "secondary", **row}
                elif conflict_policy == "合并字段":
                    merged[pk].update({"from_secondary": row})
            else:
                merged[f"b_{key}"] = {"_source": "secondary", **row}

        result = list(merged.values())

        # 去重
        if dedup_policy == "按键去重":
            seen = set()
            deduped = []
            for row in result:
                key = str(row.get(merge_key, ""))
                if key not in seen:
                    seen.add(key)
                    deduped.append(row)
            result = deduped

        # 排序
        sort_field = rule.get("结果排序", "")
        if sort_field:
            result = sorted(result, key=lambda r: str(r.get(sort_field, "")))

        return result

    def merge_and_audit(self, rule_id: int, source_a_rows: list[dict[str, Any]],
                        source_b_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """归并两个数据源并写入审计记录。

        跨模块增强：归并完成后自动写入call_audit审计。
        """
        merged = self.merge_sources(rule_id, source_a_rows, source_b_rows)
        rule = store.find(self.MODULE, rule_id)

        # 联动增强：归并结果写入审计
        from app.services.data_manager import write_audit, DATA_ROOT
        audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": rule.get("主API", "") if rule else "",
            "caller": "data_merge",
            "event": "merge_completed",
            "primary_count": len(source_a_rows),
            "secondary_count": len(source_b_rows),
            "merged_count": len(merged),
            "rule_no": rule.get("规则编号", "") if rule else "",
        })
        return merged

    def merge_from_exports(self, rule_id: int) -> list[dict[str, Any]]:
        """从导出产物直接读取两个数据源并进行归并。

        跨模块增强：被 export_task 完成后调用，实现导出->归并->审计链路。
        """
        from app.services.data_manager import list_exports
        exports = list_exports()
        exports.sort(key=lambda x: x.get("_created_at", ""), reverse=True)
        source_a = exports[0] if len(exports) > 0 else {}
        source_b = exports[1] if len(exports) > 1 else {}

        rows_a = []
        rows_b = []
        cols_a = source_a.get("columns", [])
        cols_b = source_b.get("columns", [])
        for row_vals in source_a.get("rows", []):
            row = {col: val for col, val in zip(cols_a, row_vals)} if cols_a else {}
            rows_a.append(row)
        for row_vals in source_b.get("rows", []):
            row = {col: val for col, val in zip(cols_b, row_vals)} if cols_b else {}
            rows_b.append(row)

        return self.merge_and_audit(rule_id, rows_a, rows_b)

    # ========== 逐列字段来源（归并字段来源表） ==========

    def merge_with_field_lineage(self, rule_id: int, source_a_rows: list[dict[str, Any]],
                                 source_b_rows: list[dict[str, Any]], *,
                                 primary_file: str = "", secondary_file: str = "") -> dict[str, Any]:
        """归并两个数据源，并把来源精确到单列单格。

        merge_sources 只给整行标 _source，无法支撑单列修正；这里为每个
        输出字段、每个来源行格记录主从归属、原文件身份和原值，供
        build_field_source_table 生成归并字段来源表。
        """
        rule = store.find(self.MODULE, rule_id) or {}
        merge_key = rule.get("归并键") or "id"
        conflict_policy = rule.get("冲突策略") or "主源优先"
        primary_file = primary_file or rule.get("主API") or "主源"
        secondary_file = secondary_file or rule.get("从API") or "从源"

        merged_rows: dict[str, dict[str, Any]] = {}
        merged_cells: dict[str, dict[str, Any]] = {}
        merged_rowkey: dict[str, str] = {}
        field_roles: dict[str, set[str]] = {}

        def absorb(src_row: dict[str, Any], role: str, source_file: str,
                   key: str, cells: dict[str, Any], out_row: dict[str, Any]) -> None:
            for field, value in src_row.items():
                field_roles.setdefault(field, set()).add(role)
                candidate = {
                    "source_role": role,
                    "source_file": source_file,
                    "source_field": field,
                    "source_row_key": str(key),
                    "value": value,
                }
                if field not in cells:
                    cells[field] = {
                        "source_role": role,
                        "source_file": source_file,
                        "source_field": field,
                        "source_row_key": str(key),
                        "original_value": value,
                        "candidates": [candidate],
                        "conflict": False,
                    }
                    out_row[field] = value
                    continue
                # 主从同名字段：保留双方候选，生效值按冲突策略取舍
                cell = cells[field]
                cell["candidates"].append(candidate)
                cell["conflict"] = True
                if role == "secondary" and conflict_policy == "从源覆盖":
                    cell.update({
                        "source_role": role,
                        "source_file": source_file,
                        "source_row_key": str(key),
                        "original_value": value,
                    })
                    out_row[field] = value

        for a_row in source_a_rows:
            key = str(a_row.get(merge_key, ""))
            skey = f"a_{key}"
            merged_rows[skey], merged_cells[skey] = {}, {}
            merged_rowkey[skey] = key
            absorb(a_row, "primary", primary_file, key, merged_cells[skey], merged_rows[skey])

        for b_row in source_b_rows:
            key = str(b_row.get(merge_key, ""))
            skey = f"a_{key}" if f"a_{key}" in merged_rows else f"b_{key}"
            if skey not in merged_rows:
                merged_rows[skey], merged_cells[skey] = {}, {}
                merged_rowkey[skey] = key
            absorb(b_row, "secondary", secondary_file, key, merged_cells[skey], merged_rows[skey])

        rows = list(merged_rows.values())
        row_keys = [merged_rowkey[sk] for sk in merged_rows]
        cell_sources = {merged_rowkey[sk]: merged_cells[sk] for sk in merged_rows}

        sort_field = rule.get("结果排序", "")
        if sort_field:
            order = sorted(range(len(rows)), key=lambda i: str(rows[i].get(sort_field, "")))
            rows = [rows[i] for i in order]
            row_keys = [row_keys[i] for i in order]

        return {
            "ok": True,
            "rule_id": rule_id,
            "merge_key": merge_key,
            "conflict_policy": conflict_policy,
            "primary_file": primary_file,
            "secondary_file": secondary_file,
            "rows": rows,
            "row_keys": row_keys,
            "cell_sources": cell_sources,
            "field_sources": {
                field: {"roles": sorted(roles), "conflict": len(roles) > 1}
                for field, roles in field_roles.items()
            },
        }

    def build_field_source_table(self, merged: dict[str, Any], field_mapping: str) -> list[dict[str, Any]]:
        """归并字段来源表：输出别名 → 主/从源实际输入字段的逐列关联。

        模板字段映射把原字段改名成输出别名，主从两列可能撞同一个别名；
        每列记录全部候选来源，并按归并冲突策略标出生效来源，重名列的
        原文件身份在 candidates 里完整保留。结果写入共享目录供
        result_format 和更正复核消费。
        """
        pairs = []
        for pair in (field_mapping or "").split(","):
            if ":" in pair:
                orig, alias = pair.split(":", 1)
                pairs.append((orig.strip(), alias.strip()))

        file_of = {"primary": merged.get("primary_file", "主源"),
                   "secondary": merged.get("secondary_file", "从源")}
        field_sources = merged.get("field_sources", {})
        policy = merged.get("conflict_policy", "主源优先")

        columns: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for orig, alias in pairs:
            if alias not in columns:
                columns[alias] = {"alias": alias, "candidates": []}
                order.append(alias)
            for role in field_sources.get(orig, {}).get("roles", []):
                candidate = {"source_role": role, "source_file": file_of[role],
                             "source_field": orig}
                if candidate not in columns[alias]["candidates"]:
                    columns[alias]["candidates"].append(candidate)

        table = []
        for alias in order:
            column = columns[alias]
            candidates = column["candidates"]
            column["conflict"] = len(candidates) > 1
            resolved = None
            resolved_by = None
            if len(candidates) == 1:
                resolved, resolved_by = candidates[0], "唯一来源"
            elif policy == "主源优先":
                resolved = next((c for c in candidates if c["source_role"] == "primary"), None)
                resolved_by = "主源优先" if resolved else None
            elif policy == "从源覆盖":
                resolved = next((c for c in candidates if c["source_role"] == "secondary"), None)
                resolved_by = "从源覆盖" if resolved else None
            column["resolved_by"] = resolved_by
            column["source_role"] = resolved["source_role"] if resolved else None
            column["source_file"] = resolved["source_file"] if resolved else None
            column["source_field"] = resolved["source_field"] if resolved else None
            table.append(column)

        from app.services.data_manager import write_field_source_table
        write_field_source_table({
            "rule_id": merged.get("rule_id"),
            "field_mapping": field_mapping,
            "columns": table,
        })
        return table

    # ========== 逐列更正 workflow ==========

    def request_correction(self, field_source_table: list[dict[str, Any]],
                           merged: dict[str, Any], alias: str, row_key: str,
                           new_value: Any, *, source_role: str | None = None,
                           reviewer: str = "复核人") -> dict[str, Any]:
        """复核人按列归属发起更正申请。

        - 别名必须在归并字段来源表中，否则无从追溯；
        - 重名列(别名冲突)未指定归属时来源仍有歧义，不能直接触发原值更新；
        - 复核人只能更正主源列，从源列一律拒绝；
        - 申请保留原值和原文件身份，落到单个来源行和列。
        """
        column = next((c for c in field_source_table if c.get("alias") == alias), None)
        if column is None:
            return {"ok": False, "message": f"输出别名「{alias}」不在归并字段来源表中"}

        candidates = column.get("candidates", [])
        if source_role:
            target = next((c for c in candidates if c.get("source_role") == source_role), None)
            if target is None:
                return {"ok": False, "message": f"列「{alias}」没有归属为{source_role}的来源"}
        elif len(candidates) > 1:
            return {"ok": False,
                    "message": f"列「{alias}」来源仍有歧义（{len(candidates)}个候选），"
                               "需按列归属指定主源或从源后再发起更正"}
        elif candidates:
            target = candidates[0]
        else:
            return {"ok": False, "message": f"列「{alias}」没有可更正的来源"}

        if not self.REVIEWER_PERMISSIONS.get(target["source_role"], False):
            return {"ok": False, "message": "复核人无权限改动从源，仅能更正主源列"}

        row_key = str(row_key)
        cell = merged.get("cell_sources", {}).get(row_key, {}).get(target["source_field"])
        if cell is None:
            return {"ok": False,
                    "message": f"来源行 {row_key} 不存在字段 {target['source_field']}，更正无法落位"}
        candidate = next((c for c in cell.get("candidates", [])
                          if c["source_role"] == target["source_role"]), None)
        if candidate is None:
            return {"ok": False, "message": f"来源行 {row_key} 的该列归属与来源表不一致"}

        from app.services.data_manager import append_correction, list_corrections
        correction = {
            "correction_id": f"COR-{len(list_corrections()) + 1:04d}",
            "alias": alias,
            "row_key": row_key,
            "merge_key": merged.get("merge_key", "id"),
            "source_role": target["source_role"],
            "source_file": target["source_file"],
            "source_field": target["source_field"],
            "source_row_key": candidate["source_row_key"],
            "original_value": candidate["value"],
            "new_value": new_value,
            "reviewer": reviewer,
            "status": "待确认",
        }
        append_correction(correction)
        return {"ok": True, "correction": correction}

    def apply_correction(self, correction: dict[str, Any],
                         source_a_rows: list[dict[str, Any]],
                         source_b_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """确认更正：落到可验证的单个来源行和列，返回更正回执。

        执行前回读来源行校验原值未被改动，只更新目标行目标列一格；
        回执保留原值、原文件身份和落位坐标，并写入审计。
        """
        if correction.get("status") == "已更正":
            return {"ok": False, "message": "该更正已执行，请勿重复提交"}

        target_rows = source_a_rows if correction.get("source_role") == "primary" else source_b_rows
        merge_key = correction.get("merge_key", "id")
        source_row = next((r for r in target_rows
                           if str(r.get(merge_key, "")) == str(correction.get("source_row_key", ""))), None)
        if source_row is None:
            return {"ok": False,
                    "message": f"来源行 {correction.get('source_row_key')} 不存在，更正无法落位"}

        field = correction.get("source_field", "")
        current = source_row.get(field)
        if current != correction.get("original_value"):
            return {"ok": False,
                    "message": f"原值已变更（当前{current!r}），更正需重新核对后再发起"}

        source_row[field] = correction.get("new_value")
        receipt = {
            **correction,
            "status": "已更正",
            "verified_cell": {
                "source_file": correction.get("source_file"),
                "source_row_key": correction.get("source_row_key"),
                "source_field": field,
                "original_value": correction.get("original_value"),
                "new_value": correction.get("new_value"),
            },
        }

        from app.services.data_manager import DATA_ROOT, append_correction, write_audit
        append_correction(receipt)
        audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": correction.get("source_file", ""),
            "caller": "data_merge",
            "event": "correction_applied",
            "correction_id": correction.get("correction_id"),
            "alias": correction.get("alias"),
            "source_field": field,
            "source_row_key": correction.get("source_row_key"),
            "reviewer": correction.get("reviewer", ""),
        })
        return {"ok": True, "receipt": receipt}

    def verify_correction(self, receipt: dict[str, Any], formatted: dict[str, Any]) -> dict[str, Any]:
        """更正回执复核：重新查询格式化结果，确认表头别名关联的仍是该列。

        从表头别名一路核对到原字段和来源行：字段凭据的源文件/源字段要
        与回执一致，单元格凭据的来源行和取值要与回执的落位和新值一致。
        """
        alias = receipt.get("alias", "")
        row_key = str(receipt.get("row_key", ""))
        credential = (formatted.get("field_credentials") or {}).get(alias)
        if credential is None:
            return {"ok": False, "message": f"格式化结果中不存在别名「{alias}」的字段凭据"}
        cell = (formatted.get("cell_credentials") or {}).get(row_key, {}).get(alias)
        if cell is None:
            return {"ok": False, "message": f"行 {row_key} 的列「{alias}」没有单元格凭据"}

        chain_ok = (credential.get("source_file") == receipt.get("source_file")
                    and credential.get("source_field") == receipt.get("source_field")
                    and str(cell.get("source_row_key", "")) == str(receipt.get("source_row_key", "")))
        value_ok = cell.get("value") == receipt.get("new_value")
        ok = chain_ok and value_ok
        return {
            "ok": ok,
            "credential": credential,
            "cell": cell,
            "message": "表头到原字段和行的关联已确认" if ok else "关联链或取值与更正回执不一致",
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            from app.services.data_manager import read_api_snapshot
            registry = read_api_snapshot()
            primary_api = entry.get("主API", "")
            secondary_api = entry.get("从API", "")
            api_names = {v.get("api_name") for v in (registry.values() if isinstance(registry, dict) else []) if isinstance(v, dict)}
            if primary_api not in api_names and secondary_api not in api_names:
                return None, "至少一个源API尚未注册"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        merge_list = []
        for row in rows:
            merge_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "primary_api": row.get("主API"),
                "secondary_api": row.get("从API"),
                "merge_key": row.get("归并键"),
                "conflict_policy": row.get("冲突策略"),
                "dedup_policy": row.get("去重策略"),
                "sort": row.get("结果排序"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(merge_list),
            "merges": merge_list,
            "active_count": sum(1 for r in merge_list if r.get("status") == "已生效"),
        }