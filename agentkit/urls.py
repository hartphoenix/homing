from django.urls import path

from . import views

app_name = "agentkit"

urlpatterns = [
    path("", views.index, name="index"),
    path("pkg/VERSION", views.version, name="version"),
    path("pkg/manifest.json", views.manifest, name="manifest"),
    path("pkg/homing-agent-kit-<int:number>.zip", views.archive, name="archive"),
    path("pkg/SKILL.md", views.skill, name="skill"),
    # ``str`` never matches a slash, so an encoded or literal ``../`` fails to route at all.
    path("pkg/references/<str:name>.md", views.reference, name="reference"),
    path("pkg/scripts/<str:name>", views.script, name="script"),
]
