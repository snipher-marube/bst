from django.urls import path
from . import views

app_name = 'reports'

urlpatterns = [
    path('dashboard/<uuid:dashboard_id>/generate/',
         views.GenerateReportView.as_view(), name='generate'),
    path('job/<uuid:job_id>/status/',
         views.ReportJobStatusView.as_view(), name='status'),
    path('job/<uuid:job_id>/download/',
         views.DownloadReportView.as_view(), name='download'),
    path('share/<uuid:share_token>/',
         views.ShareReportView.as_view(), name='share'),
]
