# site-seo-audit

Two small, dependency-free Python scripts that we use to keep [kwamlemedia.com](https://kwamlemedia.com) healthy in search. They work for any static or mostly-static site: point them at your origin.

## `seo_audit.py` — crawl a live site (or a built folder) and report

For every address in the sitemap (plus a few you name) it checks:

- status code and redirect chains
- `<title>` length (≤ 60), meta description length (110–165), one `<h1>`
- canonical equals the address, `og:url` equals the canonical
- `hreflang` links: every language present, `x-default` present, targets answer 200, and the reciprocal link exists
- Open Graph and Twitter tags, `meta robots` without an accidental `noindex`
- every `<img>` has `alt`, every JSON-LD block parses (types are listed)
- `lang` and `dir` on `<html>`
- the sitemap itself: valid XML, no duplicates, `lastmod` present, every address 200 with a self-canonical
- `robots.txt`: present, carries a `Sitemap:` line, blocks nothing the pages need
- a real 404 page for a wrong address, response times and HTML sizes (slowest 10)

```bash
python seo_audit.py --base https://example.com --out audit.md
python seo_audit.py --base https://example.com --limit 50          # quick trial
python seo_audit.py --root ./dist --base https://example.com       # the built folder instead of the live site
```

Output: a Markdown report with a summary table (pass/fail per check), the list of failures, the slowest pages and the structured-data types found. `--json` also dumps the raw per-page results.

## `psi.py` — PageSpeed Insights for a list of pages

Calls the free PageSpeed Insights API (no key needed, rate-limited) for the pages you list, mobile and desktop, and appends a Markdown section with the four category scores, LCP / CLS / TBT and the top opportunities per page. The API answers 429 when the daily quota is gone; `--lhr-dir` summarizes local Lighthouse JSON files instead.

```bash
python psi.py --base https://example.com --pages / /blog /pricing --append audit.md
```

## Notes

- Python 3.10+, standard library only.
- The crawler identifies itself with a clear user agent and runs with a small number of workers; keep it that way on sites you do not own.
- Written for the Kwamle Media site; shared in case it saves someone an afternoon.

## License

MIT
