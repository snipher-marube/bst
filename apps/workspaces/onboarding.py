"""
apps/workspaces/onboarding.py
==============================
Onboarding service that seeds a new workspace with realistic sample data
based on the chosen industry, then auto-generates a dashboard.

Usage
-----
    from apps.workspaces.onboarding import OnboardingService
    dashboard = OnboardingService.seed_workspace(workspace, industry='sales', user=request.user)
"""

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sample dataset definitions
# Each entry is a dict with:
#   table_name  – human-readable name shown in the UI
#   description – short blurb for the table description field
#   schema      – list of column defs matching DataTable.FIELD_TYPES
#   records     – list of dicts matching the schema
# ---------------------------------------------------------------------------

INDUSTRY_DATA = {
    'sales': {
        'table_name': 'Sales Pipeline',
        'description': 'Track deals from prospect to close — monitor your revenue pipeline in real time.',
        'schema': [
            {'name': 'customer',    'type': 'text',     'required': True},
            {'name': 'product',     'type': 'text',     'required': True},
            {'name': 'amount',      'type': 'currency', 'required': True},
            {'name': 'stage',       'type': 'text',     'required': True},
            {'name': 'region',      'type': 'text',     'required': False},
            {'name': 'close_date',  'type': 'date',     'required': False},
        ],
        'records': [
            {'customer': 'Safaricom Ltd',       'product': 'Pro Plan',    'amount': 120000, 'stage': 'Closed Won',  'region': 'Nairobi',  'close_date': '2026-03-15'},
            {'customer': 'KCB Group',           'product': 'Enterprise',  'amount': 350000, 'stage': 'Negotiation', 'region': 'Nairobi',  'close_date': '2026-04-30'},
            {'customer': 'Equity Bank',         'product': 'Pro Plan',    'amount': 180000, 'stage': 'Proposal',   'region': 'Nairobi',  'close_date': '2026-05-10'},
            {'customer': 'Bamburi Cement',      'product': 'Starter',     'amount': 45000,  'stage': 'Closed Won', 'region': 'Mombasa',  'close_date': '2026-02-28'},
            {'customer': 'Kenya Airways',       'product': 'Enterprise',  'amount': 500000, 'stage': 'Discovery',  'region': 'Nairobi',  'close_date': '2026-06-01'},
            {'customer': 'NCBA Bank',           'product': 'Pro Plan',    'amount': 200000, 'stage': 'Closed Won', 'region': 'Nairobi',  'close_date': '2026-03-01'},
            {'customer': 'Unga Group',          'product': 'Starter',     'amount': 60000,  'stage': 'Proposal',   'region': 'Nakuru',   'close_date': '2026-04-15'},
            {'customer': 'Nation Media',        'product': 'Pro Plan',    'amount': 95000,  'stage': 'Negotiation','region': 'Nairobi',  'close_date': '2026-04-20'},
            {'customer': 'Britam Holdings',     'product': 'Enterprise',  'amount': 420000, 'stage': 'Closed Won', 'region': 'Nairobi',  'close_date': '2026-01-31'},
            {'customer': 'East African Brew',   'product': 'Starter',     'amount': 38000,  'stage': 'Discovery',  'region': 'Kisumu',   'close_date': '2026-05-05'},
            {'customer': 'Stanbic Bank',        'product': 'Pro Plan',    'amount': 160000, 'stage': 'Closed Won', 'region': 'Nairobi',  'close_date': '2026-03-20'},
            {'customer': 'Total Energies KE',   'product': 'Starter',     'amount': 52000,  'stage': 'Proposal',   'region': 'Mombasa',  'close_date': '2026-04-25'},
            {'customer': 'CFC Stanbic',         'product': 'Enterprise',  'amount': 310000, 'stage': 'Negotiation','region': 'Nairobi',  'close_date': '2026-05-15'},
            {'customer': 'Naivas Supermarket',  'product': 'Pro Plan',    'amount': 140000, 'stage': 'Closed Won', 'region': 'Nairobi',  'close_date': '2026-02-14'},
            {'customer': 'Jamii Telecom',       'product': 'Starter',     'amount': 41000,  'stage': 'Discovery',  'region': 'Eldoret',  'close_date': '2026-06-10'},
        ],
    },

    'finance': {
        'table_name': 'Monthly Budget Tracker',
        'description': 'Compare budgeted vs actual spend per category — identify over/under-spend at a glance.',
        'schema': [
            {'name': 'category',   'type': 'text',     'required': True},
            {'name': 'month',      'type': 'text',     'required': True},
            {'name': 'budgeted',   'type': 'currency', 'required': True},
            {'name': 'actual',     'type': 'currency', 'required': True},
            {'name': 'variance',   'type': 'currency', 'required': False},
            {'name': 'approved',   'type': 'boolean',  'required': False},
        ],
        'records': [
            {'category': 'Salaries',      'month': 'January',  'budgeted': 850000, 'actual': 855000, 'variance': -5000,  'approved': 'true'},
            {'category': 'Marketing',     'month': 'January',  'budgeted': 120000, 'actual': 98000,  'variance': 22000,  'approved': 'true'},
            {'category': 'IT & Software', 'month': 'January',  'budgeted': 60000,  'actual': 71000,  'variance': -11000, 'approved': 'false'},
            {'category': 'Operations',    'month': 'January',  'budgeted': 200000, 'actual': 195000, 'variance': 5000,   'approved': 'true'},
            {'category': 'Salaries',      'month': 'February', 'budgeted': 850000, 'actual': 850000, 'variance': 0,      'approved': 'true'},
            {'category': 'Marketing',     'month': 'February', 'budgeted': 120000, 'actual': 134000, 'variance': -14000, 'approved': 'false'},
            {'category': 'IT & Software', 'month': 'February', 'budgeted': 60000,  'actual': 58000,  'variance': 2000,   'approved': 'true'},
            {'category': 'Operations',    'month': 'February', 'budgeted': 200000, 'actual': 210000, 'variance': -10000, 'approved': 'false'},
            {'category': 'Salaries',      'month': 'March',    'budgeted': 870000, 'actual': 870000, 'variance': 0,      'approved': 'true'},
            {'category': 'Marketing',     'month': 'March',    'budgeted': 150000, 'actual': 142000, 'variance': 8000,   'approved': 'true'},
            {'category': 'IT & Software', 'month': 'March',    'budgeted': 60000,  'actual': 65000,  'variance': -5000,  'approved': 'true'},
            {'category': 'Operations',    'month': 'March',    'budgeted': 200000, 'actual': 188000, 'variance': 12000,  'approved': 'true'},
        ],
    },

    'hr': {
        'table_name': 'Employee Directory',
        'description': 'Central record of all staff — track headcount, departments, and salary ranges.',
        'schema': [
            {'name': 'full_name',   'type': 'text',     'required': True},
            {'name': 'department',  'type': 'text',     'required': True},
            {'name': 'role',        'type': 'text',     'required': True},
            {'name': 'salary',      'type': 'currency', 'required': False},
            {'name': 'hire_date',   'type': 'date',     'required': False},
            {'name': 'status',      'type': 'text',     'required': False},
        ],
        'records': [
            {'full_name': 'Amina Odhiambo',   'department': 'Engineering',  'role': 'Software Engineer',   'salary': 180000, 'hire_date': '2022-03-01', 'status': 'Active'},
            {'full_name': 'Brian Mwangi',      'department': 'Sales',        'role': 'Account Executive',   'salary': 120000, 'hire_date': '2021-07-15', 'status': 'Active'},
            {'full_name': 'Catherine Njeri',   'department': 'Finance',      'role': 'Financial Analyst',   'salary': 140000, 'hire_date': '2020-11-01', 'status': 'Active'},
            {'full_name': 'David Otieno',      'department': 'Engineering',  'role': 'DevOps Engineer',     'salary': 200000, 'hire_date': '2023-01-10', 'status': 'Active'},
            {'full_name': 'Esther Wambui',     'department': 'Marketing',    'role': 'Marketing Manager',   'salary': 160000, 'hire_date': '2019-05-20', 'status': 'Active'},
            {'full_name': 'Francis Kamau',     'department': 'HR',           'role': 'HR Generalist',       'salary': 110000, 'hire_date': '2022-09-01', 'status': 'Active'},
            {'full_name': 'Grace Akinyi',      'department': 'Sales',        'role': 'Sales Manager',       'salary': 190000, 'hire_date': '2018-02-14', 'status': 'Active'},
            {'full_name': 'Hassan Abdi',       'department': 'Engineering',  'role': 'Frontend Developer',  'salary': 165000, 'hire_date': '2023-06-05', 'status': 'Active'},
            {'full_name': 'Irene Cherop',      'department': 'Finance',      'role': 'Accountant',          'salary': 125000, 'hire_date': '2021-03-22', 'status': 'Active'},
            {'full_name': 'James Kariuki',     'department': 'Operations',   'role': 'Operations Lead',     'salary': 175000, 'hire_date': '2020-08-01', 'status': 'Active'},
            {'full_name': 'Lydia Mutua',       'department': 'Marketing',    'role': 'Content Strategist',  'salary': 105000, 'hire_date': '2024-01-15', 'status': 'Probation'},
            {'full_name': 'Michael Koech',     'department': 'Engineering',  'role': 'Backend Developer',   'salary': 170000, 'hire_date': '2022-11-07', 'status': 'Active'},
        ],
    },

    'operations': {
        'table_name': 'Project Tracker',
        'description': 'Monitor all active projects — status, owners, deadlines, and completion progress.',
        'schema': [
            {'name': 'project',     'type': 'text',       'required': True},
            {'name': 'owner',       'type': 'text',       'required': True},
            {'name': 'status',      'type': 'text',       'required': True},
            {'name': 'priority',    'type': 'text',       'required': False},
            {'name': 'completion',  'type': 'percentage', 'required': False},
            {'name': 'deadline',    'type': 'date',       'required': False},
        ],
        'records': [
            {'project': 'Website Redesign',        'owner': 'Amina O.',    'status': 'In Progress',  'priority': 'High',   'completion': 65,  'deadline': '2026-05-01'},
            {'project': 'CRM Integration',         'owner': 'Brian M.',    'status': 'In Progress',  'priority': 'High',   'completion': 40,  'deadline': '2026-04-30'},
            {'project': 'Staff Training Program',  'owner': 'Francis K.',  'status': 'Completed',    'priority': 'Medium', 'completion': 100, 'deadline': '2026-03-31'},
            {'project': 'Q2 Marketing Campaign',   'owner': 'Esther W.',   'status': 'In Progress',  'priority': 'High',   'completion': 55,  'deadline': '2026-06-30'},
            {'project': 'Office Expansion',        'owner': 'James K.',    'status': 'Planning',     'priority': 'Low',    'completion': 10,  'deadline': '2026-09-01'},
            {'project': 'Mobile App MVP',          'owner': 'Hassan A.',   'status': 'In Progress',  'priority': 'High',   'completion': 30,  'deadline': '2026-07-15'},
            {'project': 'Financial Audit',         'owner': 'Catherine N.','status': 'Completed',    'priority': 'High',   'completion': 100, 'deadline': '2026-03-15'},
            {'project': 'Customer Portal',         'owner': 'Amina O.',    'status': 'Planning',     'priority': 'Medium', 'completion': 5,   'deadline': '2026-08-01'},
            {'project': 'Data Migration',          'owner': 'David O.',    'status': 'In Progress',  'priority': 'High',   'completion': 80,  'deadline': '2026-04-20'},
            {'project': 'Brand Refresh',           'owner': 'Lydia M.',    'status': 'Planning',     'priority': 'Low',    'completion': 15,  'deadline': '2026-10-01'},
        ],
    },

    'other': {
        'table_name': 'Business Overview',
        'description': 'A general-purpose tracker for your key business metrics and activities.',
        'schema': [
            {'name': 'item',        'type': 'text',     'required': True},
            {'name': 'category',    'type': 'text',     'required': True},
            {'name': 'value',       'type': 'number',   'required': False},
            {'name': 'status',      'type': 'text',     'required': False},
            {'name': 'date',        'type': 'date',     'required': False},
            {'name': 'notes',       'type': 'text',     'required': False},
        ],
        'records': [
            {'item': 'Revenue Target Q1',    'category': 'Finance',    'value': 500000, 'status': 'Achieved',    'date': '2026-03-31', 'notes': 'Exceeded by 8%'},
            {'item': 'New Customers Q1',     'category': 'Sales',      'value': 42,     'status': 'Achieved',    'date': '2026-03-31', 'notes': 'Target was 40'},
            {'item': 'Website Visitors',     'category': 'Marketing',  'value': 12500,  'status': 'On Track',    'date': '2026-03-31', 'notes': 'Monthly average'},
            {'item': 'Support Tickets',      'category': 'Operations', 'value': 135,    'status': 'Review',      'date': '2026-03-31', 'notes': 'Above 120 threshold'},
            {'item': 'Revenue Target Q2',    'category': 'Finance',    'value': 600000, 'status': 'In Progress', 'date': '2026-06-30', 'notes': 'Tracking at 45%'},
            {'item': 'Headcount Growth',     'category': 'HR',         'value': 5,      'status': 'In Progress', 'date': '2026-06-30', 'notes': '2 of 5 hires done'},
            {'item': 'NPS Score',            'category': 'Operations', 'value': 62,     'status': 'Good',        'date': '2026-03-15', 'notes': 'Up from 58 last quarter'},
            {'item': 'Churn Rate',           'category': 'Sales',      'value': 3,      'status': 'Good',        'date': '2026-03-31', 'notes': 'Target is under 5%'},
            {'item': 'Cost Per Acquisition', 'category': 'Marketing',  'value': 1850,   'status': 'Review',      'date': '2026-03-31', 'notes': 'Up from KES 1,600'},
            {'item': 'Team Satisfaction',    'category': 'HR',         'value': 78,     'status': 'Good',        'date': '2026-03-01', 'notes': 'Out of 100'},
        ],
    },

    'ecommerce': {
        'table_name': 'E-commerce Orders',
        'description': 'Track orders, revenue, and product performance across your online store.',
        'schema': [
            {'name': 'order_id',    'type': 'text',     'required': True},
            {'name': 'product',     'type': 'text',     'required': True},
            {'name': 'category',    'type': 'text',     'required': True},
            {'name': 'quantity',    'type': 'number',   'required': True},
            {'name': 'unit_price',  'type': 'currency', 'required': True},
            {'name': 'revenue',     'type': 'currency', 'required': True},
            {'name': 'status',      'type': 'text',     'required': True},
            {'name': 'order_date',  'type': 'date',     'required': False},
        ],
        'records': [
            {'order_id': 'ORD-001', 'product': 'Wireless Earbuds',    'category': 'Electronics', 'quantity': 2,  'unit_price': 4500,  'revenue': 9000,  'status': 'Delivered',  'order_date': '2026-03-01'},
            {'order_id': 'ORD-002', 'product': 'Running Shoes',       'category': 'Fashion',     'quantity': 1,  'unit_price': 8200,  'revenue': 8200,  'status': 'Delivered',  'order_date': '2026-03-03'},
            {'order_id': 'ORD-003', 'product': 'Blender Pro',         'category': 'Home',        'quantity': 1,  'unit_price': 6800,  'revenue': 6800,  'status': 'Shipped',    'order_date': '2026-03-05'},
            {'order_id': 'ORD-004', 'product': 'Yoga Mat',            'category': 'Sports',      'quantity': 3,  'unit_price': 1500,  'revenue': 4500,  'status': 'Delivered',  'order_date': '2026-03-06'},
            {'order_id': 'ORD-005', 'product': 'Smart Watch',         'category': 'Electronics', 'quantity': 1,  'unit_price': 18000, 'revenue': 18000, 'status': 'Processing', 'order_date': '2026-03-10'},
            {'order_id': 'ORD-006', 'product': 'Coffee Maker',        'category': 'Home',        'quantity': 2,  'unit_price': 5200,  'revenue': 10400, 'status': 'Delivered',  'order_date': '2026-03-12'},
            {'order_id': 'ORD-007', 'product': 'Leather Handbag',     'category': 'Fashion',     'quantity': 1,  'unit_price': 12000, 'revenue': 12000, 'status': 'Returned',   'order_date': '2026-03-14'},
            {'order_id': 'ORD-008', 'product': 'Protein Powder',      'category': 'Sports',      'quantity': 4,  'unit_price': 3200,  'revenue': 12800, 'status': 'Delivered',  'order_date': '2026-03-15'},
            {'order_id': 'ORD-009', 'product': 'Laptop Stand',        'category': 'Electronics', 'quantity': 2,  'unit_price': 2800,  'revenue': 5600,  'status': 'Shipped',    'order_date': '2026-03-18'},
            {'order_id': 'ORD-010', 'product': 'Sunglasses',          'category': 'Fashion',     'quantity': 1,  'unit_price': 3500,  'revenue': 3500,  'status': 'Delivered',  'order_date': '2026-03-20'},
            {'order_id': 'ORD-011', 'product': 'Air Fryer',           'category': 'Home',        'quantity': 1,  'unit_price': 9500,  'revenue': 9500,  'status': 'Delivered',  'order_date': '2026-03-22'},
            {'order_id': 'ORD-012', 'product': 'Resistance Bands',    'category': 'Sports',      'quantity': 5,  'unit_price': 800,   'revenue': 4000,  'status': 'Processing', 'order_date': '2026-03-25'},
            {'order_id': 'ORD-013', 'product': 'Noise-Cancel Headset','category': 'Electronics', 'quantity': 1,  'unit_price': 14500, 'revenue': 14500, 'status': 'Delivered',  'order_date': '2026-03-27'},
            {'order_id': 'ORD-014', 'product': 'Desk Lamp',           'category': 'Home',        'quantity': 3,  'unit_price': 2100,  'revenue': 6300,  'status': 'Delivered',  'order_date': '2026-03-29'},
            {'order_id': 'ORD-015', 'product': 'Sneakers Pro',        'category': 'Fashion',     'quantity': 2,  'unit_price': 7800,  'revenue': 15600, 'status': 'Shipped',    'order_date': '2026-03-31'},
        ],
    },

    'saas': {
        'table_name': 'SaaS Metrics',
        'description': 'Core SaaS KPIs — MRR, churn, CAC, LTV, and cohort growth in one place.',
        'schema': [
            {'name': 'month',       'type': 'text',     'required': True},
            {'name': 'mrr',         'type': 'currency', 'required': True},
            {'name': 'new_mrr',     'type': 'currency', 'required': True},
            {'name': 'churned_mrr', 'type': 'currency', 'required': True},
            {'name': 'customers',   'type': 'number',   'required': True},
            {'name': 'new_signups', 'type': 'number',   'required': True},
            {'name': 'churn_rate',  'type': 'percentage','required': True},
            {'name': 'cac',         'type': 'currency', 'required': False},
            {'name': 'ltv',         'type': 'currency', 'required': False},
        ],
        'records': [
            {'month': 'Oct 2025', 'mrr': 180000,  'new_mrr': 22000,  'churned_mrr': 8000,  'customers': 72,  'new_signups': 9,  'churn_rate': 4.4, 'cac': 12000, 'ltv': 68000},
            {'month': 'Nov 2025', 'mrr': 194000,  'new_mrr': 25000,  'churned_mrr': 11000, 'customers': 78,  'new_signups': 11, 'churn_rate': 5.7, 'cac': 11500, 'ltv': 70000},
            {'month': 'Dec 2025', 'mrr': 208000,  'new_mrr': 28000,  'churned_mrr': 14000, 'customers': 83,  'new_signups': 12, 'churn_rate': 7.2, 'cac': 13000, 'ltv': 72000},
            {'month': 'Jan 2026', 'mrr': 225000,  'new_mrr': 34000,  'churned_mrr': 17000, 'customers': 90,  'new_signups': 14, 'churn_rate': 8.2, 'cac': 12500, 'ltv': 75000},
            {'month': 'Feb 2026', 'mrr': 247000,  'new_mrr': 38000,  'churned_mrr': 16000, 'customers': 99,  'new_signups': 16, 'churn_rate': 6.5, 'cac': 11800, 'ltv': 78000},
            {'month': 'Mar 2026', 'mrr': 268000,  'new_mrr': 41000,  'churned_mrr': 20000, 'customers': 107, 'new_signups': 18, 'churn_rate': 7.5, 'cac': 12200, 'ltv': 80000},
            {'month': 'Apr 2026', 'mrr': 289000,  'new_mrr': 45000,  'churned_mrr': 24000, 'customers': 116, 'new_signups': 20, 'churn_rate': 8.3, 'cac': 11000, 'ltv': 82000},
        ],
    },

    'marketing': {
        'table_name': 'Marketing Funnel',
        'description': 'Track leads through every funnel stage — from awareness to closed deal.',
        'schema': [
            {'name': 'channel',     'type': 'text',     'required': True},
            {'name': 'stage',       'type': 'text',     'required': True},
            {'name': 'leads',       'type': 'number',   'required': True},
            {'name': 'conversions', 'type': 'number',   'required': True},
            {'name': 'conv_rate',   'type': 'percentage','required': True},
            {'name': 'spend',       'type': 'currency', 'required': False},
            {'name': 'revenue',     'type': 'currency', 'required': False},
            {'name': 'month',       'type': 'text',     'required': False},
        ],
        'records': [
            {'channel': 'Google Ads',    'stage': 'Awareness',     'leads': 4200, 'conversions': 840,  'conv_rate': 20.0, 'spend': 85000,  'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Google Ads',    'stage': 'Consideration', 'leads': 840,  'conversions': 252,  'conv_rate': 30.0, 'spend': 0,      'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Google Ads',    'stage': 'Decision',      'leads': 252,  'conversions': 63,   'conv_rate': 25.0, 'spend': 0,      'revenue': 189000, 'month': 'Q1 2026'},
            {'channel': 'LinkedIn',      'stage': 'Awareness',     'leads': 1800, 'conversions': 540,  'conv_rate': 30.0, 'spend': 60000,  'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'LinkedIn',      'stage': 'Consideration', 'leads': 540,  'conversions': 216,  'conv_rate': 40.0, 'spend': 0,      'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'LinkedIn',      'stage': 'Decision',      'leads': 216,  'conversions': 65,   'conv_rate': 30.0, 'spend': 0,      'revenue': 325000, 'month': 'Q1 2026'},
            {'channel': 'Organic SEO',   'stage': 'Awareness',     'leads': 6500, 'conversions': 1300, 'conv_rate': 20.0, 'spend': 15000,  'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Organic SEO',   'stage': 'Consideration', 'leads': 1300, 'conversions': 390,  'conv_rate': 30.0, 'spend': 0,      'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Organic SEO',   'stage': 'Decision',      'leads': 390,  'conversions': 78,   'conv_rate': 20.0, 'spend': 0,      'revenue': 234000, 'month': 'Q1 2026'},
            {'channel': 'Email',         'stage': 'Awareness',     'leads': 2200, 'conversions': 660,  'conv_rate': 30.0, 'spend': 8000,   'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Email',         'stage': 'Consideration', 'leads': 660,  'conversions': 264,  'conv_rate': 40.0, 'spend': 0,      'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Email',         'stage': 'Decision',      'leads': 264,  'conversions': 79,   'conv_rate': 30.0, 'spend': 0,      'revenue': 316000, 'month': 'Q1 2026'},
            {'channel': 'WhatsApp',      'stage': 'Awareness',     'leads': 950,  'conversions': 380,  'conv_rate': 40.0, 'spend': 5000,   'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'WhatsApp',      'stage': 'Decision',      'leads': 380,  'conversions': 114,  'conv_rate': 30.0, 'spend': 0,      'revenue': 228000, 'month': 'Q1 2026'},
            {'channel': 'Referral',      'stage': 'Awareness',     'leads': 700,  'conversions': 350,  'conv_rate': 50.0, 'spend': 0,      'revenue': 0,      'month': 'Q1 2026'},
            {'channel': 'Referral',      'stage': 'Decision',      'leads': 350,  'conversions': 140,  'conv_rate': 40.0, 'spend': 0,      'revenue': 560000, 'month': 'Q1 2026'},
        ],
    },
}

INDUSTRY_LABELS = {
    'sales':      'Sales & CRM',
    'finance':    'Finance & Budgeting',
    'hr':         'Human Resources',
    'operations': 'Operations & Projects',
    'ecommerce':  'E-commerce',
    'saas':       'SaaS Metrics',
    'marketing':  'Marketing Funnel',
    'other':      'Other / General',
}

TEMPLATE_META = {
    'sales':      {'icon': 'fa-handshake',     'color': 'bg-blue-100 text-blue-600',   'desc': 'Deals, pipeline stages, and revenue forecasting.'},
    'finance':    {'icon': 'fa-chart-line',    'color': 'bg-green-100 text-green-600', 'desc': 'Budget vs actuals, variance tracking by category.'},
    'hr':         {'icon': 'fa-users',         'color': 'bg-purple-100 text-purple-600','desc': 'Employee directory, departments, and headcount.'},
    'operations': {'icon': 'fa-tasks',         'color': 'bg-amber-100 text-amber-600', 'desc': 'Project tracker with status, owners, and deadlines.'},
    'ecommerce':  {'icon': 'fa-shopping-cart', 'color': 'bg-pink-100 text-pink-600',   'desc': 'Orders, products, categories, and fulfilment status.'},
    'saas':       {'icon': 'fa-rocket',        'color': 'bg-indigo-100 text-indigo-600','desc': 'MRR, churn, CAC, LTV, and monthly cohort growth.'},
    'marketing':  {'icon': 'fa-funnel-dollar', 'color': 'bg-orange-100 text-orange-600','desc': 'Leads, conversions, and spend across every channel.'},
    'other':      {'icon': 'fa-th-large',      'color': 'bg-gray-100 text-gray-600',   'desc': 'General-purpose metrics and KPI tracker.'},
}


class OnboardingService:
    """Seeds a workspace with sample data and auto-generates a dashboard."""

    @staticmethod
    def seed_workspace(workspace, industry: str, user):
        """
        Create sample DataTable + Records for the chosen industry, then
        auto-generate a dashboard via DataTable.generate_default_dashboard().

        Returns the created Dashboard instance.
        """
        from apps.dashboards.models import DataTable, Record

        industry = industry.lower().strip()
        if industry not in INDUSTRY_DATA:
            industry = 'other'

        dataset = INDUSTRY_DATA[industry]

        # Save industry on workspace
        workspace.industry = industry
        workspace.save(update_fields=['industry'])

        # Create the DataTable
        table, created = DataTable.objects.get_or_create(
            workspace=workspace,
            name=dataset['table_name'],
            defaults={
                'description': dataset['description'],
                'schema': dataset['schema'],
                'created_by': user,
            }
        )

        if created:
            # Bulk-create records
            record_objs = [
                Record(
                    table=table,
                    data=row,
                    created_by=user,
                )
                for row in dataset['records']
            ]
            Record.objects.bulk_create(record_objs)

            # Update denormalized count
            table.record_count = len(dataset['records'])
            table.save(update_fields=['record_count'])

            logger.info(
                "Onboarding: seeded '%s' with %d records for workspace %s",
                table.name, len(dataset['records']), workspace.id,
            )

        # Auto-generate dashboard (safe to call even if table already existed)
        from apps.dashboards.models import Dashboard
        existing = Dashboard.objects.filter(
            workspace=workspace,
            name=f"{table.name} Dashboard",
            is_active=True,
        ).first()

        if existing:
            return existing

        dashboard = table.generate_default_dashboard()
        return dashboard
