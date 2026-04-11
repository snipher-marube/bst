from django.contrib import admin
from apps.insights.models import Insight, WorkspaceLLMBudget


@admin.register(Insight)
class InsightAdmin(admin.ModelAdmin):
    list_display  = ('title', 'insight_type', 'workspace', 'llm_model', 'created_at')
    list_filter   = ('insight_type', 'workspace')
    search_fields = ('title', 'description', 'source_table_name')
    readonly_fields = ('id', 'created_at', 'prompt_tokens', 'completion_tokens', 'llm_model')


@admin.register(WorkspaceLLMBudget)
class WorkspaceLLMBudgetAdmin(admin.ModelAdmin):
    list_display  = ('workspace', 'month', 'tokens_used', 'monthly_limit')
    list_filter   = ('workspace',)
    readonly_fields = ('id',)
