from django.urls import include, path

from catalog.api.grid import feed_runs_page

urlpatterns = [path("api/", include("catalog.api.urls")), path("runs/", feed_runs_page, name="feed-runs-page")]
