"""One page size rule for every list endpoint: a default of 25 and a ceiling of 100, the same ceiling search has."""
from rest_framework.pagination import LimitOffsetPagination

MAX_LIMIT = 100


class CappedLimitOffsetPagination(LimitOffsetPagination):
    default_limit = 25
    # without a ceiling `?limit=100000` returns every row; with one DRF quietly lowers the limit to this
    max_limit = MAX_LIMIT
