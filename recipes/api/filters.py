from functools import reduce
from django.contrib.postgres.search import SearchRank, SearchQuery
from django.db.models import F
from rest_framework.filters import SearchFilter


class SearchVectorFilter(SearchFilter):
    """
    Sub-classing `SearchFilter` to enable full-text search capabilities of postgres when a search vector is defined.

    For example, if there is a SearchVectorField defined as "search_vector" then this search filter
    will directly filter the vector like the following:

        Recipe.objects.filter(search_vector='cheeses')

    Results are ordered by search rank.
    """
    search_vector_field_name = 'search_vector'

    def filter_queryset(self, request, queryset, view):
        queryset = super().filter_queryset(request, queryset, view)
        # build combined search queries
        search_queries = [SearchQuery(term) for term in self.get_search_terms(request)]
        if search_queries:
            search_query = reduce(lambda x, y: x & y, search_queries)
            # include and order by search rank
            queryset = queryset.model.objects.annotate(search_rank=SearchRank(F('search_vector'), search_query)).filter(search_vector=search_query)
            queryset = queryset.order_by('-search_rank')
            queryset = queryset.order_by(F('image_path').asc(nulls_last=True))
        return queryset

    def construct_search(self, field_name):
        if field_name == self.search_vector_field_name:
            return field_name
        return super().construct_search(field_name)
