"""
Document generation utilities for CRM Chat.

Generates PDF, DOCX, PPTX, XLSX, and CSV files from AI-generated text content.
"""

import io
import os
import csv
import re
import uuid
import logging
from django.conf import settings
from reportlab.platypus import Spacer

logger = logging.getLogger(__name__)


# ========================= CONSTANTS =========================

GENERATED_DOCS_DIR = "generated_docs"

# The marker the AI outputs to signal document generation
DOCGEN_PATTERN = re.compile(
    r'\$\$DOCGEN\s*(\{.*?\})\s*\$\$',
    re.DOTALL,
)


# ========================= MAIN DISPATCHER =========================

def parse_docgen_marker(full_text: str):
    """
    Look for $$DOCGEN{...}$$ in the AI response.

    Returns:
        (body_text, config_dict) if found, else (None, None)

        body_text:   everything before the marker (the document content)
        config_dict: parsed JSON with keys like 'format', 'title'
    """
    import json

    match = DOCGEN_PATTERN.search(full_text)
    if not match:
        return None, None

    try:
        config = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse DOCGEN JSON: %s", e)
        return None, None

    # Body is everything before the marker
    body_text = full_text[:match.start()].strip()

    return body_text, config


def generate_document_file(body_text: str, doc_format: str, title: str):
    """
    Generate a document file from body text.

    Args:
        body_text:  the document content (plain text / markdown)
        doc_format: one of 'pdf', 'docx', 'pptx', 'xlsx', 'csv'
        title:      document title

    Returns:
        (relative_path, filename) — path relative to MEDIA_ROOT
    """
    doc_format = (doc_format or "pdf").lower().strip()

    generators = {
        "pdf":  _generate_pdf,
        "docx": _generate_docx,
        "pptx": _generate_pptx,
        "xlsx": _generate_xlsx,
        "csv":  _generate_csv,
    }

    generator = generators.get(doc_format)
    if generator is None:
        logger.warning("Unsupported doc format '%s', falling back to PDF", doc_format)
        generator = _generate_pdf
        doc_format = "pdf"

    file_bytes, extension = generator(body_text, title)

    # Build a unique filename
    safe_title = re.sub(r'[^\w\s-]', '', title or "document")[:60].strip().replace(" ", "_")
    unique_id = uuid.uuid4().hex[:8]
    filename = f"{safe_title}_{unique_id}.{extension}"

    # Save to media directory
    from django.core.files.storage import default_storage
    from django.core.files.base import ContentFile
   
    relative_path = f"{GENERATED_DOCS_DIR}/{filename}"
   
    # Save using Django's storage backend (handles local or Azure Blob automatically)
    saved_path = default_storage.save(relative_path, ContentFile(file_bytes))
    relative_path = saved_path

    logger.info(
        "Generated %s document: %s (%d bytes)",
        doc_format.upper(), filename, len(file_bytes),
    )

    return relative_path, filename


# ========================= PDF GENERATOR =========================

def _generate_pdf(body_text: str, title: str):
    """Generate a PDF from body text using reportlab with full markdown support."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor, Color
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Title'],
        fontSize=22,
        spaceAfter=20,
        textColor=HexColor('#1a1a2e'),
    )

    h1_style = ParagraphStyle(
        'DocH1',
        parent=styles['Heading1'],
        fontSize=18,
        spaceBefore=20,
        spaceAfter=10,
        textColor=HexColor('#1a1a2e'),
    )

    h2_style = ParagraphStyle(
        'DocH2',
        parent=styles['Heading2'],
        fontSize=15,
        spaceBefore=16,
        spaceAfter=8,
        textColor=HexColor('#16213e'),
    )

    h3_style = ParagraphStyle(
        'DocH3',
        parent=styles['Heading3'],
        fontSize=13,
        spaceBefore=14,
        spaceAfter=6,
        textColor=HexColor('#1f4068'),
    )

    h4_style = ParagraphStyle(
        'DocH4',
        parent=styles['Heading4'],
        fontSize=11,
        spaceBefore=12,
        spaceAfter=6,
        textColor=HexColor('#1f4068'),
        bold=True,
    )

    body_style = ParagraphStyle(
        'DocBody',
        parent=styles['Normal'],
        fontSize=10,
        leading=15,
        spaceAfter=6,
    )

    bullet_style = ParagraphStyle(
        'DocBullet',
        parent=styles['Normal'],
        fontSize=10,
        leading=15,
        spaceAfter=4,
        leftIndent=20,
        bulletIndent=8,
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        textColor=HexColor('#ffffff'),
        alignment=TA_CENTER,
    )

    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
    )

    flowables = []

    # Title
    if title:
        flowables.append(Paragraph(_escape_xml(title), title_style))
        flowables.append(Spacer(1, 12))

    # Parse body text
    lines = body_text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Empty line → spacer
        if not stripped:
            flowables.append(Spacer(1, 6))
            i += 1
            continue

        # Markdown table detection: | col | col |
        if stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 3:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith('|') and lines[i].strip().endswith('|'):
                line = lines[i].strip()
                # Skip separator rows like |---|---|
                if not re.match(r'^\|[\s\-:|\+]+\|$', line):
                    cells = [c.strip() for c in line.strip('|').split('|')]
                    table_lines.append(cells)
                i += 1

            if table_lines:
                flowables.append(Spacer(1, 8))
                flowables.append(_build_pdf_table(
                    table_lines, table_header_style, table_cell_style
                ))
                flowables.append(Spacer(1, 8))
            continue

        # Heading detection (#### → #)
        if stripped.startswith('#'):
            heading_match = re.match(r'^(#{1,6})\s+(.*)', stripped)
            if heading_match:
                level = len(heading_match.group(1))
                text = _md_inline_to_rl(heading_match.group(2))
                style_map = {1: h1_style, 2: h2_style, 3: h3_style}
                style = style_map.get(level, h4_style)
                flowables.append(Paragraph(text, style))
                i += 1
                continue

        # Bullet points (- or *)
        if stripped.startswith('- ') or stripped.startswith('* '):
            text = _md_inline_to_rl(stripped[2:])
            flowables.append(Paragraph(f"•  {text}", bullet_style))
            i += 1
            continue

        # Numbered lists (1. 2. etc.)
        num_match = re.match(r'^(\d+)\.\s+(.*)', stripped)
        if num_match:
            num = num_match.group(1)
            text = _md_inline_to_rl(num_match.group(2))
            flowables.append(Paragraph(f"{num}.  {text}", bullet_style))
            i += 1
            continue

        # Regular paragraph — convert inline markdown
        text = _md_inline_to_rl(stripped)
        flowables.append(Paragraph(text, body_style))
        i += 1

    doc.build(flowables)
    return buffer.getvalue(), "pdf"


def _escape_xml(text: str) -> str:
    """Escape XML special characters for reportlab Paragraph."""
    return (
        text.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
    )


def _md_inline_to_rl(text: str) -> str:
    """
    Convert inline markdown to reportlab XML tags.
    **bold** → <b>bold</b>
    *italic* → <i>italic</i>
    `code` → <font face="Courier">code</font>
    """
    # First escape XML
    text = _escape_xml(text)

    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # Italic: *text* or _text_ (but not inside bold)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)

    # Inline code: `text`
    text = re.sub(r'`(.+?)`', r'<font face="Courier" size="9">\1</font>', text)

    return text


def _build_pdf_table(rows, header_style, cell_style):
    """Build a reportlab Table from parsed markdown table rows."""
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib.colors import HexColor, Color
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph

    if not rows:
        return Spacer(1, 0)

    # Convert cells to Paragraphs
    table_data = []
    for row_idx, row in enumerate(rows):
        styled_row = []
        for cell in row:
            style = header_style if row_idx == 0 else cell_style
            text = _md_inline_to_rl(cell) if row_idx > 0 else _escape_xml(cell)
            if row_idx == 0:
                text = f"<b>{text}</b>"
            styled_row.append(Paragraph(text, style))
        table_data.append(styled_row)

    # Calculate column widths
    page_width = (8.27 - 1.5) * inch  # A4 width minus margins
    num_cols = max(len(r) for r in table_data)
    col_width = page_width / num_cols

    table = Table(table_data, colWidths=[col_width] * num_cols)

    # Style the table
    style_commands = [
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1a1a2e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), HexColor('#ffffff')),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),

        # Body rows
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 6),

        # Grid
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#cccccc')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]

    # Alternate row colors for body
    for row_idx in range(1, len(table_data)):
        if row_idx % 2 == 0:
            style_commands.append(
                ('BACKGROUND', (0, row_idx), (-1, row_idx), HexColor('#f5f5f5'))
            )

    table.setStyle(TableStyle(style_commands))
    return table


# ========================= DOCX GENERATOR =========================

def _generate_docx(body_text: str, title: str):
    """Generate a DOCX from body text using python-docx with full markdown support."""
    import docx
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn

    document = docx.Document()

    # Title
    if title:
        heading = document.add_heading(title, level=0)
        for run in heading.runs:
            run.font.color.rgb = RGBColor(0x1a, 0x1a, 0x2e)

    # Parse body
    lines = body_text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if not stripped:
            i += 1
            continue

        # Markdown table detection
        if stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 3:
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith('|') and lines[i].strip().endswith('|'):
                line = lines[i].strip()
                if not re.match(r'^\|[\s\-:|\+]+\|$', line):
                    cells = [c.strip() for c in line.strip('|').split('|')]
                    table_rows.append(cells)
                i += 1

            if table_rows:
                num_cols = max(len(r) for r in table_rows)
                table = document.add_table(rows=len(table_rows), cols=num_cols)
                table.style = 'Table Grid'
                table.alignment = WD_TABLE_ALIGNMENT.CENTER

                for row_idx, row_data in enumerate(table_rows):
                    for col_idx, cell_text in enumerate(row_data):
                        if col_idx < num_cols:
                            cell = table.rows[row_idx].cells[col_idx]
                            cell.text = ''
                            para = cell.paragraphs[0]
                            # Remove **bold** markers and apply bold formatting
                            clean_text = re.sub(r'\*\*(.+?)\*\*', r'\1', cell_text)
                            run = para.add_run(clean_text)
                            run.font.size = Pt(9)
                            if row_idx == 0:
                                run.bold = True
                                # Header row shading
                                shading = cell._element.get_or_add_tcPr()
                                shd = docx.oxml.OxmlElement('w:shd')
                                shd.set(qn('w:fill'), '1a1a2e')
                                shd.set(qn('w:val'), 'clear')
                                shading.append(shd)
                                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

                document.add_paragraph('')  # spacing after table
            continue

        # Heading detection (all levels)
        heading_match = re.match(r'^(#{1,6})\s+(.*)', stripped)
        if heading_match:
            level = min(len(heading_match.group(1)), 4)  # docx supports 0-4
            text = re.sub(r'\*\*(.+?)\*\*', r'\1', heading_match.group(2))
            document.add_heading(text, level=level)
            i += 1
            continue

        # Bullet points
        if stripped.startswith('- ') or stripped.startswith('* '):
            text = stripped[2:]
            para = document.add_paragraph(style='List Bullet')
            _add_docx_runs_with_bold(para, text)
            i += 1
            continue

        # Numbered lists
        num_match = re.match(r'^(\d+)\.\s+(.*)', stripped)
        if num_match:
            text = num_match.group(2)
            para = document.add_paragraph(style='List Number')
            _add_docx_runs_with_bold(para, text)
            i += 1
            continue

        # Regular paragraph
        para = document.add_paragraph()
        _add_docx_runs_with_bold(para, stripped)
        for run in para.runs:
            run.font.size = Pt(11)
        i += 1

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), "docx"


def _add_docx_runs_with_bold(para, text: str):
    """
    Parse inline **bold** in text and add as separate runs to a docx paragraph.
    E.g. "**Name:** Alice" → bold "Name:" + normal " Alice"
    """
    import docx

    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        if part.startswith('**') and part.endswith('**'):
            run = para.add_run(part[2:-2])
            run.bold = True
        else:
            para.add_run(part)


# ========================= PPTX GENERATOR =========================

def _generate_pptx(body_text: str, title: str):
    """Generate a PPTX from body text using python-pptx."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Title slide
    title_slide_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(title_slide_layout)
    slide.shapes.title.text = title or "Presentation"
    if slide.placeholders[1]:
        slide.placeholders[1].text = "Generated Document"

    # Split content by slide markers or by headings
    if '---SLIDE---' in body_text:
        slides_content = body_text.split('---SLIDE---')
    else:
        # Split by ## headings
        slides_content = re.split(r'\n(?=## )', body_text)
        if len(slides_content) <= 1:
            # Fall back: split by # headings
            slides_content = re.split(r'\n(?=# )', body_text)

    for slide_text in slides_content:
        slide_text = slide_text.strip()
        if not slide_text:
            continue

        slide_lines = slide_text.split('\n')
        slide_title = slide_lines[0].lstrip('#').strip() if slide_lines else "Slide"
        slide_body = '\n'.join(slide_lines[1:]).strip() if len(slide_lines) > 1 else ""

        content_layout = prs.slide_layouts[1]  # Title + Content
        slide = prs.slides.add_slide(content_layout)
        slide.shapes.title.text = slide_title

        if slide_body and slide.placeholders[1]:
            tf = slide.placeholders[1].text_frame
            tf.text = ""

            body_lines = slide_body.split('\n')
            for i, bline in enumerate(body_lines):
                bline = bline.strip()
                if not bline:
                    continue

                if bline.startswith('- ') or bline.startswith('* '):
                    bline = bline[2:]

                if i == 0:
                    tf.text = bline
                else:
                    p = tf.add_paragraph()
                    p.text = bline
                    p.font.size = Pt(18)

    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue(), "pptx"


# ========================= XLSX GENERATOR =========================

def _generate_xlsx(body_text: str, title: str):
    """Generate an XLSX from body text using openpyxl."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (title or "Sheet1")[:31]  # Excel sheet name limit

    # Try to parse markdown table or CSV-like data
    rows = _extract_table_data(body_text)

    if rows:
        # Header styling
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="1a1a2e", end_color="1a1a2e", fill_type="solid")

        for row_idx, row_data in enumerate(rows, start=1):
            for col_idx, cell_value in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=cell_value.strip())

                if row_idx == 1:
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = Alignment(horizontal='center')

        # Auto-adjust column widths
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_length + 4, 50)
    else:
        # Plain text fallback: put each line in a row
        for row_idx, line in enumerate(body_text.split('\n'), start=1):
            ws.cell(row=row_idx, column=1, value=line.strip())

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), "xlsx"


# ========================= CSV GENERATOR =========================

def _generate_csv(body_text: str, title: str):
    """Generate a CSV from body text."""
    output = io.StringIO()
    writer = csv.writer(output)

    rows = _extract_table_data(body_text)

    if rows:
        for row in rows:
            writer.writerow([cell.strip() for cell in row])
    else:
        # Fallback: each line is a row with one column
        for line in body_text.split('\n'):
            stripped = line.strip()
            if stripped:
                writer.writerow([stripped])

    return output.getvalue().encode('utf-8-sig'), "csv"


# ========================= HELPERS =========================

def _extract_table_data(text: str):
    """
    Extract tabular data from text.

    Handles:
    - Markdown tables:  | col1 | col2 |
    - CSV-style:        col1, col2, col3
    - Tab-separated:    col1\tcol2\tcol3

    Returns list of lists, or empty list if no table found.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # 1. Try markdown table
    md_rows = []
    for line in lines:
        if line.startswith('|') and line.endswith('|'):
            # Skip separator rows like |---|---|
            if re.match(r'^\|[\s\-:|\+]+\|$', line):
                continue
            cells = [c.strip() for c in line.strip('|').split('|')]
            md_rows.append(cells)

    if len(md_rows) >= 2:
        return md_rows

    # 2. Try CSV-style (comma separated, at least 2 columns per row)
    csv_rows = []
    for line in lines:
        # Skip lines that look like prose (long sentences without commas for structure)
        if ',' in line:
            cells = [c.strip() for c in line.split(',')]
            if len(cells) >= 2:
                csv_rows.append(cells)

    if len(csv_rows) >= 2:
        return csv_rows

    # 3. Try tab-separated
    tab_rows = []
    for line in lines:
        if '\t' in line:
            cells = [c.strip() for c in line.split('\t')]
            if len(cells) >= 2:
                tab_rows.append(cells)

    if len(tab_rows) >= 2:
        return tab_rows

    return []
