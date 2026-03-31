from django.urls import path
from . import views

app_name = 'exports'

urlpatterns = [
    path('tables/<uuid:table_id>/export/', views.export_table, name='export_table'),
]
