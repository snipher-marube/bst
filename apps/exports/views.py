"""
apps/exports/views.py
=====================
Data-export views for AnalyticsMeta.

Supported formats
-----------------
csv (default)
    RFC 4180 CSV with a header row.  Column order follows the table's
    schema definition; ``_id`` and ``_created_at`` meta-columns are
    appended at the end.  Streamed row-by-row — never loads all records
    into memory, so tables with millions of rows export safely.

json
    JSON array streamed incrementally (``[`` … records … ``]``).
    Each element is serialised through Django's ``DjangoJSONEncoder`` so
    dates, UUIDs and Decimals round-trip safely.

excel / xlsx
    OpenPyXL workbook with a single sheet named after the table.
    The header row is styled with a blue fill and white bold text.
    Columns are auto-width-fitted (capped at 50 characters).
    NOTE: Excel export loads all rows into memory (openpyxl limitation).
    Tables with more than EXCEL_MAX_ROWS rows are rejected with 400 to
    prevent OOM; use CSV for large datasets.

URL
---
All three formats share a single endpoint::

    GET /exports/tables/<uuid:table_id>/export/?format=csv|json|excel

Authentication
--------------
``@login_required`` — unauthenticated requests are redirected to the
login page.

Workspace isolation
-------------------
Every request resolves the table against ``request.user.current_workspace``
(set by ``CurrentWorkspaceMiddleware``).  A table from a different
workspace returns **404** even if the UUID is otherwise valid.

Audit trail
-----------
Every successful export writes one ``AuditLog`` row recording the user,
table, and chosen format for compliance purposes.
"""
import csv
import io
import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.core.serializers.json import DjangoJSONEncoder

from apps.dashboards.models import DataTable, AuditLog

logger = logging.getLogger(__name__)

# Maximum rows allowed for Excel export before we refuse to avoid OOM.
# Override via settings.EXCEL_MAX_EXPORT_ROWS (default 50 000).
EXCEL_MAX_ROWS = getattr(settings, 'EXCEL_MAX_EXPORT_ROWS', 50_000)


def _get_table_for_user(table_id, user):
    """Return the ``DataTable`` owned by the user's active workspace, or ``None``.

    Returns ``None`` (rather than 404) when the user has no current workspace
    so the caller can respond with a structured JSON 404 instead of a redirect.
    """
    workspace = getattr(user, 'current_workspace', None)
    if not workspace:
        return None
    return get_object_or_404(DataTable, id=table_id, workspace=workspace, is_active=True)


def _column_order(table):
    """Return the ordered list of user-defined column names for a table."""
    return [f['name'] for f in table.schema] if table.schema else None


def _record_iter(table):
    """Yield one ``dict`` per active record using a server-side cursor.

    Uses ``.iterator(chunk_size=500)`` so Django never loads more than
    ~500 records at a time from the database.

    Two meta-columns — ``_id`` and ``_created_at`` — are appended after
    the schema-defined columns.
    """
    col_order = _column_order(table)
    qs = table.records.filter(is_active=True).order_by('created_at')
    for record in qs.iterator(chunk_size=500):
        data = record.data or {}
        if col_order:
            row = {col: data.get(col, '') for col in col_order}
        else:
            row = dict(data)
        row['_id'] = str(record.id)
        row['_created_at'] = record.created_at.isoformat() if record.created_at else ''
        yield row


@login_required
def export_table(request, table_id):
    """Export all active records of a table in the requested format.

    URL: ``GET /exports/tables/<uuid:table_id>/export/?format=csv|json|excel``
    """
    fmt = request.GET.get('format', 'csv').lower()
    table = _get_table_for_user(table_id, request.user)

    if table is None:
        return JsonResponse({'error': 'No active workspace or table not found'}, status=404)

    # Audit every export attempt.
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

    filename = f"{table.name.replace(' ', '_')}_export"

    if fmt == 'json':
        return _export_json_streaming(table, filename)
    elif fmt in ('excel', 'xlsx'):
        return _export_excel(table, filename)
    else:
        return _export_csv_streaming(table, filename)


# ---------------------------------------------------------------------------
# CSV — streamed via StreamingHttpResponse
# ---------------------------------------------------------------------------

class _EchoBuffer:
    """Minimal file-like object for ``csv.writer`` that returns each written
    value so we can yield it from a generator."""
    def write(self, value):
        return value


def _csv_row_generator(table):
    """Yield the header line then one CSV line per record, never holding
    more than one row in memory."""
    col_order = _column_order(table)
    writer = csv.writer(_EchoBuffer())

    # Determine header from schema or first record
    first = None
    record_qs = table.records.filter(is_active=True).order_by('created_at')
    first_rec = record_qs.first()
    if first_rec:
        data = first_rec.data or {}
        if col_order:
            headers = col_order + ['_id', '_created_at']
        else:
            headers = list(data.keys()) + ['_id', '_created_at']
    elif col_order:
        headers = col_order + ['_id', '_created_at']
    else:
        headers = ['_id', '_created_at']

    yield writer.writerow(headers)

    # Stream every record without loading all into memory
    for row in _record_iter(table):
        yield writer.writerow([row.get(h, '') if row.get(h) is not None else '' for h in headers])


def _export_csv_streaming(table, filename):
    response = StreamingHttpResponse(
        _csv_row_generator(table),
        content_type='text/csv; charset=utf-8',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
    return response


# ---------------------------------------------------------------------------
# JSON — streamed as a JSON array
# ---------------------------------------------------------------------------

def _json_array_generator(table):
    """Yield fragments that together form a valid JSON array, one record
    at a time.  This avoids building the entire list in RAM."""
    encoder = DjangoJSONEncoder(ensure_ascii=False)
    first = True
    yield '[\n'
    for row in _record_iter(table):
        if not first:
            yield ',\n'
        yield encoder.encode(row)
        first = False
    yield '\n]\n'


def _export_json_streaming(table, filename):
    response = StreamingHttpResponse(
        _json_array_generator(table),
        content_type='application/json; charset=utf-8',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}.json"'
    return response


# ---------------------------------------------------------------------------
# Excel — must be in-memory (openpyxl limitation); size-guarded
# ---------------------------------------------------------------------------

def _export_excel(table, filename):
    """Return an ``HttpResponse`` containing an OpenPyXL ``.xlsx`` workbook.

    Rejects tables with more than ``EXCEL_MAX_ROWS`` active records to
    prevent OOM.  Use CSV or JSON for larger datasets.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return JsonResponse({'error': 'openpyxl is required for Excel export. '
                                      'Install it with: pip install openpyxl'}, status=500)

    row_count = table.records.filter(is_active=True).count()
    if row_count > EXCEL_MAX_ROWS:
        return JsonResponse(
            {'error': (
                f'Table has {row_count:,} rows which exceeds the Excel export '
                f'limit of {EXCEL_MAX_ROWS:,}. Please use CSV or JSON format instead.'
            )},
            status=400,
        )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = table.name[:31]  # Excel sheet name limit

    # Collect rows (size is known-safe because of the guard above)
    rows = list(_record_iter(table))

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
    header_fill = PatternFill(start_color='03466E', end_color='03466E', fill_type='solid')
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
            ws.cell(row=row_idx, column=col_idx, value='' if value is None else value)

    # Auto-fit column widths (approximate — capped at 50 chars)
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
