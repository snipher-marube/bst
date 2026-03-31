"""
Data export views – supports CSV, JSON, and Excel (xlsx).
"""
import csv
import io
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.core.serializers.json import DjangoJSONEncoder

from apps.dashboards.models import DataTable, AuditLog

logger = logging.getLogger(__name__)


def _get_table_for_user(table_id, user):
    """Return the DataTable if the user's current workspace owns it, else 404."""
    workspace = getattr(user, 'current_workspace', None)
    if not workspace:
        return None
    return get_object_or_404(DataTable, id=table_id, workspace=workspace, is_active=True)


def _records_to_rows(table):
    """Yield dicts (one per active record) in schema column order."""
    column_order = [f['name'] for f in table.schema] if table.schema else None
    for record in table.records.filter(is_active=True).order_by('created_at'):
        data = record.data or {}
        if column_order:
            row = {col: data.get(col, '') for col in column_order}
        else:
            row = data
        row['_id'] = str(record.id)
        row['_created_at'] = record.created_at.isoformat() if record.created_at else ''
        yield row


@login_required
def export_table(request, table_id):
    """
    Export a table's records.

    Query params:
        format  – csv (default) | json | excel
    """
    fmt = request.GET.get('format', 'csv').lower()
    table = _get_table_for_user(table_id, request.user)

    if table is None:
        return JsonResponse({'error': 'No active workspace or table not found'}, status=404)

    # Audit
    AuditLog.objects.create(
        workspace=table.workspace,
        user=request.user,
        action='export',
        content_type='DataTable',
        object_id=table.id,
        object_repr=str(table),
        changes={'format': fmt},
        ip_address=request.META.get('REMOTE_ADDR'),
    )

    rows = list(_records_to_rows(table))
    filename = f"{table.name.replace(' ', '_')}_export"

    if fmt == 'json':
        return _export_json(rows, filename)
    elif fmt in ('excel', 'xlsx'):
        return _export_excel(rows, filename, table)
    else:
        return _export_csv(rows, filename, table)


def _export_csv(rows, filename, table):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'

    if not rows:
        return response

    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (v if v is not None else '') for k, v in row.items()})

    return response


def _export_json(rows, filename):
    content = json.dumps(rows, cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)
    response = HttpResponse(content, content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="{filename}.json"'
    return response


def _export_excel(rows, filename, table):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return JsonResponse({'error': 'openpyxl is required for Excel export'}, status=500)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = table.name[:31]  # Excel sheet name limit

    if not rows:
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        response = HttpResponse(
            buffer.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
        return response

    headers = list(rows[0].keys())

    # Header row styling
    header_fill = PatternFill(start_color='4F81BD', end_color='4F81BD', fill_type='solid')
    header_font = Font(color='FFFFFF', bold=True)

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')

    # Data rows
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, key in enumerate(headers, start=1):
            value = row.get(key, '')
            if value is None:
                value = ''
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Auto-fit column widths (approximate)
    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    response = HttpResponse(
        buffer.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
    return response
