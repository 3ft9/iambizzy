from django.urls import path

from . import views

urlpatterns = [
    path("favicon.ico", views.favicon, name="favicon"),
    path("", views.home, name="home"),
    path("connections/<int:pk>/edit", views.edit_connection, name="edit_connection"),
    path("rules/<int:pk>/labels", views.edit_connection, name="rule_labels"),
    path("calendars/<int:pk>/remove", views.remove_calendar, name="remove_calendar"),
    path("calendars", views.calendars, name="calendars"),
    path("connections/new", views.new_connection, name="new_connection"),
    path("feeds", views.add_feed, name="add_feed"),
    path("rules", views.add_rule, name="add_rule"),
    path("rules/<int:pk>", views.action, name="action"),
    path("oauth/start", views.oauth_start, name="oauth_start"),
    path("oauth/callback", views.oauth_callback, name="oauth_callback"),
    path("calendars/select", views.select_calendars, name="select_calendars"),
]
