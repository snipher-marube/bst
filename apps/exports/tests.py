import json
from django.test import TestCase, Client as DjangoClient

from apps.dashboards.factories import UserFactory, WorkspaceFactory, DataTableWithSchemaFactory
from apps.dashboards.models import Record


class TestExportTableView(TestCase):
    """Tests for the export_table view (CSV, JSON, Excel formats)."""

    def setUp(self):
        self.client = DjangoClient()
        self.user = UserFactory()
        self.client.force_login(self.user)
        self.workspace = WorkspaceFactory(owner=self.user)
        session = self.client.session
        session['current_workspace_id'] = str(self.workspace.id)
        session.save()
        self.user.current_workspace = self.workspace

        # Table with a simple schema
        self.table = DataTableWithSchemaFactory(workspace=self.workspace)
        # Create two records matching the DataTableWithSchemaFactory schema
        # (customer, amount, region, closed)
        Record.objects.create(
            table=self.table,
            data={'customer': 'Alice', 'amount': 100, 'region': 'North', 'closed': True},
        )
        Record.objects.create(
            table=self.table,
            data={'customer': 'Bob', 'amount': 200, 'region': 'South', 'closed': False},
        )

    def _url(self, fmt=None):
        url = f'/exports/tables/{self.table.id}/export/'
        if fmt:
            url += f'?format={fmt}'
        return url

    # ── Auth guard ────────────────────────────────────────────────────────────

    def test_unauthenticated_redirects(self):
        resp = DjangoClient().get(self._url())
        self.assertEqual(resp.status_code, 302)

    # ── CSV (default) ─────────────────────────────────────────────────────────

    def test_csv_export_default_format(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        self.assertIn('attachment', resp['Content-Disposition'])
        self.assertIn('.csv', resp['Content-Disposition'])

    def test_csv_export_explicit_format(self):
        resp = self.client.get(self._url('csv'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('Alice', content)
        self.assertIn('Bob', content)
        self.assertIn('customer', content)

    def test_csv_export_empty_table(self):
        """Exporting a table with no records returns an empty CSV (no error)."""
        Record.objects.filter(table=self.table).delete()
        resp = self.client.get(self._url('csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')

    # ── JSON ──────────────────────────────────────────────────────────────────

    def test_json_export(self):
        resp = self.client.get(self._url('json'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')
        data = json.loads(resp.content)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        names = {row['customer'] for row in data}
        self.assertEqual(names, {'Alice', 'Bob'})

    def test_json_export_includes_meta_fields(self):
        resp = self.client.get(self._url('json'))
        rows = json.loads(resp.content)
        self.assertIn('_id', rows[0])
        self.assertIn('_created_at', rows[0])

    # ── Excel ─────────────────────────────────────────────────────────────────

    def test_excel_export(self):
        resp = self.client.get(self._url('excel'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            'spreadsheetml.sheet',
            resp['Content-Type'],
        )
        self.assertIn('.xlsx', resp['Content-Disposition'])

    def test_excel_export_empty_table(self):
        Record.objects.filter(table=self.table).delete()
        resp = self.client.get(self._url('xlsx'))
        self.assertEqual(resp.status_code, 200)

    # ── No-workspace guard ───────────────────────────────────────────────────

    def test_no_workspace_returns_404(self):
        """If current_workspace is not set the view returns 404 JSON."""
        # Create a user with no workspace
        other_user = UserFactory()
        c = DjangoClient()
        c.force_login(other_user)
        resp = c.get(self._url())
        self.assertIn(resp.status_code, [302, 404])

    # ── Wrong workspace ───────────────────────────────────────────────────────

    def test_other_workspace_table_returns_404(self):
        """A table in a different workspace is not accessible."""
        other_ws = WorkspaceFactory(owner=self.user)
        other_table = DataTableWithSchemaFactory(workspace=other_ws)
        resp = self.client.get(f'/exports/tables/{other_table.id}/export/')
        self.assertEqual(resp.status_code, 404)
