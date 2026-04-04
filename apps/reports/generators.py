"""
PDF report generator using ReportLab.

Entry point: ``build_dashboard_pdf(dashboard, insights, widgets_data, requested_by)``
Returns the PDF as bytes.

Layout (A4 portrait):
  1. Cover page   — title, workspace, date, logo watermark
  2. Executive Summary — KPI snapshot + top insights in plain language
  3. Per-widget sections — chart image + caption + data table (if ≤ 15 rows)
  4. AI Insights appendix — full list of trend / anomaly / comparison findings
  5. Footer on every page — page number, workspace name, timestamp
"""

import io
import logging
from datetime import datetime
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy ReportLab imports — raise a clear error if not installed
# ---------------------------------------------------------------------------

def _rl():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm, mm
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
            PageBreak, HRFlowable, KeepTogether,
        )
        from reportlab.platypus.flowables import BalancedColumns
        return dict(
            colors=colors, A4=A4, getSampleStyleSheet=getSampleStyleSheet,
            ParagraphStyle=ParagraphStyle, cm=cm, mm=mm,
            TA_CENTER=TA_CENTER, TA_LEFT=TA_LEFT, TA_RIGHT=TA_RIGHT,
            SimpleDocTemplate=SimpleDocTemplate, Paragraph=Paragraph,
            Spacer=Spacer, Image=Image, Table=Table, TableStyle=TableStyle,
            PageBreak=PageBreak, HRFlowable=HRFlowable, KeepTogether=KeepTogether,
        )
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for PDF generation. "
            "Add 'reportlab' to requirements.txt and reinstall."
        ) from exc


# ---------------------------------------------------------------------------
# Brand constants
# ---------------------------------------------------------------------------

BRAND_DARK   = '#03466e'
BRAND_ACCENT = '#0a8043'
BRAND_ORANGE = '#e37400'
BRAND_LIGHT  = '#e8f4fd'
GREY_TEXT    = '#555555'
GREY_BORDER  = '#dddddd'

WIDGET_COLOR_MAP = {
    'blue':   '#03466e',
    'green':  '#0a8043',
    'orange': '#e37400',
    'purple': '#8e24aa',
    'red':    '#c62828',
    'teal':   '#00838f',
}

KPI_COLORS = list(WIDGET_COLOR_MAP.values())


# ---------------------------------------------------------------------------
# Style factory
# ---------------------------------------------------------------------------

def _make_styles(rl: dict) -> dict:
    PS = rl['ParagraphStyle']
    base = rl['getSampleStyleSheet']()
    return {
        'cover_title': PS('CoverTitle', parent=base['Title'],
                          fontSize=28, textColor=rl['colors'].HexColor(BRAND_DARK),
                          spaceAfter=6, leading=34),
        'cover_sub':   PS('CoverSub', parent=base['Normal'],
                          fontSize=13, textColor=rl['colors'].HexColor(BRAND_DARK),
                          spaceAfter=4),
        'cover_date':  PS('CoverDate', parent=base['Normal'],
                          fontSize=10, textColor=rl['colors'].HexColor(GREY_TEXT)),
        'h1':          PS('H1', parent=base['Heading1'],
                          fontSize=16, textColor=rl['colors'].HexColor(BRAND_DARK),
                          spaceBefore=14, spaceAfter=6),
        'h2':          PS('H2', parent=base['Heading2'],
                          fontSize=12, textColor=rl['colors'].HexColor(BRAND_DARK),
                          spaceBefore=10, spaceAfter=4),
        'body':        PS('Body', parent=base['Normal'],
                          fontSize=9, textColor=rl['colors'].HexColor(GREY_TEXT),
                          spaceAfter=4, leading=13),
        'caption':     PS('Caption', parent=base['Normal'],
                          fontSize=8, textColor=rl['colors'].HexColor(GREY_TEXT),
                          spaceAfter=6, alignment=rl['TA_CENTER']),
        'insight_type': PS('InsightType', parent=base['Normal'],
                           fontSize=7, textColor=rl['colors'].white,
                           leading=10),
        'insight_title': PS('InsightTitle', parent=base['Normal'],
                            fontSize=9, textColor=rl['colors'].HexColor(BRAND_DARK),
                            fontName='Helvetica-Bold', spaceBefore=2, spaceAfter=1),
        'insight_body':  PS('InsightBody', parent=base['Normal'],
                            fontSize=8, textColor=rl['colors'].HexColor(GREY_TEXT),
                            leading=11, spaceAfter=4),
        'footer':      PS('Footer', parent=base['Normal'],
                          fontSize=7, textColor=rl['colors'].HexColor(GREY_TEXT),
                          alignment=rl['TA_CENTER']),
        'kpi_value':   PS('KpiValue', parent=base['Normal'],
                          fontSize=20, fontName='Helvetica-Bold',
                          textColor=rl['colors'].HexColor(BRAND_DARK),
                          alignment=rl['TA_CENTER'], spaceAfter=0),
        'kpi_label':   PS('KpiLabel', parent=base['Normal'],
                          fontSize=8, textColor=rl['colors'].HexColor(GREY_TEXT),
                          alignment=rl['TA_CENTER']),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_number(value: Any) -> str:
    try:
        v = float(value)
        if v >= 1_000_000:
            return f'{v/1_000_000:.1f}M'
        if v >= 1_000:
            return f'{v/1_000:.1f}K'
        return f'{v:,.2f}' if v != int(v) else f'{int(v):,}'
    except (TypeError, ValueError):
        return str(value)


def _pil_image_to_rl(img_bytes: bytes, max_width_cm: float, rl: dict) -> Any:
    """Wrap PNG bytes in a ReportLab Image with max width constraint."""
    from PIL import Image as PILImage
    pil = PILImage.open(io.BytesIO(img_bytes))
    orig_w, orig_h = pil.size
    aspect = orig_h / orig_w
    w = max_width_cm * rl['cm']
    h = w * aspect
    return rl['Image'](io.BytesIO(img_bytes), width=w, height=h)


def _make_data_table(headers: list, rows: list, rl: dict) -> Any:
    """Build a ReportLab Table from headers + row data."""
    col_count = len(headers)
    page_width = A4_WIDTH_CM(rl)
    col_width = (page_width / col_count) * rl['cm']
    col_widths = [col_width] * col_count

    table_data = [headers] + rows
    tbl = rl['Table'](table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(rl['TableStyle']([
        ('BACKGROUND',  (0, 0), (-1, 0),  rl['colors'].HexColor(BRAND_DARK)),
        ('TEXTCOLOR',   (0, 0), (-1, 0),  rl['colors'].white),
        ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',    (0, 0), (-1, 0),  8),
        ('FONTSIZE',    (0, 1), (-1, -1), 7),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [
            rl['colors'].HexColor(BRAND_LIGHT),
            rl['colors'].white,
        ]),
        ('TEXTCOLOR',   (0, 1), (-1, -1), rl['colors'].HexColor(GREY_TEXT)),
        ('GRID',        (0, 0), (-1, -1), 0.4, rl['colors'].HexColor(GREY_BORDER)),
        ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING',     (0, 0), (-1, -1), 4),
    ]))
    return tbl


def A4_WIDTH_CM(rl) -> float:
    """Usable width in cm (A4 minus 2.5 cm margins each side)."""
    return (rl['A4'][0] - 5 * rl['cm']) / rl['cm']


# ---------------------------------------------------------------------------
# Page header / footer callback
# ---------------------------------------------------------------------------

def _make_page_template(workspace_name: str, report_date: str):
    """Return an onPage callback for SimpleDocTemplate."""
    def _on_page(canvas, doc):
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        w, h = doc.pagesize

        # Header bar
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(BRAND_DARK))
        canvas.rect(0, h - 1.1 * cm, w, 1.1 * cm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont('Helvetica-Bold', 9)
        canvas.drawString(1.5 * cm, h - 0.7 * cm, 'AnalyticsMeta')
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(w - 1.5 * cm, h - 0.7 * cm, workspace_name)

        # Footer
        canvas.setFillColor(colors.HexColor(GREY_TEXT))
        canvas.setFont('Helvetica', 7)
        canvas.drawCentredString(w / 2, 0.6 * cm, f'Page {doc.page}  ·  {report_date}')
        canvas.line(1.5 * cm, 0.9 * cm, w - 1.5 * cm, 0.9 * cm)
        canvas.restoreState()

    return _on_page


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_cover(dashboard, workspace, requested_by, report_date: str, rl: dict, styles: dict) -> list:
    cm = rl['cm']
    Spacer_ = rl['Spacer']
    Paragraph_ = rl['Paragraph']
    HRFlowable_ = rl['HRFlowable']
    PageBreak_ = rl['PageBreak']
    colors_ = rl['colors']

    story = [
        Spacer_(1, 3 * cm),
        Paragraph_(dashboard.name, styles['cover_title']),
        HRFlowable_(width='100%', thickness=2, color=colors_.HexColor(BRAND_ACCENT), spaceAfter=8),
        Paragraph_(f"Workspace: <b>{workspace.name}</b>", styles['cover_sub']),
        Paragraph_(f"Generated: {report_date}", styles['cover_date']),
        Paragraph_(f"Requested by: {requested_by.get_full_name() or requested_by.email}",
                   styles['cover_date']),
        Spacer_(1, 0.8 * cm),
    ]

    if dashboard.description:
        story.append(Paragraph_(dashboard.description, styles['body']))

    story.append(PageBreak_())
    return story


def _build_exec_summary(kpi_widgets: list, top_insights: list, rl: dict, styles: dict) -> list:
    """Executive Summary: KPI grid + top-3 insights."""
    Paragraph_ = rl['Paragraph']
    Spacer_    = rl['Spacer']
    Table_     = rl['Table']
    TableStyle_= rl['TableStyle']
    colors_    = rl['colors']
    cm         = rl['cm']

    story = [Paragraph_('Executive Summary', styles['h1'])]

    # KPI mini-cards in a 4-up grid
    if kpi_widgets:
        kpi_cells = []
        for i, (title, value, prefix, suffix, color_key) in enumerate(kpi_widgets[:8]):
            c = WIDGET_COLOR_MAP.get(color_key, KPI_COLORS[i % len(KPI_COLORS)])
            display = f'{prefix}{_fmt_number(value)}{suffix}'
            cell_content = Table_(
                [[Paragraph_(display, styles['kpi_value'])],
                 [Paragraph_(title,   styles['kpi_label'])]],
                colWidths=[3.8 * cm],
            )
            cell_content.setStyle(TableStyle_([
                ('BACKGROUND', (0, 0), (-1, -1), colors_.HexColor(BRAND_LIGHT)),
                ('BOX',        (0, 0), (-1, -1), 1, colors_.HexColor(c)),
                ('TOPPADDING',  (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING',  (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ]))
            kpi_cells.append(cell_content)

        # Pad to multiples of 4
        while len(kpi_cells) % 4 != 0:
            kpi_cells.append(Paragraph_('', styles['body']))

        rows = [kpi_cells[i:i+4] for i in range(0, len(kpi_cells), 4)]
        kpi_table = Table_(rows, colWidths=[4.0 * cm] * 4, hAlign='LEFT')
        kpi_table.setStyle(TableStyle_([
            ('VALIGN',  (0, 0), (-1, -1), 'TOP'),
            ('PADDING', (0, 0), (-1, -1), 4),
        ]))
        story += [kpi_table, Spacer_(1, 0.5 * cm)]

    # Top insights
    if top_insights:
        story.append(Paragraph_('Key Findings', styles['h2']))
        for ins in top_insights[:3]:
            badge_color = {
                'trend': BRAND_DARK, 'anomaly': '#c62828',
                'comparison': BRAND_ACCENT, 'summary': BRAND_ORANGE,
            }.get(ins.insight_type, BRAND_DARK)

            badge = Table_(
                [[Paragraph_(ins.insight_type.upper(), styles['insight_type'])]],
                colWidths=[1.6 * cm],
            )
            badge.setStyle(TableStyle_([
                ('BACKGROUND',    (0, 0), (-1, -1), colors_.HexColor(badge_color)),
                ('TOPPADDING',    (0, 0), (-1, -1), 2),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ('LEFTPADDING',   (0, 0), (-1, -1), 4),
                ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
            ]))

            row_tbl = Table_(
                [[badge, Table_(
                    [[Paragraph_(ins.title, styles['insight_title'])],
                     [Paragraph_(ins.description, styles['insight_body'])]],
                    colWidths=[13 * cm],
                )]],
                colWidths=[1.8 * cm, 13.4 * cm],
            )
            row_tbl.setStyle(TableStyle_([
                ('VALIGN',  (0, 0), (-1, -1), 'TOP'),
                ('PADDING', (0, 0), (-1, -1), 3),
                ('BOX',     (0, 0), (-1, -1), 0.4, colors_.HexColor(GREY_BORDER)),
                ('BACKGROUND', (0, 0), (-1, -1), colors_.HexColor('#fafafa')),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story += [row_tbl, Spacer_(1, 0.2 * cm)]

    story.append(rl['PageBreak']())
    return story


def _build_widget_section(widget, widget_data: Any, rl: dict, styles: dict) -> list:
    """One full section per dashboard widget."""
    from apps.reports.charts import (
        render_line_chart, render_bar_chart, render_pie_chart,
        render_kpi_card, extract_chart_data,
    )
    Paragraph_ = rl['Paragraph']
    Spacer_    = rl['Spacer']
    cm         = rl['cm']

    story = [Paragraph_(widget.title, styles['h2'])]

    wtype = widget.widget_type
    viz   = widget.viz_config or {}

    if wtype == 'metric':
        val    = widget_data.get('val', widget_data.get('value', widget_data.get('count', ''))) \
                 if isinstance(widget_data, dict) else ''
        prefix = viz.get('prefix', '')
        suffix = viz.get('suffix', '')
        color  = WIDGET_COLOR_MAP.get(viz.get('color', ''), KPI_COLORS[0])
        try:
            img_bytes = render_kpi_card(widget.title, val, prefix, suffix, color=color,
                                        width_in=3.5, height_in=2.0)
            rl_img = _pil_image_to_rl(img_bytes, max_width_cm=4.5, rl=rl)
            story += [rl_img, Spacer_(1, 0.2 * cm)]
        except Exception:
            story.append(Paragraph_(f"Value: {prefix}{_fmt_number(val)}{suffix}", styles['body']))

    elif wtype in ('line_chart', 'bar_chart', 'pie_chart'):
        labels, values = extract_chart_data(widget_data)
        if labels and values:
            try:
                if wtype == 'line_chart':
                    img_bytes = render_line_chart(widget.title, labels, values,
                                                  y_label=viz.get('y_axis', ''),
                                                  width_in=6.5, height_in=3.2)
                elif wtype == 'bar_chart':
                    horizontal = len(labels) > 8
                    img_bytes = render_bar_chart(widget.title, labels, values,
                                                 y_label=viz.get('y_axis', ''),
                                                 horizontal=horizontal,
                                                 width_in=6.5, height_in=3.2)
                else:
                    img_bytes = render_pie_chart(widget.title, labels, values,
                                                 width_in=5.0, height_in=3.5)
                rl_img = _pil_image_to_rl(img_bytes, max_width_cm=A4_WIDTH_CM(rl) - 1, rl=rl)
                story += [
                    rl_img,
                    Paragraph_(
                        f'{wtype.replace("_", " ").title()} — {len(labels)} data points',
                        styles['caption'],
                    ),
                ]
            except Exception as exc:
                logger.warning('Chart render failed for widget %s: %s', widget.id, exc)
                story.append(Paragraph_(f'[Chart unavailable: {exc}]', styles['caption']))

            # Data table — only if ≤ 15 rows (avoids bloat)
            if len(labels) <= 15:
                headers = [viz.get('x_axis', 'Label'), viz.get('y_axis', 'Value')]
                rows = [[str(l), _fmt_number(v)] for l, v in zip(labels, values)]
                story += [Spacer_(1, 0.2 * cm), _make_data_table(headers, rows, rl)]

    elif wtype == 'table':
        if isinstance(widget_data, list) and widget_data:
            first_row = widget_data[0]
            if isinstance(first_row, dict):
                headers = list(first_row.keys())
                rows    = [[str(row.get(h, '')) for h in headers] for row in widget_data[:20]]
                story.append(_make_data_table(headers, rows, rl))
                if len(widget_data) > 20:
                    story.append(Paragraph_(
                        f'Showing 20 of {len(widget_data)} records.', styles['caption']
                    ))

    story.append(Spacer_(1, 0.4 * cm))
    return story


def _build_insights_appendix(insights, rl: dict, styles: dict) -> list:
    """Full AI Insights appendix, grouped by type."""
    if not insights:
        return []

    Paragraph_ = rl['Paragraph']
    Spacer_    = rl['Spacer']
    colors_    = rl['colors']
    Table_     = rl['Table']
    TableStyle_= rl['TableStyle']
    cm         = rl['cm']

    story = [
        rl['PageBreak'](),
        Paragraph_('AI Insights Appendix', styles['h1']),
        Paragraph_(
            'The following findings were automatically generated by analyzing your data '
            'for trends, anomalies, and cross-table comparisons.',
            styles['body'],
        ),
        Spacer_(1, 0.3 * cm),
    ]

    from itertools import groupby
    sorted_insights = sorted(insights, key=lambda i: i.insight_type)
    for itype, group in groupby(sorted_insights, key=lambda i: i.insight_type):
        story.append(Paragraph_(itype.title(), styles['h2']))
        for ins in group:
            badge_color = {
                'trend': BRAND_DARK, 'anomaly': '#c62828',
                'comparison': BRAND_ACCENT, 'summary': BRAND_ORANGE,
            }.get(itype, BRAND_DARK)

            content_tbl = Table_(
                [[Paragraph_(ins.title, styles['insight_title'])],
                 [Paragraph_(ins.description, styles['insight_body'])],
                 [Paragraph_(
                     f'Source: {ins.source_table_name or "–"}  ·  '
                     f'{ins.created_at.strftime("%b %d, %Y")}',
                     styles['caption'],
                 )]],
                colWidths=[A4_WIDTH_CM(rl) * cm - 2 * cm],
            )
            content_tbl.setStyle(TableStyle_([
                ('BOX',           (0, 0), (-1, -1), 0.4, colors_.HexColor(badge_color)),
                ('LEFTPADDING',   (0, 0), (-1, -1), 8),
                ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
                ('TOPPADDING',    (0, 0), (0,  0),  6),
                ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
                ('BACKGROUND',    (0, 0), (-1, -1), colors_.HexColor('#fafafa')),
            ]))
            story += [content_tbl, Spacer_(1, 0.25 * cm)]

    return story


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_dashboard_pdf(
    dashboard,
    workspace,
    insights,
    widgets_with_data: list[tuple],   # [(widget, widget_data), ...]
    requested_by,
) -> bytes:
    """
    Build and return PDF bytes for the given dashboard.

    :param dashboard:         Dashboard model instance
    :param workspace:         Workspace model instance
    :param insights:          QuerySet / list of Insight instances
    :param widgets_with_data: List of (Widget, raw_data_dict) tuples
    :param requested_by:      User who triggered the report
    :returns:                 PDF file as bytes
    """
    rl = _rl()
    report_date = timezone.now().strftime('%B %d, %Y at %H:%M %Z')
    styles = _make_styles(rl)
    on_page = _make_page_template(workspace.name, report_date)

    buf = io.BytesIO()
    doc = rl['SimpleDocTemplate'](
        buf,
        pagesize=rl['A4'],
        leftMargin=2.5 * rl['cm'],
        rightMargin=2.5 * rl['cm'],
        topMargin=2.0 * rl['cm'],
        bottomMargin=1.5 * rl['cm'],
        title=dashboard.name,
        author=workspace.name,
        subject='AnalyticsMeta Dashboard Report',
    )

    story = []

    # 1. Cover
    story += _build_cover(dashboard, workspace, requested_by, report_date, rl, styles)

    # 2. Executive Summary — collect KPI data from metric widgets
    kpi_widgets = []
    for widget, data in widgets_with_data:
        if widget.widget_type == 'metric':
            val    = data.get('val', data.get('value', data.get('count', ''))) \
                     if isinstance(data, dict) else ''
            prefix = (widget.viz_config or {}).get('prefix', '')
            suffix = (widget.viz_config or {}).get('suffix', '')
            color  = (widget.viz_config or {}).get('color', '')
            kpi_widgets.append((widget.title, val, prefix, suffix, color))

    story += _build_exec_summary(kpi_widgets, list(insights)[:5], rl, styles)

    # 3. Per-widget sections (skip metrics — already in exec summary)
    non_kpi = [(w, d) for w, d in widgets_with_data if w.widget_type != 'metric']
    if non_kpi:
        story.append(rl['Paragraph']('Dashboard Visualizations', styles['h1']))
        for widget, data in non_kpi:
            story += _build_widget_section(widget, data, rl, styles)

    # 4. AI Insights appendix
    story += _build_insights_appendix(list(insights), rl, styles)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
