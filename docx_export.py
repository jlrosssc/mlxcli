import re
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape


def inline_runs(markdown):
    runs = []
    token_re = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)")
    pos = 0
    for match in token_re.finditer(markdown):
        if match.start() > pos:
            runs.append(run_xml(markdown[pos:match.start()]))
        token = match.group(0)
        if token.startswith("**"):
            runs.append(run_xml(token[2:-2], bold=True))
        elif token.startswith("*"):
            runs.append(run_xml(token[1:-1], italic=True))
        elif token.startswith("`"):
            runs.append(run_xml(token[1:-1], code=True))
        pos = match.end()
    if pos < len(markdown):
        runs.append(run_xml(markdown[pos:]))
    return "".join(runs) or run_xml("")


def run_xml(text, bold=False, italic=False, code=False):
    props = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if code:
        props.append('<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/><w:shd w:fill="EDEFF3"/>')
    prop_xml = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f"<w:r>{prop_xml}<w:t{preserve}>{escape(text)}</w:t></w:r>"


def paragraph_xml(text, style=None, num_id=None, ilvl=0):
    p_pr_parts = []
    if style:
        p_pr_parts.append(f'<w:pStyle w:val="{style}"/>')
    if num_id is not None:
        p_pr_parts.append(
            f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>'
        )
    p_pr = f"<w:pPr>{''.join(p_pr_parts)}</w:pPr>" if p_pr_parts else ""
    return f"<w:p>{p_pr}{inline_runs(text)}</w:p>"


def table_xml(rows):
    cells_xml = []
    for row in rows:
        cell_parts = []
        for cell in row:
            cell_parts.append(
                "<w:tc>"
                '<w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                f"{paragraph_xml(cell.strip())}"
                "</w:tc>"
            )
        cells_xml.append(f"<w:tr>{''.join(cell_parts)}</w:tr>")
    borders = (
        "<w:tblPr><w:tblBorders>"
        '<w:top w:val="single" w:sz="6" w:space="0" w:color="B7C0CC"/>'
        '<w:left w:val="single" w:sz="6" w:space="0" w:color="B7C0CC"/>'
        '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="B7C0CC"/>'
        '<w:right w:val="single" w:sz="6" w:space="0" w:color="B7C0CC"/>'
        '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="D8DEE8"/>'
        '<w:insideV w:val="single" w:sz="6" w:space="0" w:color="D8DEE8"/>'
        "</w:tblBorders></w:tblPr>"
    )
    return f"<w:tbl>{borders}{''.join(cells_xml)}</w:tbl>"


def is_table_separator(line):
    stripped = line.strip()
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped))


def parse_table_row(line):
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def markdown_blocks(markdown):
    lines = markdown.splitlines()
    i = 0
    in_code = False
    code_lines = []
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                yield ("code", "\n".join(code_lines))
                code_lines = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            rows = [parse_table_row(line)]
            i += 2
            while i < len(lines) and "|" in lines[i].strip():
                rows.append(parse_table_row(lines[i]))
                i += 1
            yield ("table", rows)
            continue
        yield ("line", line)
        i += 1
    if code_lines:
        yield ("code", "\n".join(code_lines))


def body_xml(title, markdown):
    body = [paragraph_xml(title, "Title")]
    body.append(paragraph_xml(f"Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}", "Subtitle"))
    for kind, value in markdown_blocks(markdown):
        if kind == "table":
            body.append(table_xml(value))
            continue
        if kind == "code":
            for code_line in value.splitlines() or [""]:
                body.append(paragraph_xml(code_line, "Code"))
            continue
        stripped = value.strip()
        if not stripped:
            body.append(paragraph_xml(""))
        elif stripped.startswith("### "):
            body.append(paragraph_xml(stripped[4:], "Heading3"))
        elif stripped.startswith("## "):
            body.append(paragraph_xml(stripped[3:], "Heading2"))
        elif stripped.startswith("# "):
            body.append(paragraph_xml(stripped[2:], "Heading1"))
        elif re.match(r"^[-*]\s+", stripped):
            body.append(paragraph_xml(re.sub(r"^[-*]\s+", "", stripped), "ListParagraph", num_id=1))
        elif re.match(r"^\d+[.)]\s+", stripped):
            body.append(paragraph_xml(re.sub(r"^\d+[.)]\s+", "", stripped), "ListParagraph", num_id=2))
        else:
            body.append(paragraph_xml(stripped))
    return "".join(body)


def markdown_to_docx(path, title, markdown):
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {body_xml(title, markdown)}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/></w:rPr>
    <w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="0B2545"/><w:sz w:val="44"/></w:rPr>
    <w:pPr><w:spacing w:after="220"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:color w:val="667085"/><w:sz w:val="20"/></w:rPr>
    <w:pPr><w:spacing w:after="260"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="32"/></w:rPr>
    <w:pPr><w:spacing w:before="360" w:after="160"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="2F6FA3"/><w:sz w:val="26"/></w:rPr>
    <w:pPr><w:spacing w:before="260" w:after="120"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="344054"/><w:sz w:val="23"/></w:rPr>
    <w:pPr><w:spacing w:before="180" w:after="80"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph">
    <w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Code">
    <w:name w:val="Code"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/><w:sz w:val="19"/></w:rPr>
    <w:pPr><w:spacing w:before="40" w:after="40"/><w:shd w:fill="F2F4F7"/></w:pPr>
  </w:style>
</w:styles>'''
    numbering_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
  <w:abstractNum w:abstractNumId="2"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    doc_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/numbering.xml", numbering_xml)
