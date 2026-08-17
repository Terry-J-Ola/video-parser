"""Create the batch processing statistics workbook without external XLSX dependencies."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


class BatchRecord(Protocol):
    """Fields required from a batch video processing record."""

    name: str
    status: str
    total_tokens: int
    elapsed_seconds: float


_STATUS_LABELS = {
    "complete": "完成",
    "partial": "部分完成",
    "skipped": "已跳过",
    "failed": "失败",
}


def _inline_cell(reference: str, value: str, style: int) -> str:
    text = escape(value)
    preserve = ' xml:space="preserve"' if value != value.strip() else ""
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr">'
        f"<is><t{preserve}>{text}</t></is></c>"
    )


def _number_cell(reference: str, value: int | float, style: int) -> str:
    return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'


def _formula_cell(
    reference: str,
    formula: str,
    cached_value: int | float,
    style: int,
) -> str:
    return (
        f'<c r="{reference}" s="{style}"><f>{escape(formula)}</f>'
        f"<v>{cached_value}</v></c>"
    )


def _worksheet_xml(records: Sequence[BatchRecord], generated_at: datetime) -> str:
    data_start = 8
    data_end = data_start + len(records) - 1
    total_row = data_start + len(records)
    total_tokens = sum(record.total_tokens for record in records)
    total_seconds = round(sum(record.elapsed_seconds for record in records), 3)

    rows: list[str] = []
    rows.append(
        '<row r="1" ht="28" customHeight="1">'
        + _inline_cell("A1", "课程视频批量处理统计", 1)
        + "</row>"
    )
    rows.append(
        '<row r="2" ht="20" customHeight="1">'
        + _inline_cell(
            "A2",
            f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
            2,
        )
        + "</row>"
    )
    rows.append(
        '<row r="3" ht="20" customHeight="1">'
        + _inline_cell("A3", "Token 以模型服务返回的 usage 为准；未返回时记为 0。", 2)
        + "</row>"
    )

    if records:
        count_cell = _formula_cell("B5", f"COUNTA(B{data_start}:B{data_end})", len(records), 4)
        token_cell = _formula_cell("E5", f"SUM(D{data_start}:D{data_end})", total_tokens, 4)
        time_cell = _formula_cell("B6", f"SUM(E{data_start}:E{data_end})", total_seconds, 5)
    else:
        count_cell = _number_cell("B5", 0, 4)
        token_cell = _number_cell("E5", 0, 4)
        time_cell = _number_cell("B6", 0, 5)

    rows.append(
        '<row r="5" ht="22" customHeight="1">'
        + _inline_cell("A5", "视频数量", 3)
        + count_cell
        + _inline_cell("D5", "总 Token", 3)
        + token_cell
        + "</row>"
    )
    rows.append(
        '<row r="6" ht="22" customHeight="1">'
        + _inline_cell("A6", "总耗时（秒）", 3)
        + time_cell
        + "</row>"
    )

    headers = ["序号", "文件名", "状态", "Token 总量", "耗时（秒）"]
    header_cells = "".join(
        _inline_cell(f"{column}7", value, 6)
        for column, value in zip("ABCDE", headers, strict=True)
    )
    rows.append(f'<row r="7" ht="24" customHeight="1">{header_cells}</row>')

    for index, record in enumerate(records, start=1):
        row_number = data_start + index - 1
        status = _STATUS_LABELS.get(record.status, record.status)
        cells = [
            _number_cell(f"A{row_number}", index, 8),
            _inline_cell(f"B{row_number}", record.name, 7),
            _inline_cell(f"C{row_number}", status, 9),
            _number_cell(f"D{row_number}", record.total_tokens, 8),
            _number_cell(f"E{row_number}", round(record.elapsed_seconds, 3), 10),
        ]
        rows.append(f'<row r="{row_number}" ht="21" customHeight="1">{"".join(cells)}</row>')

    total_cells = [
        _inline_cell(f"A{total_row}", "", 11),
        _inline_cell(f"B{total_row}", "合计", 11),
        _inline_cell(f"C{total_row}", "", 11),
    ]
    if records:
        total_cells.extend(
            [
                _formula_cell(
                    f"D{total_row}",
                    f"SUM(D{data_start}:D{data_end})",
                    total_tokens,
                    12,
                ),
                _formula_cell(
                    f"E{total_row}",
                    f"SUM(E{data_start}:E{data_end})",
                    total_seconds,
                    13,
                ),
            ]
        )
    else:
        total_cells.extend(
            [
                _number_cell(f"D{total_row}", 0, 12),
                _number_cell(f"E{total_row}", 0, 13),
            ]
        )
    rows.append(f'<row r="{total_row}" ht="23" customHeight="1">{"".join(total_cells)}</row>')

    auto_filter = f'<autoFilter ref="A7:E{data_end}"/>' if records else ""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:E{total_row}"/>
  <sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="7" topLeftCell="A8" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>
    <col min="1" max="1" width="9" customWidth="1"/>
    <col min="2" max="2" width="46" customWidth="1"/>
    <col min="3" max="3" width="15" customWidth="1"/>
    <col min="4" max="4" width="18" customWidth="1"/>
    <col min="5" max="5" width="18" customWidth="1"/>
  </cols>
  <sheetData>{''.join(rows)}</sheetData>
  <mergeCells count="3"><mergeCell ref="A1:E1"/><mergeCell ref="A2:E2"/><mergeCell ref="A3:E3"/></mergeCells>
  {auto_filter}
  <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>'''


_CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''

_ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''

_WORKBOOK = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="24000" windowHeight="12000"/></bookViews>
  <sheets><sheet name="批次统计" sheetId="1" r:id="rId1"/></sheets>
  <calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>
</workbook>'''

_WORKBOOK_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>
  <fonts count="5">
    <font><sz val="11"/><name val="Microsoft YaHei"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="16"/><name val="Microsoft YaHei"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><b/><color rgb="FF1F2937"/><sz val="11"/><name val="Microsoft YaHei"/></font>
    <font><i/><color rgb="FF64748B"/><sz val="10"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9EAF7"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="3">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left/><right/><top/><bottom style="thin"><color rgb="FFD1D5DB"/></bottom><diagonal/></border>
    <border><left/><right/><top style="double"><color rgb="FF64748B"/></top><bottom/><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="14">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="3" fontId="3" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="164" fontId="3" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="2" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>
    <xf numFmtId="3" fontId="3" fillId="4" borderId="2" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="164" fontId="3" fillId="4" borderId="2" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

_APP_PROPERTIES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>video-content-parser</Application>
</Properties>'''


def _core_properties(generated_at: datetime) -> str:
    timestamp = generated_at.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>video-content-parser</dc:creator>
  <cp:lastModifiedBy>video-content-parser</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>'''


def export_batch_summary_xlsx(
    records: Sequence[BatchRecord],
    xlsx_path: Path,
    *,
    generated_at: datetime | None = None,
) -> None:
    """Write a formatted XLSX batch report with per-video and formula-driven totals."""

    created_at = generated_at or datetime.now().astimezone()
    target = Path(xlsx_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("docProps/app.xml", _APP_PROPERTIES)
        archive.writestr("docProps/core.xml", _core_properties(created_at))
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(records, created_at))
