import csv
import os
import time
from urllib.parse import unquote, urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import transaction

from recipes.models import Recipe
from recipes.management.commands.scrape import Command as ScrapeCommand
from recipes.management.commands.scrape import REQUEST_HEADERS, URL_NYT

REQUEST_TIMEOUT = 20


class Command(BaseCommand):
    help = 'Find and remove duplicate recipes that share the same NYT numeric id'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Delete duplicate rows (default is a dry run)')
        parser.add_argument('--refresh', action='store_true', help='Re-scrape kept recipes after deletion')
        parser.add_argument('--delete-images', action='store_true', help='Delete orphaned image files for removed rows')
        parser.add_argument('--csv', default='/tmp/recipes/recipe_duplicates.csv', help='Write report to CSV file')
        parser.add_argument('--max-groups', type=int, default=0, help='Limit to the first N groups (for testing)')
        parser.add_argument('--delay', type=float, default=0.2, help='Seconds to wait between network requests')

    def handle(self, *args, **options):
        self.apply = options['apply']
        self.refresh = options['refresh']
        self.delete_images = options['delete_images']
        self.delay = options['delay']

        groups = self._duplicate_groups()
        if options['max_groups']:
            groups = groups[:options['max_groups']]

        self.stdout.write(self.style.SUCCESS(f'Found {len(groups)} duplicate groups'))

        report = []
        unresolved = 0
        for group in groups:
            kept, deleted, resolved = self._resolve_group(group)
            if not resolved:
                unresolved += 1
            report.append({
                'recipe_id': group[0]['recipe_id'],
                'kept_slug': kept['slug'],
                'kept_id': kept['id'],
                'deleted_slugs': ', '.join(d['slug'] for d in deleted),
                'deleted_ids': ', '.join(str(d['id']) for d in deleted),
                'kept_date': str(kept['date_added']),
                'deleted_dates': ', '.join(str(d['date_added']) for d in deleted),
                'resolved': resolved,
            })

        self._write_report(report, options['csv'])

        total_deleted = sum(len(r['deleted_ids'].split(', ')) for r in report if r['deleted_ids'])
        self.stdout.write(self.style.SUCCESS(
            f'{total_deleted} rows to delete across {len(report)} groups '
            f'({unresolved} groups unresolved, falling back to newest row)'
        ))

        if not self.apply:
            self.stdout.write(self.style.WARNING('Dry run: no changes made. Re-run with --apply to delete.'))
            return

        delete_ids = [int(did) for r in report for did in r['deleted_ids'].split(', ') if r['deleted_ids']]

        self.stdout.write(self.style.SUCCESS(f'Deleting {len(delete_ids)} rows...'))
        image_paths = []
        with transaction.atomic():
            to_delete = Recipe.objects.filter(id__in=delete_ids)
            if self.delete_images:
                image_paths = list(to_delete.exclude(image_path=None).values_list('image_path', flat=True))
            to_delete.delete()
        if self.delete_images:
            self._delete_images(image_paths)

        if self.refresh:
            kept_slugs = [r['kept_slug'] for r in report]
            self._refresh_kept(kept_slugs)

        cache.clear()
        self.stdout.write(self.style.SUCCESS('Done'))

    def _duplicate_groups(self):
        rows = list(Recipe.objects.filter(slug__regex=r'^[0-9]+-').values(
            'id', 'slug', 'date_added', 'rating_count'
        ))
        groups = {}
        for row in rows:
            rid = row['slug'].split('-', 1)[0]
            row['recipe_id'] = rid
            groups.setdefault(rid, []).append(row)
        return [sorted(group, key=lambda r: r['slug']) for group in groups.values() if len(group) > 1]

    def _resolve_group(self, group):
        slugs = [r['slug'] for r in group]
        canonical = self._resolve_canonical(slugs)
        if canonical:
            kept = next(r for r in group if r['slug'] == canonical)
            deleted = [r for r in group if r['slug'] != canonical]
            return kept, deleted, True
        kept = self._fallback_keep(group)
        deleted = [r for r in group if r['slug'] != kept['slug']]
        return kept, deleted, False

    def _resolve_canonical(self, slugs):
        for slug in slugs:
            url = f'{URL_NYT}/recipes/{slug}'
            try:
                response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
                final = unquote(os.path.basename(urlparse(response.url).path))
                if final in slugs:
                    return final
            except requests.RequestException:
                pass
            time.sleep(self.delay)
        return None

    @staticmethod
    def _fallback_keep(group):
        return max(group, key=lambda r: (r['date_added'], r['rating_count'] or 0, r['slug']))

    def _write_report(self, report, csv_path):
        with open(csv_path, 'w', newline='') as out_file:
            writer = csv.DictWriter(out_file, fieldnames=list(report[0].keys()))
            writer.writeheader()
            for row in report:
                writer.writerow(row)
        self.stdout.write(self.style.SUCCESS(f'Report written to {csv_path}'))

    def _delete_images(self, image_paths):
        recipes_dir = os.path.join(settings.STATIC_ROOT, 'recipes')
        for image_path in image_paths:
            if not image_path:
                continue
            file_path = os.path.join(recipes_dir, os.path.basename(image_path))
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    self.stdout.write(self.style.WARNING(f'Could not remove {file_path}'))

    def _refresh_kept(self, kept_slugs):
        self.stdout.write(self.style.SUCCESS(f'Re-scraping {len(kept_slugs)} kept recipes...'))
        scraper = ScrapeCommand()
        for i, slug in enumerate(kept_slugs):
            try:
                scraper.scrape_specific_recipe(slug)
            except Exception:
                self.stdout.write(self.style.WARNING(f'Failed to refresh {slug}'))
            if i != 0 and i % 50 == 0:
                self.stdout.write(self.style.SUCCESS(f'Refreshed {i} recipes so far'))
            time.sleep(self.delay)
        self.stdout.write(self.style.SUCCESS('Refresh complete'))
