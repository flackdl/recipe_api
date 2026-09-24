# AGENTS.md

Django 3.2 + Django REST Framework recipe API. Single app: `recipes`. Settings module is `recipe_api.settings`.

## Hard requirements (easy to get wrong)

- **PostgreSQL only** — the app uses `ArrayField`, `SearchVectorField`, `GinIndex`, and the `unaccent` extension (enabled in `recipes/migrations/0013_*.py`). SQLite will not run. DB is configured via `DATABASE_URL` (`dj-database-url`), default `postgres://postgres:postgres@localhost:5432/postgres`.
- **No test suite and no toolchain** — `recipes/tests.py` is empty, there is no pytest/linter/formatter/typecheck/CI config. Don't go hunting for them. `test_groups.py` at the repo root is a throwaway script with hardcoded `~/Downloads` paths, **not** a runnable test.
- **`DEBUG` is a presence flag** — `DEBUG = 'DEBUG' in os.environ`. Any value (even empty string) enables debug. DB cache (`DatabaseCache` → `cache` table) is configured only when `DEBUG` is unset; in dev (`DEBUG=1`) caching silently falls back to locmem.

## Setup & run (order matters)

```sh
docker compose up -d postgres
pip install -r requirements.txt
python manage.py migrate
python manage.py createcachetable     # creates the cache table prod + scrape need
python manage.py createsuperuser
DEBUG=1 python manage.py runserver   # http://localhost:8000
```

## Data pipeline

Scrape is the data pipeline (`python manage.py scrape ...`). Flags are ordered: **run `--urls` before `--recipes`** — `--recipes` reads `/tmp/recipes/urls.json` and exits if it's missing.

- `scrape --urls` — crawl NYT search pages, write `urls.json`
- `scrape --recipes` — scrape every URL (skips existing rows unless `--force`)
- `scrape --specific-recipe-slug <slug>` — re-scrape one recipe
- Scrape writes images into `STATIC_ROOT` (`staticfiles/recipes/`) and calls `cache.clear()` at the end.

## Front-end / static wiring

- The Vue 2 SPA is a **single prebuilt file**, `static/index.html`, loaded from CDN (Bulma/Buefy). There is no `package.json` / npm build in this repo.
- `views.main` serves `static/index.html` directly (no Django templates); `views.just_the_recipe` handles `^http.*` paths as a redirect into the SPA hash route.
- `static/recipes` is a **symlink** into `staticfiles/recipes/` (the gitignored `collectstatic` output) so `runserver` can serve scraped images.

## Search

Full-text search is **precomputed**, not DB-triggered: the scrape command populates the `search_vector` column on `Recipe` (weights name/categories/ingredients via `unaccent`). A row is only searchable after scrape populates its vector. Search is ranked via `SearchVectorFilter` in `recipes/api/filters.py`; category filter requires a recipe to match **all** selected categories.

## Deploy

Production runs in Docker: `docker-entrypoint.sh` runs `migrate` + `createcachetable` + gunicorn, fronted by Caddy (`Caddyfile`). Scheduled scrapes run via `deck-chores` labels in `docker-compose.yml`. Swap Caddy's `recipes.eerieemu.com` for `localhost:80` in dev. See `README.md` for the deploy commands.
