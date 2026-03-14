# apps/insights/generators/sales_insights.py
from datetime import datetime, timedelta
from django.db.models import Count, Sum, Avg
from apps.dashboards.models import Record, DataTable
import json
import logging

logger = logging.getLogger(__name__)

class SalesInsightGenerator:
    """
    Generates intelligent insights specifically for sales pipeline data
    """
    
    def __init__(self, workspace):
        self.workspace = workspace
    
    def analyze_sales_pipeline(self, table):
        """
        Analyze a sales pipeline table and generate insights
        """
        schema = {f['name']: f['type'] for f in table.schema}
        
        # Check if this looks like a sales pipeline
        if 'Deal Value' not in schema or 'Status' not in schema:
            return None
        
        insights = []
        
        # 1. Pipeline Value by Stage
        stage_data = self._get_pipeline_by_stage(table)
        if stage_data:
            insights.append({
                'type': 'bar_chart',
                'title': 'Pipeline Value by Stage',
                'description': 'Total deal value in each pipeline stage',
                'data': stage_data,
                'priority': 5,
                'viz_config': {
                    'x_axis': 'stage',
                    'y_axis': 'value',
                    'colors': ['#03466e']
                }
            })
        
        # 2. Win Rate Analysis
        win_rate = self._calculate_win_rate(table)
        if win_rate:
            insights.append({
                'type': 'metric',
                'title': 'Win Rate',
                'description': 'Percentage of deals won',
                'data': {'val': win_rate},
                'priority': 5,
                'viz_config': {
                    'suffix': '%',
                    'format': 'percentage'
                }
            })
        
        # 3. Average Deal Size
        avg_deal = self._average_deal_size(table)
        if avg_deal:
            insights.append({
                'type': 'metric',
                'title': 'Average Deal Size',
                'description': 'Average value per deal',
                'data': {'val': avg_deal},
                'priority': 4,
                'viz_config': {
                    'prefix': '$',
                    'format': 'currency'
                }
            })
        
        # 4. Sales Trend Over Time
        trend_data = self._sales_trend(table)
        if trend_data:
            insights.append({
                'type': 'line_chart',
                'title': 'Sales Trend',
                'description': 'Deal value over time',
                'data': trend_data,
                'priority': 5,
                'viz_config': {
                    'x_axis': 'date',
                    'y_axis': 'value'
                }
            })
        
        # 5. Top Deals
        top_deals = self._top_deals(table)
        if top_deals:
            insights.append({
                'type': 'table',
                'title': 'Top Deals',
                'description': 'Largest deals in pipeline',
                'data': top_deals,
                'priority': 3
            })
        
        # 6. AI-Powered Prediction
        prediction = self._predict_closed_deals(table)
        if prediction:
            insights.append({
                'type': 'metric',
                'title': 'Projected Monthly Revenue',
                'description': 'AI-powered prediction for next 30 days',
                'data': {'val': prediction, 'trend': prediction['trend']},
                'priority': 5,
                'viz_config': {
                    'prefix': '$',
                    'format': 'currency'
                }
            })
        
        return insights
    
    def _get_pipeline_by_stage(self, table):
        """Get total value in each pipeline stage"""
        records = Record.objects.filter(table=table, is_active=True)
        
        stage_values = {}
        for record in records:
            data = record.data
            if data and 'Status' in data and 'Deal Value' in data:
                stage = data['Status']
                try:
                    value = float(data['Deal Value'])
                    stage_values[stage] = stage_values.get(stage, 0) + value
                except (ValueError, TypeError):
                    continue
        
        if stage_values:
            return {
                'labels': list(stage_values.keys()),
                'values': list(stage_values.values())
            }
        return None
    
    def _calculate_win_rate(self, table):
        """Calculate win rate percentage"""
        records = Record.objects.filter(table=table, is_active=True)
        
        total = 0
        won = 0
        
        for record in records:
            data = record.data
            if data and 'Status' in data:
                total += 1
                if data['Status'].lower() in ['won', 'closed won', 'closed-won']:
                    won += 1
        
        if total > 0:
            return round((won / total) * 100, 1)
        return None
    
    def _average_deal_size(self, table):
        """Calculate average deal size"""
        records = Record.objects.filter(table=table, is_active=True)
        
        total_value = 0
        count = 0
        
        for record in records:
            data = record.data
            if data and 'Deal Value' in data:
                try:
                    total_value += float(data['Deal Value'])
                    count += 1
                except (ValueError, TypeError):
                    continue
        
        if count > 0:
            return round(total_value / count, 2)
        return None
    
    def _sales_trend(self, table):
        """Get sales trend over time"""
        from collections import defaultdict
        from datetime import datetime
        
        records = Record.objects.filter(table=table, is_active=True).order_by('created_at')
        
        daily_values = defaultdict(float)
        
        for record in records:
            data = record.data
            if data and 'Deal Value' in data:
                try:
                    value = float(data['Deal Value'])
                    date = record.created_at.date()
                    daily_values[date] += value
                except (ValueError, TypeError):
                    continue
        
        if daily_values:
            # Sort by date
            sorted_dates = sorted(daily_values.keys())
            return {
                'labels': [d.strftime('%Y-%m-%d') for d in sorted_dates],
                'values': [daily_values[d] for d in sorted_dates]
            }
        return None
    
    def _top_deals(self, table):
        """Get top 10 deals by value"""
        records = Record.objects.filter(table=table, is_active=True)
        
        deals = []
        for record in records:
            data = record.data
            if data and 'Deal Value' in data and 'Company Name' in data:
                try:
                    value = float(data['Deal Value'])
                    deals.append({
                        'Company': data['Company Name'],
                        'Value': value,
                        'Status': data.get('Status', 'Unknown'),
                        'Close Date': data.get('Close Date', 'N/A')
                    })
                except (ValueError, TypeError):
                    continue
        
        # Sort by value and return top 10
        deals.sort(key=lambda x: x['Value'], reverse=True)
        return deals[:10]
    
    def _predict_closed_deals(self, table):
        """Simple AI prediction for next month's closed deals"""
        from datetime import datetime, timedelta
        from collections import defaultdict
        
        # Get historical closed deals
        records = Record.objects.filter(table=table, is_active=True)
        
        monthly_values = defaultdict(float)
        monthly_counts = defaultdict(int)
        
        for record in records:
            data = record.data
            if data and 'Deal Value' in data and 'Status' in data:
                if data['Status'].lower() in ['won', 'closed won', 'closed-won']:
                    try:
                        value = float(data['Deal Value'])
                        month_key = record.created_at.strftime('%Y-%m')
                        monthly_values[month_key] += value
                        monthly_counts[month_key] += 1
                    except (ValueError, TypeError):
                        continue
        
        if len(monthly_values) < 2:
            return None
        
        # Simple moving average prediction
        months = sorted(monthly_values.keys())
        recent_months = months[-3:]  # Last 3 months
        
        avg_monthly_value = sum(monthly_values[m] for m in recent_months) / len(recent_months)
        avg_deal_count = sum(monthly_counts[m] for m in recent_months) / len(recent_months)
        
        # Calculate trend
        if len(months) >= 3:
            first_avg = sum(monthly_values[m] for m in months[:3]) / 3
            last_avg = sum(monthly_values[m] for m in months[-3:]) / 3
            trend = 'up' if last_avg > first_avg else 'down'
            trend_percent = round(((last_avg - first_avg) / first_avg) * 100, 1)
        else:
            trend = 'stable'
            trend_percent = 0
        
        return {
            'value': round(avg_monthly_value, 2),
            'count': round(avg_deal_count, 1),
            'trend': trend,
            'trend_percent': trend_percent
        }