from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("feed-runs", views.FeedRunViewSet, basename="feed-run")

urlpatterns = [
    path("parts/search/", views.PartSearchView.as_view(), name="part-search"),
    path("parts/<int:pk>/", views.PartDetailView.as_view(), name="part-detail"),
    path("fitment/", views.FitmentView.as_view(), name="fitment"),
    path("review/", views.ReviewQueueView.as_view(), name="review"),
    *router.urls,
]
