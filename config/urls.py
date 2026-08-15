from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from config.views import health_live, health_ready
from tracker.views import TrackerLoginView, TrackerLogoutView


urlpatterns = [
    path("health/live", health_live, name="health-live"),
    path("health/ready", health_ready, name="health-ready"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("api.urls")),
    path("login/", TrackerLoginView.as_view(), name="login"),
    path("logout/", TrackerLogoutView.as_view(), name="logout"),
    path("", RedirectView.as_view(pattern_name="tracker:project-list", permanent=False)),
    path("", include(("tracker.urls", "tracker"), namespace="tracker")),
]
