from django.urls import path

from backend.db_services.mysql.dts.views import MySQLDtsMigrateViewSet

urlpatterns = [
    path(
        "dts/migrate/query_migrate_records/",
        MySQLDtsMigrateViewSet.as_view({"get": "query_migrate_records"}),
    ),
    path(
        "dts/migrate/force_failed_migrate/",
        MySQLDtsMigrateViewSet.as_view({"post": "force_failed_migrate"}),
    ),
    path(
        "dts/migrate/preview/",
        MySQLDtsMigrateViewSet.as_view({"post": "preview"}),
    ),
    path(
        "dts/clusters/",
        MySQLDtsMigrateViewSet.as_view({"get": "list_dts_clusters"}),
    ),
]
