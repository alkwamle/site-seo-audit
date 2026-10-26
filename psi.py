#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PageSpeed Insights (free, no key) for a handful of pages, mobile then desktop, standard library only.

    cd site
    python tools/psi.py                                   # the six pages below, 10 s between requests, Markdown to stdout
    python tools/psi.py --append ../growth/seo/audit-2026-10-26.md   # ... and append the section to that report
    python tools/psi.py --pages / /ar --strategy mobile   # a quick trial

Each run of Lighthouse takes 30-60 s on Google's side; the API rate-limits anonymous callers, hence the pause between
requests (and a 60 s retry on 429). The result per page and strategy: the four category scores, LCP, CLS, TBT, and the
five biggest opportunities (by estimated savings). The raw JSON is kept next to the report (--json) for later comparison.
"""
import argparse, datetime, json, os, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BASE = 'https://kwamlemedia.com'
PAGES = ['/', '/ar', '/solutions', '/solutions/whatsapp-business-automation', '/blog/whatsapp-order-confirmation',
         '/blog/facebook-comment-ai-auto-reply']
API = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
CATS = ['performance', 'seo', 'accessibility', 'best-practices']
CAT_AR = {'performance': 'الأداء', 'seo': 'السيو', 'accessibility': 'الوصول', 'best-practices': 'أفضل الممارسات'}

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def run_psi(url, strategy, timeout=180):
    q = [('url', url), ('strategy', strategy)] + [('category', c) for c in CATS]
    req = urllib.request.Request(API + '?' + urllib.parse.urlencode(q), headers={'User-Agent': 'KwamlePSI/1.0'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', 'replace')[:300]
            except Exception:
                pass
            if e.code in (429, 500, 503) and attempt < 2:
                print('  HTTP %d, waiting 60 s ...' % e.code, flush=True)
                time.sleep(60)
                continue
            return {'error': 'HTTP %d %s' % (e.code, body)}
        except Exception as e:
            if attempt < 2:
                time.sleep(20)
                continue
            return {'error': str(e)[:200]}
    return {'error': 'gave up'}


def summarize(j):
    if 'error' in j:
        return {'error': j['error']}
    lh = j.get('lighthouseResult', {})
    cats = lh.get('categories', {})
    audits = lh.get('audits', {})
    out = {'scores': {c: (round(cats[c]['score'] * 100) if cats.get(c, {}).get('score') is not None else None) for c in CATS}}
    for key, name in (('largest-contentful-paint', 'LCP'), ('cumulative-layout-shift', 'CLS'), ('total-blocking-time', 'TBT'),
                      ('first-contentful-paint', 'FCP'), ('speed-index', 'SI')):
        a = audits.get(key, {})
        out[name] = a.get('displayValue', '—')
    opps = []
    for k, a in audits.items():
        d = a.get('details') or {}
        if d.get('type') == 'opportunity' and (a.get('score') is None or a.get('score') < 1):
            opps.append((d.get('overallSavingsMs') or 0, d.get('overallSavingsBytes') or 0, a.get('title', k), a.get('displayValue', '')))
    opps.sort(key=lambda x: (-x[0], -x[1]))
    out['opportunities'] = [{'title': t, 'savings_ms': ms, 'savings_kb': round(b / 1024), 'display': dv} for ms, b, t, dv in opps[:5]]
    # failed SEO / a11y / best-practice audits (score 0) are worth listing too: they are the cheap fixes
    failed = []
    for cat in ('seo', 'accessibility', 'best-practices'):
        for ref in cats.get(cat, {}).get('auditRefs', []):
            a = audits.get(ref['id'], {})
            if a.get('score') is not None and a['score'] < 1 and a.get('scoreDisplayMode') in ('binary', 'numeric'):
                failed.append('%s: %s' % (CAT_AR[cat], a.get('title', ref['id'])))
    out['failed_audits'] = failed[:12]
    fld = j.get('loadingExperience', {})
    out['field_data'] = fld.get('overall_category', 'No data') if fld.get('metrics') else 'No data'
    return out


def markdown(results, when, source=''):
    L = []
    w = L.append
    w('## PageSpeed Insights — %s' % when.strftime('%Y-%m-%d %H:%M UTC'))
    w('')
    w('الأداة: `python tools/psi.py` (موبايل ثم ديسكتوب، 10 ثواني بين كل طلب).%s بيانات الزوار الحقيقية (Core Web Vitals): %s.' %
      ((' ' + source) if source else '',
       '«No data» لكل الصفحات' if all(r.get('field_data', 'No data') == 'No data' for r in results.values() if 'error' not in r) else 'موجودة لبعض الصفحات'))
    w('')
    w('| الصفحة | الجهاز | الأداء | السيو | الوصول | أفضل الممارسات | LCP | CLS | TBT |')
    w('|---|---|---:|---:|---:|---:|---:|---:|---:|')
    for (page, strategy), r in results.items():
        if 'error' in r:
            w('| %s | %s | خطأ: %s | | | | | | |' % (page, strategy, r['error'][:60]))
            continue
        s = r['scores']
        w('| %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (page, strategy, s['performance'], s['seo'], s['accessibility'], s['best-practices'], r['LCP'], r['CLS'], r['TBT']))
    w('')
    w('### أكبر فرص التحسين لكل صفحة')
    w('')
    for (page, strategy), r in results.items():
        if 'error' in r:
            continue
        w('**%s — %s**' % (page, strategy))
        w('')
        for o in r['opportunities']:
            sav = []
            if o['savings_ms']:
                sav.append('%d ms' % o['savings_ms'])
            if o['savings_kb']:
                sav.append('%d KB' % o['savings_kb'])
            w('- %s%s' % (o['title'], (' — ' + ', '.join(sav)) if sav else ''))
        if not r['opportunities']:
            w('- مفيش فرص مسجّلة')
        if r['failed_audits']:
            w('- فحوص ساقطة: ' + '؛ '.join(r['failed_audits']))
        w('')
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description='PageSpeed Insights for a few pages (no API key)')
    ap.add_argument('--base', default=BASE)
    ap.add_argument('--pages', nargs='*', default=PAGES)
    ap.add_argument('--strategy', nargs='*', default=['mobile', 'desktop'])
    ap.add_argument('--sleep', type=float, default=10)
    ap.add_argument('--json', default='', help='raw results (default ../growth/seo/psi-<today>.json)')
    ap.add_argument('--append', default='', help='append the Markdown section to this report file')
    ap.add_argument('--lhr-dir', default='', help='summarize local Lighthouse JSON files (<strategy>-<name>.json, name = path with / as _, '
                                                  '"home" for /) instead of calling the API: the fallback when the anonymous API quota is used up (HTTP 429)')
    a = ap.parse_args()
    when = datetime.datetime.now(datetime.timezone.utc)
    jpath = a.json or os.path.join(ROOT, 'growth', 'seo', 'psi-%s.json' % when.strftime('%Y-%m-%d'))
    results, raw = {}, {}
    first = True
    source = ''
    for strategy in a.strategy:
        for page in a.pages:
            if a.lhr_dir:
                name = 'home' if page == '/' else page.strip('/').replace('/', '_')
                f = os.path.join(a.lhr_dir, '%s-%s.json' % (strategy, name))
                if not os.path.isfile(f):
                    results[(page, strategy)] = {'error': 'no file ' + os.path.basename(f)}
                    continue
                lhr = json.load(open(f, encoding='utf-8'))
                j = {'lighthouseResult': lhr, 'loadingExperience': {}}
                source = 'المصدر: Lighthouse %s محلي (نفس محرك PageSpeed، قياس معملي بمحاكاة موبايل/ديسكتوب) لأن حصة الـ API المجانية اليومية كانت منتهية (HTTP 429).' % lhr.get('lighthouseVersion', '')
                s = summarize(j)
                results[(page, strategy)] = s
                print('LHR %s %s: %s' % (strategy, page, s.get('error') or ('%(performance)s / %(seo)s / %(accessibility)s / %(best-practices)s' % s['scores'])), flush=True)
                continue
            if not first:
                time.sleep(a.sleep)
            first = False
            url = a.base.rstrip('/') + page
            print('PSI %s %s ...' % (strategy, url), flush=True)
            j = run_psi(url, strategy)
            raw['%s %s' % (strategy, page)] = j if 'error' in j else {'lighthouseResult': {'categories': j.get('lighthouseResult', {}).get('categories'),
                                                                                        'audits': j.get('lighthouseResult', {}).get('audits'),
                                                                                        'fetchTime': j.get('lighthouseResult', {}).get('fetchTime')},
                                                                  'loadingExperience': j.get('loadingExperience')}
            s = summarize(j)
            results[(page, strategy)] = s
            print('  ', s.get('error') or ('%(performance)s / %(seo)s / %(accessibility)s / %(best-practices)s' % s['scores']), flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(jpath)), exist_ok=True)
    with open(jpath, 'w', encoding='utf-8') as f:
        json.dump({'when': when.isoformat(), 'source': source or 'PageSpeed Insights API',
                   'results': {'%s %s' % (k[1], k[0]): v for k, v in results.items()}, 'raw': raw}, f, ensure_ascii=False)
    md = markdown(results, when, source)
    print(md)
    if a.append:
        with open(a.append, 'a', encoding='utf-8', newline='\n') as f:
            f.write('\n' + md)
        print('appended to', a.append)
    return 0


if __name__ == '__main__':
    sys.exit(main())
