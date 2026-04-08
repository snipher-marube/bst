"""
apps/exports/views.py
=====================
Data-export views for AnalyticsMeta.

Supported formats
-----------------
csv (default)
    RFC 4180 CSV with a header row.  Column order follows the table's
    schema definition; ``_id`` and ``_created_at`` meta-columns are
    appended at the end.

json
    Pretty-printed JSON array.  Each element is one record dict with
    the same column order as CSV, serialised through Django's
    ``DjangoJSONEncoder`` so dates, UUIDs and Decimals round-trip safely.

excel / xlsx
    OpenPyXL workbook with a single sheet named after the table.
    The header row is styled with a blue fill and white bold text.
    Columns are auto-width-fitted (capped at 50 characters).

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

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.core.serializers.json import DjangoJSONEncoder

from apps.dashboards.models import DataTable, AuditLog

logger = logging.getLogger(__name__)


def _get_table_for_user(table_id, user):
    """Return the ``DataTable`` owned by the user's active workspace, or ``None``.

    Returns ``None`` (rather than 404) when the user has no current workspace
    so the caller can respond with a structured JSON 404 instead of a redirect.

    Parameters
    ----------
    table_id : UUID
        Primary key of the requested ``DataTable``.
    user : User
        Authenticated user whose ``current_workspace`` attribute is checked.

    Returns
    -------
    DataTable | None
    """
    workspace = getattr(user, 'current_workspace', None)
    if not workspace:
        return None
    return get_object_or_404(DataTable, id=table_id, workspace=workspace, is_active=True)


def _records_to_rows(table):
    """Yield one ``dict`` per active record in schema column order.

    Two meta-columns — ``_id`` and ``_created_at`` — are appended after
    the schema-defined columns so they're always at the end regardless of
    the table's field definitions.

    Parameters
    ----------
    table : DataTable
        Source table; only records with ``is_active=True`` are included.

    Yields
    ------
    dict
        Mapping of column name → value for every active record ordered
        by ``created_at`` (oldest first).
    """
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
    """Export all active records of a table in the requested format.

    Dispatches to ``_export_csv``, ``_export_json``, or ``_export_excel``
    depending on the ``format`` query parameter, then writes one
    ``AuditLog`` entry so every export is traceable.

    URL: ``GET /exports/tables/<uuid:table_id>/export/?format=csv|json|excel``

    Parameters
    ----------
    request : HttpRequest
        Must be authenticated.  ``request.user.current_workspace`` is used
        to scope the table lookup.
    table_id : UUID
        Primary key of the target ``DataTable``.

    Query Parameters
    ----------------
    format : str, optional
        One of ``csv`` (default), ``json``, ``excel``, or ``xlsx``.

    Returns
    -------
    HttpResponse
        File attachment with the appropriate MIME type, or a 404 JSON
        response when the workspace is missing or the table is not found.
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
    """Return an ``HttpResponse`` with RFC 4180 CSV content.

    An empty table returns a valid (headerless) response rather than
    raising an error.  ``None`` values are coerced to ``''`` so the CSV
    is always well-formed.

    Parameters
    ----------
    rows : list[dict]
        Pre-built row dicts from ``_records_to_rows``.
    filename : str
        Base filename (without extension) used in ``Content-Disposition``.
    table : DataTable
        Unused directly; reserved for future per-column type formatting.
    """
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
    """Return an ``HttpResponse`` containing a pretty-printed JSON array.

    ``DjangoJSONEncoder`` handles ``UUID``, ``datetime``, and ``Decimal``
    values that the standard ``json`` module cannot serialise.

    Parameters
    ----------
    rows : list[dict]
        Pre-built row dicts from ``_records_to_rows``.
    filename : str
        Base filename (without extension) used in ``Content-Disposition``.
    """
    content = json.dumps(rows, cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)
    response = HttpResponse(content, content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="{filename}.json"'
    return response


def _export_excel(rows, filename, table):
    """Return an ``HttpResponse`` containing an OpenPyXL ``.xlsx`` workbook.

    The first row is styled with a blue header fill (``#4F81BD``) and white
    bold text.  Column widths are auto-fitted to content, capped at 50
    characters to prevent absurdly wide columns.

    Parameters
    ----------
    rows : list[dict]
        Pre-built row dicts from ``_records_to_rows``.
    filename : str
        Base filename (without extension) used in ``Content-Disposition``.
    table : DataTable
        Used for the worksheet name (Excel limit: 31 characters).

    Returns
    -------
    HttpResponse
        XLSX download, or a 500 JSON response if ``openpyxl`` is not installed.
    """
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
