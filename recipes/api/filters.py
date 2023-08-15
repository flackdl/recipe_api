from functools import reduce

from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.search import SearchRank, SearchQuery
from django.db.models import F
from rest_framework.filters import SearchFilter
from django_filters import rest_framework as filters, ModelMultipleChoiceFilter

from recipe_api.settings import POSTGRES_LANGUAGE_UNACCENT
from recipes.models import Recipe, Category


class RecipeFilter(filters.FilterSet):
    has_image = filters.BooleanFilter(field_name="image_path", lookup_expr='isnull', label='Has Image', exclude=True)
    categories = ModelMultipleChoiceFilter(
        queryset=Category.objects.all(),
        method='filter_categories',
    )

    def filter_categories(self, queryset, name, value):
        # custom filter to guarantee recipes only appear when they
        # include ALL the supplied categories vs when a recipe may only have one overlapping category
        categories = value
        if not categories:
            return queryset
        queryset = queryset.annotate(cats=ArrayAgg('categories__id'))
        queryset = queryset.filter(cats__contains=[c.id for c in categories])
        return queryset

    def filter_queryset(self, queryset):
        # order by existing fields and then rating with nulls last
        return super().filter_queryset(queryset).order_by(
            *queryset.query.order_by,
            F('rating_value').desc(nulls_last=True),
            '-rating_count',
        )

    class Meta:
        model = Recipe
        fields = {
            'name': ['exact'],
            'slug': ['exact'],
            'rating_value': ['gte'],
            'rating_count': ['gte'],
        }


class SearchVectorFilter(SearchFilter):
    """
    Sub-classing `SearchFilter` to enable full-text search capabilities of postgres when a search vector is defined.

    For example, if there is a SearchVectorField defined as "search_vector" then this search filter
    will directly filter the vector like the following:

        Recipe.objects.filter(search_vector='cheeses')

    Results are ordered by search rank.
    """
    search_vector_field_name = 'search_vector'

    # TODO - implement trigram search

    def filter_queryset(self, request, queryset, view):
        queryset = super().filter_queryset(request, queryset, view)
        # build combined search queries
        search_queries = [SearchQuery(term, config=POSTGRES_LANGUAGE_UNACCENT) for term in self.get_search_terms(request)]
        if search_queries:
            search_query = reduce(lambda x, y: x & y, search_queries)
            # include and order by search rank
            queryset = queryset.model.objects.annotate(search_rank=SearchRank(F('search_vector'), search_query))
            queryset = queryset.filter(search_vector=search_query)
            queryset = queryset.order_by('-search_rank')
        return queryset

    def construct_search(self, field_name):
        if field_name == self.search_vector_field_name:
            return field_name
        return super().construct_search(field_name)
