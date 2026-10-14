#!/usr/bin/env python3
"""Export the daily HK low-price candidate list for 同花顺远航版 custom groups.

Read cloud_run.py's data/latest.json and use the USER-VERIFIED blockstockV3.xml
as a template. Only replace <security> nodes under the exact custom group
(id=35, name=港股低价自选). Never touch the installed trading client or place orders.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

GROUP_ID = "35"
GROUP_NAME = "港股低价自选"
MARKET = "UHKM"
SESSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
CODE_RE = re.compile(r"\d{1,5}\Z")


class ExportError(ValueError):
    """Input is unsafe or incompatible with the verified template."""


def normalize_code(code: object) -> str:
    """Convert five-digit HKEX source code to verified HKxxxx form.

    '00753' -> 'HK0753', '00020' -> 'HK0020',
    '01810' -> 'HK1810', '09866' -> 'HK9866'.
    """
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        raise ExportError(f"无效港股代码：{code!r}，预期纯数字字符串（最多5位）")
    number = int(code)
    if not 1 <= number <= 99999:
        raise ExportError(f"港股代码超出有效范围：{code!r}")
    return "HK" + str(number).zfill(4)


def read_daily_candidates(path: Path) -> tuple[str, list[str]]:
    with path.open("r", encoding="utf-8-sig") as stream:
        report = json.load(stream)
    if not isinstance(report, dict):
        raise ExportError("latest.json 顶层必须是对象")
    meta = report.get("meta")
    candidates = report.get("ranked_candidates")
    if not isinstance(meta, dict) or not isinstance(candidates, list):
        raise ExportError("latest.json 缺少 meta 或 ranked_candidates")
    session = meta.get("target_session_hkt")
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise ExportError("未找到合法的目标港股交易日期")
    if meta.get("status") != "complete_date_coverage":
        raise ExportError("当日行情数据不完整，停止生成XML，不覆盖旧文件")
    if type(meta.get("candidate_count")) is not int or meta["candidate_count"] != len(candidates):
        raise ExportError("报告候选数量与实际排名数量不一致")
    if not candidates:
        raise ExportError("今日候选股票为空，为防止清空板块，拒绝导出")
    seen: set[str] = set()
    codes: list[str] = []
    for rank, row in enumerate(candidates, 1):
        if not isinstance(row, dict) or type(row.get("rank")) is not int or row["rank"] != rank:
            raise ExportError(f"候选榜单第{rank}行排名不连续")
        if row.get("data_status") != "fresh" or row.get("latest_date") != session:
            raise ExportError(f"候选榜单第{rank}行不是{session}的新鲜价格")
        code = normalize_code(row.get("code"))
        if code in seen:
            raise ExportError(f"重复股票代码：{code}")
        seen.add(code)
        codes.append(code)
    return session, codes


def render_group_xml(template: Path, codes: list[str]) -> bytes:
    if not codes or len(set(codes)) != len(codes):
        raise ExportError("不得导出空列表或重复代码")
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.parse(template, parser=parser)
    root = tree.getroot()
    if root.tag != "hevo":
        raise ExportError("模板不是已验证的 hevo 格式")
    blocks = [b for b in root.findall("Block") if b.get("id") == GROUP_ID]
    if len(blocks) != 1 or blocks[0].get("name") != GROUP_NAME:
        raise ExportError("找不到唯一的港股低价自选（id=35）分组，拒绝修改其他分组")
    block = blocks[0]
    original_children = list(block)
    first_index = next((i for i, c in enumerate(original_children) if c.tag == "security"), len(original_children))
    for child in original_children:
        if child.tag == "security":
            block.remove(child)
    for index, code in enumerate(codes):
        block.insert(first_index + index, ET.Element("security", {"market": MARKET, "code": code}))
    ET.indent(tree, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n"


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as f:
            f.write(content)
            temp_path = Path(f.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def run(root: Path, *, input_path: Path | None = None, template: Path | None = None,
        export_dir: Path | None = None, dry_run: bool = False) -> tuple[str, int, Path]:
    root = root.resolve()
    source = input_path or root / "data" / "latest.json"
    baseline = template or root / "config" / "ths_blockstock_template.xml"
    destination = export_dir or root / "data" / "ths"
    session, codes = read_daily_candidates(source)
    xml = render_group_xml(baseline, codes)
    output = destination / "blockstockV3.xml"
    dated = destination / "history" / f"{session}_blockstockV3.xml"
    if not dry_run:
        atomic_bytes(dated, xml)
        atomic_bytes(output, xml)
    return session, len(codes), output


def main() -> None:
    p = argparse.ArgumentParser(description="将cloud_run低位候选自动导出为同花顺远航版板块XML")
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parent, help="项目根目录（与cloud_run.py一致）")
    p.add_argument("--input", type=Path, help="候选JSON，默认 data/latest.json")
    p.add_argument("--template", type=Path, help="原生基准XML，默认 config/ths_blockstock_template.xml")
    p.add_argument("--export-dir", type=Path, help="导出目录，默认 data/ths")
    p.add_argument("--dry-run", action="store_true", help="只验证和统计，不写入文件")
    args = p.parse_args()
    try:
        session, count, path = run(args.output, input_path=args.input, template=args.template,
                                   export_dir=args.export_dir, dry_run=args.dry_run)
    except (ExportError, OSError, ET.ParseError, json.JSONDecodeError) as exc:
        p.exit(1, f"停止导出：{exc}\n")
    print(f"THS_EXPORT target={session} candidates={count} path={path} dry_run={args.dry_run}")


if __name__ == "__main__":
    main()
