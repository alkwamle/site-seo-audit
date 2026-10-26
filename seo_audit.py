#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repeatable SEO audit of the live site (or of the built tree), standard library only (urllib, html.parser, json).

    cd site
    python tools/seo_audit.py                         # crawl https://kwamlemedia.com, write ../growth/seo/audit-<today>.md
    python tools/seo_audit.py --limit 40 --out x.md   # a quick run over the first 40 sitemap addresses + the extra ones
    python tools/seo_audit.py --root .                # the built pages in site/ instead of the live site (no redirects,
                                                      # no app articles: those are served by a Pages function)

What it crawls: every address in sitemap.xml plus a fixed list of extra addresses (home, folders, redirects that must stay
single hops, two app articles, a wrong address that must answer 404).

Per page: status, redirect chain (more than one hop is a problem), response time, HTML size, <title> (<= 60 characters),
meta description (110-165), canonical equal to the address itself, hreflang (every language of the page + x-default,
every target answers 200 and links back), exactly one <h1>, no accidental noindex (meta or X-Robots-Tag), the basic og:*
and twitter:* tags, alt on every <img>, every JSON-LD block parses (its types are listed), and lang/dir on <html> match
the address.

The sitemap itself: well-formed XML, no duplicate address, every address answers 200 with itself as canonical, lastmod
present. robots.txt: present, carries a Sitemap: line, and blocks nothing of kwamlemedia.com that the pages need.

Output: a Markdown report (summary table, the failures per address, the ten slowest pages, structured-data types) with
nothing but public addresses in it.
"""
import argparse, datetime, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
ROOT = os.path.dirname(SITE)
LANGS = ['ar', 'en', 'es', 'fr', 'de', 'pt', 'it', 'ru', 'tr', 'nl', 'el', 'bn', 'vi']
RTL = {'ar'}
BASE = 'https://kwamlemedia.com'
EXTRA = ['/', '/blog', '/solutions', '/app', '/status', '/api', '/en', '/case-studies', '/blog/view',
         '/blog/facebook-comment-ai-auto-reply', '/blog/messenger-auto-reply-templates-ar', '/no-such-page-xyz']
EXPECT_404 = {'/no-such-page-xyz'}
EXPECT_REDIRECT = {'/en', '/case-studies'}          # one hop is the intended behaviour, not a finding
NO_SNIPPET = {'/blog/view'}                          # the app-article viewer shell: served by the Pages function with a real head
TITLE_MAX, DESC_MIN, DESC_MAX = 60, 110, 165
OG_NEEDED = ['og:title', 'og:description', 'og:url', 'og:image', 'og:type']
TW_NEEDED = ['twitter:card', 'twitter:title', 'twitter:description', 'twitter:image']
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) KwamleSEOAudit/1.0 (+https://kwamlemedia.com)'
TIMEOUT = 40

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


# ---------------------------------------------------------------- fetching ------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                                  # 3xx surfaces as HTTPError; the chain is followed by hand below

OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_live(url, max_hops=8):
    """Follows redirects one by one and records the chain. Returns a result dict (status 0 = network failure)."""
    chain, cur = [], url
    for _ in range(max_hops + 1):
        req = urllib.request.Request(cur, headers={
            'User-Agent': UA, 'Accept-Language': 'en',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
        t0 = time.perf_counter()
        try:
            with OPENER.open(req, timeout=TIMEOUT) as r:
                body, status, headers = r.read(), r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, headers = e.code, {k.lower(): v for k, v in e.headers.items()}
            try:
                body = e.read()
            except Exception:
                body = b''
        except Exception as e:                       # URLError, timeout, remote disconnect
            return {'url': url, 'final': cur, 'chain': chain, 'status': 0, 'ms': 0, 'size': 0, 'headers': {},
                    'body': '', 'error': str(getattr(e, 'reason', e))[:120]}
        ms = (time.perf_counter() - t0) * 1000
        if 300 <= status < 400 and headers.get('location'):
            nxt = urllib.parse.urljoin(cur, headers['location'])
            chain.append((cur, status, nxt))
            cur = nxt
            continue
        ctype = headers.get('content-type', '')
        text = body.decode('utf-8', 'replace') if ('text' in ctype or 'xml' in ctype or 'json' in ctype or not ctype) else ''
        return {'url': url, 'final': cur, 'chain': chain, 'status': status, 'ms': ms, 'size': len(body),
                'headers': headers, 'body': text, 'error': ''}
    return {'url': url, 'final': cur, 'chain': chain, 'status': 0, 'ms': 0, 'size': 0, 'headers': {}, 'body': '',
            'error': 'redirect loop'}


def make_local_fetcher(root):
    """Serves the built tree like the host would (extensionless addresses, index.html for folders, 404.html otherwise)."""
    root = os.path.abspath(root)

    def fetch(url):
        p = urllib.parse.urlsplit(url).path
        rel = p.strip('/')
        cands = ['index.html'] if not rel else [rel, rel + '.html', rel + '/index.html']
        t0 = time.perf_counter()
        for c in cands:
            f = os.path.join(root, *c.split('/'))
            if os.path.isfile(f):
                data = open(f, 'rb').read()
                return {'url': url, 'final': url, 'chain': [], 'status': 200, 'ms': (time.perf_counter() - t0) * 1000,
                        'size': len(data), 'headers': {'content-type': 'text/html; charset=utf-8'},
                        'body': data.decode('utf-8', 'replace'), 'error': ''}
        f = os.path.join(root, '404.html')
        data = open(f, 'rb').read() if os.path.isfile(f) else b''
        return {'url': url, 'final': url, 'chain': [], 'status': 404, 'ms': 0, 'size': len(data),
                'headers': {'content-type': 'text/html; charset=utf-8'}, 'body': data.decode('utf-8', 'replace'), 'error': ''}
    return fetch


# ---------------------------------------------------------------- parsing -------------------------------------------
class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = self.dir = None
        self.title = None
        self._in_title = False
        self.meta = {}                               # name/property -> first content
        self.canonicals = []
        self.hreflang = {}                           # code -> href (first)
        self.hreflang_dups = []
        self.h1 = 0
        self.imgs = 0
        self.imgs_noalt = []
        self.ld = []
        self._in_ld = False
        self._buf = []
        self.assets = []                             # same-site files the page needs (robots check)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else '') for k, v in attrs}
        if tag == 'html':
            self.lang, self.dir = a.get('lang'), a.get('dir')
        elif tag == 'title' and self.title is None:
            self._in_title, self.title = True, ''
        elif tag == 'meta':
            key = (a.get('name') or a.get('property') or '').lower()
            if key:
                self.meta.setdefault(key, a.get('content', ''))
        elif tag == 'link':
            rel = a.get('rel', '').lower().split()
            if 'canonical' in rel:
                self.canonicals.append(a.get('href', ''))
            if 'alternate' in rel and a.get('hreflang'):
                code = a['hreflang'].lower()
                if code in self.hreflang:
                    self.hreflang_dups.append(code)
                else:
                    self.hreflang[code] = a.get('href', '')
            if a.get('href') and ({'stylesheet', 'icon', 'preload', 'manifest', 'apple-touch-icon', 'mask-icon'} & set(rel)):
                self.assets.append(a['href'])
        elif tag == 'h1':
            self.h1 += 1
        elif tag == 'img':
            self.imgs += 1
            if 'alt' not in a:
                self.imgs_noalt.append(a.get('src', '')[:90])
            if a.get('src'):
                self.assets.append(a['src'])
        elif tag == 'script':
            if a.get('type', '').lower() == 'application/ld+json':
                self._in_ld, self._buf = True, []
            elif a.get('src'):
                self.assets.append(a['src'])

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        if tag == 'script' and self._in_ld:
            self.ld.append(''.join(self._buf))
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._buf.append(data)


def parse(html_text):
    p = Page()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    return p


# ---------------------------------------------------------------- helpers -------------------------------------------
def norm(u):
    """Canonical comparison: scheme/host lower-cased, no trailing slash except the root, no fragment."""
    if not u:
        return ''
    s = urllib.parse.urlsplit(u.strip())
    path = s.path or '/'
    if len(path) > 1 and path.endswith('/'):
        path = path[:-1]
    return urllib.parse.urlunsplit((s.scheme.lower(), s.netloc.lower(), path, s.query, ''))


def path_of(u):
    return urllib.parse.urlsplit(u).path or '/'


def lang_of(path):
    seg = path.strip('/').split('/')[0]
    if seg in LANGS:
        return seg
    if re.match(r'^/blog/[^/]+-ar$', path):          # an Arabic post published from the app
        return 'ar'
    return 'en'


def partial_langs_page(path):
    """Articles and solutions pages list only the languages they are written in."""
    return bool(re.match(r'^/(?:[a-z]{2}/)?(?:blog/[^/]+|solutions(?:/[^/]+)?)$', path))


def ld_types(obj):
    out = []
    if isinstance(obj, dict):
        t = obj.get('@type')
        if isinstance(t, list):
            out += [str(x) for x in t]
        elif t:
            out.append(str(t))
        if '@graph' in obj:
            for g in obj['@graph']:
                out += ld_types(g)
    elif isinstance(obj, list):
        for x in obj:
            out += ld_types(x)
    return out


class RobotsRules:
    """robots.txt the way Google reads it: groups per user-agent, * and $ in paths, the longest matching rule wins and
    Allow wins a tie (urllib.robotparser knows no wildcards, so a line like /assets/*.svg$ would block nothing there)."""
    def __init__(self, text):
        self.groups = {}                             # agent (lower) -> [(allow, pattern)]
        agents, in_rules = [], False
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            key, val = [x.strip() for x in line.split(':', 1)]
            key = key.lower()
            if key == 'user-agent':
                if in_rules:                         # a user-agent line after rules opens a new group
                    agents, in_rules = [], False
                agents.append(val.lower())
                self.groups.setdefault(val.lower(), [])
            elif key in ('allow', 'disallow') and agents:
                in_rules = True
                for a in agents:
                    self.groups[a].append((key == 'allow', val))

    def group(self, agent):
        agent = agent.lower()
        for a in self.groups:
            if a != '*' and a in agent:
                return self.groups[a]
        return self.groups.get('*', [])

    @staticmethod
    def _match(pattern, path):
        if not pattern:
            return False
        rx = '^' + ''.join('.*' if ch == '*' else ('$' if ch == '$' and i == len(pattern) - 1 else re.escape(ch)) for i, ch in enumerate(pattern))
        return re.match(rx, path) is not None

    def can_fetch(self, agent, url):
        s = urllib.parse.urlsplit(url)
        path = (s.path or '/') + (('?' + s.query) if s.query else '')
        best, best_len = True, -1
        for allow, pattern in self.group(agent):
            if self._match(pattern, path):
                L = len(pattern)
                if L > best_len or (L == best_len and allow):
                    best, best_len = allow, L
        return best


# ---------------------------------------------------------------- the audit -----------------------------------------
class Audit:
    def __init__(self, base, fetch, workers):
        self.base = base.rstrip('/')
        self.fetch = fetch
        self.workers = workers
        self.results = {}                            # requested address -> fetch result (+ parsed page)
        self.fails = []                              # (check, address, message)
        self.counts = {}                             # check -> [pass, fail]
        self.ld_count = {}
        self.no_ld = []
        self.used_sitemap = 0
        self.sitemap = {'ok': False, 'count': 0, 'dups': [], 'no_lastmod': [], 'bad_lastmod': [], 'error': ''}
        self.robots = {'status': 0, 'sitemaps': [], 'blocked': [], 'error': '', 'disallow': []}

    # -- bookkeeping
    def mark(self, check, address, ok, msg=''):
        c = self.counts.setdefault(check, [0, 0])
        c[0 if ok else 1] += 1
        if not ok:
            self.fails.append((check, address, msg))

    def get(self, url):
        r = self.results.get(url)
        if r is None:
            r = self.fetch(url)
            r['page'] = parse(r['body']) if r['body'] and 'html' in r['headers'].get('content-type', 'text/html') else None
            self.results[url] = r
        return r

    def fetch_all(self, urls):
        todo = [u for u in urls if u not in self.results]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for r in ex.map(self.fetch, todo):
                r['page'] = parse(r['body']) if r['body'] and 'html' in r['headers'].get('content-type', 'text/html') else None
                self.results[r['url']] = r
                print('  %3d %6.0fms %s' % (r['status'], r['ms'], r['url']), flush=True)

    # -- sources
    def read_sitemap(self):
        r = self.get(self.base + '/sitemap.xml')
        urls, lastmods = [], {}
        if r['status'] != 200 or not r['body']:
            self.sitemap['error'] = 'sitemap.xml answered %s' % (r['status'] or r['error'])
            return urls, lastmods
        try:
            root = ET.fromstring(r['body'].encode('utf-8'))
        except ET.ParseError as e:
            self.sitemap['error'] = 'sitemap.xml is not well-formed XML: %s' % e
            return urls, lastmods
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for u in root.findall('s:url', ns):
            loc = (u.findtext('s:loc', default='', namespaces=ns) or '').strip()
            if not loc:
                continue
            lm = (u.findtext('s:lastmod', default='', namespaces=ns) or '').strip()
            if loc in lastmods:
                self.sitemap['dups'].append(loc)
            urls.append(loc)
            lastmods[loc] = lm
            if not lm:
                self.sitemap['no_lastmod'].append(loc)
            elif not re.match(r'^\d{4}-\d{2}-\d{2}(T[\d:.+\-Z]+)?$', lm):
                self.sitemap['bad_lastmod'].append(loc + ' (' + lm + ')')
        self.sitemap['ok'] = True
        self.sitemap['count'] = len(urls)
        return list(dict.fromkeys(urls)), lastmods

    def read_robots(self):
        r = self.get(self.base + '/robots.txt')
        self.robots['status'] = r['status']
        if r['status'] != 200 or not r['body']:
            self.robots['error'] = 'robots.txt answered %s' % (r['status'] or r['error'])
            return None
        self.robots['sitemaps'] = re.findall(r'(?im)^\s*sitemap:\s*(\S+)', r['body'])
        rules = RobotsRules(r['body'])
        self.robots['disallow'] = [p for allow, p in rules.group('*') if not allow]
        return rules

    # -- per-page checks
    def check_page(self, url, expect_404=False, expect_redirect=False, in_sitemap=False):
        r = self.results[url]
        path = path_of(url)
        st = r['status']
        hops = len(r['chain'])
        # status
        if expect_404:
            self.mark('404 حقيقية', url, st == 404, 'الحالة %s بدل 404' % (st or r['error']))
            if st == 404 and r.get('page') and r['page'].title is not None:
                self.mark('صفحة 404 بالهوية', url, 'Kwamle' in r['body'] or 'كواملي' in r['body'], 'صفحة 404 من غير هوية الموقع')
            return
        self.mark('الحالة 200', url, st == 200, 'الحالة %s%s' % (st or 'فشل الاتصال', (' (' + r['error'] + ')') if r['error'] else ''))
        # redirects
        if expect_redirect:
            self.mark('التحويلات', url, hops == 1 and st == 200,
                      'المتوقع تحويل واحد لصفحة 200، الفعلي %d تحويل وحالة %s' % (hops, st))
            return                                   # the destination is audited under its own address
        elif in_sitemap:
            self.mark('التحويلات', url, hops == 0, 'عنوان في خريطة الموقع بيحوّل: ' + ' → '.join(['%s (%s)' % (c[0], c[1]) for c in r['chain']] + [r['final']]))
        else:
            self.mark('التحويلات', url, hops <= 1, 'سلسلة %d تحويلات: ' % hops + ' → '.join(['%s (%s)' % (c[0], c[1]) for c in r['chain']] + [r['final']]))
        if st != 200 or not r.get('page'):
            return
        p = r['page']
        final = r['final']
        snippet_page = path not in NO_SNIPPET
        if not snippet_page:                         # the bare viewer shell: it must not be an indexable empty page
            rob = (p.meta.get('robots', '') + ' ' + r['headers'].get('x-robots-tag', '')).lower()
            self.mark('عارض المقالات /blog/view', url, 'noindex' in rob,
                      'العارض الفاضي بيرد 200 وقابل للفهرسة (canonical «%s»، lang=%s)' % (p.canonicals[0] if p.canonicals else '', p.lang))
            return
        # title
        if p.title is None or not p.title.strip():
            self.mark('العنوان', url, False, 'من غير <title>')
        else:
            t = ' '.join(p.title.split())
            self.mark('العنوان', url, len(t) <= TITLE_MAX, 'العنوان %d حرف (الحد %d): «%s»' % (len(t), TITLE_MAX, t))
        # description
        d = ' '.join(p.meta.get('description', '').split())
        if snippet_page:
            if not d:
                self.mark('الوصف', url, False, 'من غير meta description')
            else:
                self.mark('الوصف', url, DESC_MIN <= len(d) <= DESC_MAX, 'الوصف %d حرف (المدى %d-%d)' % (len(d), DESC_MIN, DESC_MAX))
        # canonical
        if snippet_page:
            if len(p.canonicals) != 1:
                self.mark('canonical', url, False, '%d رابط canonical' % len(p.canonicals))
            else:
                self.mark('canonical', url, norm(p.canonicals[0]) == norm(final), 'canonical = %s' % (p.canonicals[0] or '(فاضي)'))
            ogu = p.meta.get('og:url', '')
            self.mark('og:url = canonical', url, bool(p.canonicals) and norm(ogu) == norm(p.canonicals[0]), 'og:url = %s' % (ogu or '(فاضي)'))
        # robots
        rob = (p.meta.get('robots', '') + ' ' + p.meta.get('googlebot', '') + ' ' + r['headers'].get('x-robots-tag', '')).lower()
        self.mark('بدون noindex', url, 'noindex' not in rob and 'none' not in rob.split(','), 'فيه noindex: ' + rob.strip())
        # h1
        self.mark('h1 واحد', url, p.h1 == 1, '%d عنوان رئيسي' % p.h1)
        # social
        if snippet_page:
            miss = [k for k in OG_NEEDED if not p.meta.get(k)]
            self.mark('وسوم og', url, not miss, 'ناقص: ' + ', '.join(miss))
            miss = [k for k in TW_NEEDED if not p.meta.get(k)]
            self.mark('وسوم twitter', url, not miss, 'ناقص: ' + ', '.join(miss))
        # images
        self.mark('alt للصور', url, not p.imgs_noalt, '%d صورة من غير alt: %s' % (len(p.imgs_noalt), ', '.join(p.imgs_noalt[:3])))
        # structured data
        bad, types = [], []
        for i, block in enumerate(p.ld):
            try:
                obj = json.loads(block)
                types += ld_types(obj)
            except Exception as e:
                bad.append('الكتلة %d: %s' % (i + 1, str(e)[:60]))
        for t in types:
            self.ld_count[t] = self.ld_count.get(t, 0) + 1
        self.mark('JSON-LD يتقرا', url, not bad, '; '.join(bad))
        if not p.ld:
            self.no_ld.append(url)
        r['ld_types'] = types
        # lang / dir
        want = lang_of(path)
        got = (p.lang or '').lower().split('-')[0]
        want_dir = 'rtl' if want in RTL else 'ltr'
        got_dir = (p.dir or 'ltr').lower()
        self.mark('lang و dir', url, got == want and got_dir == want_dir, 'lang=%s dir=%s والمتوقع lang=%s dir=%s' % (p.lang, p.dir, want, want_dir))
        # hreflang (targets are checked after every page is fetched)
        if snippet_page:
            if not p.hreflang:
                self.mark('hreflang', url, False, 'من غير روابط hreflang')
            else:
                probs = []
                if 'x-default' not in p.hreflang:
                    probs.append('من غير x-default')
                if want not in p.hreflang:
                    probs.append('لغة الصفحة نفسها (%s) مش في القائمة' % want)
                if p.hreflang_dups:
                    probs.append('مكرر: ' + ', '.join(sorted(set(p.hreflang_dups))))
                if not partial_langs_page(path):
                    missing = [l for l in LANGS if l not in p.hreflang]
                    if missing:
                        probs.append('لغات ناقصة: ' + ' '.join(missing))
                own = p.hreflang.get(want, '')
                if own and norm(own) != norm(final):
                    probs.append('رابط لغة الصفحة (%s) مش العنوان نفسه' % own)
                r['hreflang_probs'] = probs

    def check_hreflang_targets(self):
        """Every hreflang target answers 200 and names this page in its own hreflang set."""
        targets = set()
        for url, r in list(self.results.items()):
            p = r.get('page')
            if r['status'] == 200 and p and p.hreflang and path_of(url) not in NO_SNIPPET and not r.get('skip_hreflang'):
                for code, href in p.hreflang.items():
                    if href and urllib.parse.urlsplit(href).netloc.lower() == urllib.parse.urlsplit(self.base).netloc.lower():
                        targets.add(href)
        self.fetch_all(sorted(targets))
        for url, r in list(self.results.items()):
            p = r.get('page')
            if r['status'] != 200 or not p or not p.hreflang or 'hreflang_probs' not in r:
                continue
            probs = list(r['hreflang_probs'])
            me = norm(r['final'])
            for code, href in p.hreflang.items():
                if not href:
                    probs.append('%s: رابط فاضي' % code)
                    continue
                t = self.results.get(href)
                if t is None:
                    continue
                if t['status'] != 200:
                    probs.append('%s → %s بيرد %s' % (code, href, t['status'] or t['error']))
                    continue
                if t['chain']:
                    probs.append('%s → %s بيحوّل' % (code, href))
                tp = t.get('page')
                if tp and code != 'x-default' and norm(href) != me:
                    back = [norm(h) for h in tp.hreflang.values()]
                    if me not in back:
                        probs.append('%s → %s ما بيشاورش على الصفحة دي بالمثل' % (code, href))
            self.mark('hreflang', url, not probs, '; '.join(probs))

    def check_sitemap_entries(self, urls, lastmods):
        for u in urls:
            r = self.results.get(u)
            if not r:
                continue
            p = r.get('page')
            ok = r['status'] == 200 and not r['chain'] and p is not None and len(p.canonicals) == 1 and norm(p.canonicals[0]) == norm(u)
            why = ('الحالة %s' % (r['status'] or r['error'])) if r['status'] != 200 else ('بيحوّل' if r['chain'] else 'canonical = %s' % ((p.canonicals[0] if p and p.canonicals else '(فاضي)')))
            self.mark('خريطة الموقع: 200 + canonical', u, ok, why)
            self.mark('خريطة الموقع: lastmod', u, bool(lastmods.get(u)), 'من غير lastmod')

    def check_robots(self, rp, urls):
        if rp is None:
            return
        want = [self.base + '/sitemap.xml']
        has = any(norm(s) == norm(want[0]) for s in self.robots['sitemaps'])
        self.mark('robots: سطر Sitemap', self.base + '/robots.txt', has, 'مفيش سطر Sitemap لخريطة الموقع الأساسية')
        host = urllib.parse.urlsplit(self.base).netloc.lower()
        need = set(urls)
        for u, r in self.results.items():
            p = r.get('page')
            if p and r['status'] == 200:
                for a in p.assets:
                    if a.startswith('data:'):
                        continue
                    full = urllib.parse.urljoin(r['final'], a.split(' ')[0])
                    if urllib.parse.urlsplit(full).netloc.lower() == host:
                        need.add(full)
        blocked = []
        for u in sorted(need):
            for agent in ('*', 'Googlebot'):
                if not rp.can_fetch(agent, u):
                    blocked.append('%s (%s)' % (u, agent))
                    break
        self.robots['blocked'] = blocked
        self.mark('robots: ما بيحجبش الموقع', self.base + '/robots.txt', not blocked,
                  '%d عنوان محجوب: %s%s' % (len(blocked), '; '.join(blocked[:5]), ' …' if len(blocked) > 5 else ''))

    # -- run
    def run(self, limit=0):
        print('sitemap.xml ...', flush=True)
        urls, lastmods = self.read_sitemap()
        if limit:
            urls = urls[:limit]
        self.used_sitemap = len(urls)
        extra = [self.base + e for e in EXTRA]
        order = list(dict.fromkeys(urls + extra))
        print('fetching %d addresses ...' % len(order), flush=True)
        self.fetch_all(order)
        for u in order:
            path = path_of(u)
            self.check_page(u, expect_404=path in EXPECT_404, expect_redirect=path in EXPECT_REDIRECT, in_sitemap=u in lastmods)
        print('hreflang targets ...', flush=True)
        self.check_hreflang_targets()
        self.check_sitemap_entries(urls, lastmods)
        print('robots.txt ...', flush=True)
        rp = self.read_robots()
        self.check_robots(rp, order)
        self.sitemap['dups'] = sorted(set(self.sitemap['dups']))
        self.mark('خريطة الموقع: XML صحيح', self.base + '/sitemap.xml', self.sitemap['ok'], self.sitemap['error'])
        self.mark('خريطة الموقع: بدون تكرار', self.base + '/sitemap.xml', not self.sitemap['dups'], 'مكرر: ' + ', '.join(self.sitemap['dups'][:5]))
        if self.sitemap['bad_lastmod']:
            self.mark('خريطة الموقع: lastmod', self.base + '/sitemap.xml', False, 'صيغة lastmod غلط: ' + ', '.join(self.sitemap['bad_lastmod'][:5]))
        return order

    # -- report
    def report(self, order, started, out_path, source):
        pages = [self.results[u] for u in order]
        n200 = sum(1 for r in pages if r['status'] == 200)
        lines = []
        w = lines.append
        w('# فحص سيو الموقع الحي — %s' % started.strftime('%Y-%m-%d'))
        w('')
        w('- المصدر: %s' % source)
        w('- وقت الفحص: %s' % started.strftime('%Y-%m-%d %H:%M UTC'))
        w('- الأداة: `python tools/seo_audit.py` (من مجلد `site/`)')
        w('- العناوين المفحوصة: %d (من خريطة الموقع %d + %d عنوان إضافي)، منها %d بيرد 200' % (len(order), self.used_sitemap, len(EXTRA), n200))
        w('- روابط hreflang اللي اتجابت زيادة عشان التحقق: %d' % max(0, len(self.results) - len(order) - 2))
        w('')
        w('## الملخص')
        w('')
        w('| الفحص | ناجح | فاشل |')
        w('|---|---:|---:|')
        for check, (ok, bad) in self.counts.items():
            w('| %s | %d | %d |' % (check, ok, bad))
        w('')
        w('## خريطة الموقع')
        w('')
        w('- XML صحيح: %s' % ('نعم' if self.sitemap['ok'] else 'لا — ' + self.sitemap['error']))
        w('- عدد العناوين: %d' % self.sitemap['count'])
        w('- عناوين مكررة: %d' % len(self.sitemap['dups']))
        w('- من غير lastmod: %d' % len(self.sitemap['no_lastmod']))
        w('')
        w('## robots.txt')
        w('')
        w('- الحالة: %s' % (self.robots['status'] or self.robots['error']))
        w('- سطور Sitemap: %s' % (', '.join('`%s`' % s for s in self.robots['sitemaps']) or 'مفيش'))
        w('- سطور Disallow للجميع: %s' % (', '.join('`%s`' % s for s in self.robots['disallow']) or 'مفيش'))
        w('- عناوين من الموقع نفسه محجوبة: %d' % len(self.robots['blocked']))
        for b in self.robots['blocked'][:20]:
            w('  - %s' % b)
        w('')
        w('## قائمة الفشل')
        w('')
        if not self.fails:
            w('مفيش.')
        else:
            w('| الفحص | العنوان | المشكلة |')
            w('|---|---|---|')
            for check, url, msg in self.fails:
                w('| %s | %s | %s |' % (check, url.replace('|', '%7C'), (msg or '').replace('|', '\\|').replace('\n', ' ')))
        w('')
        w('## أبطأ 10 صفحات (زمن الرد الكامل للطلب الأخير في السلسلة)')
        w('')
        w('| العنوان | الزمن | حجم HTML | الحالة |')
        w('|---|---:|---:|---:|')
        for r in sorted(pages, key=lambda x: -x['ms'])[:10]:
            w('| %s | %.0f ms | %.0f KB | %s |' % (r['url'], r['ms'], r['size'] / 1024, r['status']))
        if pages:
            ms = sorted(r['ms'] for r in pages if r['status'])
            w('')
            w('الوسيط %.0f ms، المتوسط %.0f ms، أكبر صفحة %.0f KB.' % (ms[len(ms) // 2], sum(ms) / len(ms), max(r['size'] for r in pages) / 1024))
        w('')
        w('## أنواع البيانات المهيكلة (عدد الصفحات اللي فيها كل نوع)')
        w('')
        for t, c in sorted(self.ld_count.items(), key=lambda x: -x[1]):
            w('- %s: %d' % (t, c))
        if self.no_ld:
            w('')
            w('صفحات من غير أي بيانات مهيكلة (%d): %s' % (len(self.no_ld), ', '.join(self.no_ld[:30]) + (' …' if len(self.no_ld) > 30 else '')))
        w('')
        text = '\n'.join(lines) + '\n'
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return text


def main():
    ap = argparse.ArgumentParser(description='SEO audit of kwamlemedia.com (live) or of the built tree (--root)')
    ap.add_argument('--base', default=BASE, help='site origin (default %s)' % BASE)
    ap.add_argument('--root', default='', help='audit the built pages in this folder instead of the live site')
    ap.add_argument('--out', default='', help='Markdown report path (default ../growth/seo/audit-<today>.md)')
    ap.add_argument('--limit', type=int, default=0, help='only the first N sitemap addresses (quick trial)')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--json', default='', help='also dump the raw per-page results to this JSON file')
    a = ap.parse_args()
    started = datetime.datetime.now(datetime.timezone.utc)
    out = a.out or os.path.join(ROOT, 'growth', 'seo', 'audit-%s.md' % started.strftime('%Y-%m-%d'))
    fetch = make_local_fetcher(a.root) if a.root else fetch_live
    audit = Audit(a.base, fetch, a.workers)
    order = audit.run(limit=a.limit)
    source = ('الصفحات المبنية في %s' % os.path.abspath(a.root)) if a.root else a.base
    text = audit.report(order, started, out, source)
    if a.json:
        dump = {u: {k: v for k, v in r.items() if k not in ('body', 'page', 'headers')} for u, r in audit.results.items()}
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
    print(text.split('## خريطة الموقع')[0])
    print('report:', out)
    return 1 if audit.fails else 0


if __name__ == '__main__':
    sys.exit(main())
