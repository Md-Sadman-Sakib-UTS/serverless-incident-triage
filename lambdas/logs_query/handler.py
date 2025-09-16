import os, csv, io, boto3, json, time
from datetime import datetime
from collections import Counter

s3 = boto3.client('s3')


def _json_log(msg, **kw):
    print(json.dumps({"ts": int(time.time() * 1000), "stage": "logs_query", "msg": msg, **kw}))


def _emit_rows_scanned(n: int):
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "SIREN/App",
                "Dimensions": [["Stage"]],
                "Metrics": [{"Name": "RowsScanned", "Unit": "Count"}]
            }]
        },
        "Stage": "logs_query",
        "RowsScanned": int(n)
    }))


def _parse_dt(s: str):
    if not s:
        return None
    s = s.strip()
    if s in ('?', 'null', 'None'):
        return None
    for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _hours(delta): return delta.total_seconds() / 3600.0


def handler(event, context):
    bucket = os.environ['ARTIFACTS_BUCKET']
    key = os.environ.get('INCIDENTS_KEY', 'data/incident_event_log.csv')
    max_rows = int(os.environ.get('MAX_ROWS', '50000'))  # cap scanning for demos

    # STREAM the object (no full read into memory)
    obj = s3.get_object(Bucket=bucket, Key=key)
    stream = io.TextIOWrapper(obj['Body'], encoding='utf-8', errors='ignore')
    reader = csv.DictReader(stream)

    # Minimal per-incident state to compute aggregates
    per_inc = {}
    cat = Counter()
    prio = Counter()
    rows = 0

    for row in reader:
        rows += 1
        if rows > max_rows:
            break

        num = row.get('number') or f'row{rows}'
        rec = per_inc.setdefault(num, {
            'opened_at': None, 'resolved_at': None, 'closed_at': None,
            'category': '', 'priority': ''
        })

        # keep first seen category/priority
        if not rec['category']:
            rec['category'] = row.get('category') or ''
        if not rec['priority']:
            rec['priority'] = row.get('priority') or ''

        oa = _parse_dt(row.get('opened_at', ''))
        ra = _parse_dt(row.get('resolved_at', ''))
        ca = _parse_dt(row.get('closed_at', ''))

        if oa and (rec['opened_at'] is None or oa < rec['opened_at']): rec['opened_at'] = oa
        if ra and (rec['resolved_at'] is None or ra < rec['resolved_at']): rec['resolved_at'] = ra
        if ca and (rec['closed_at'] is None or ca < rec['closed_at']): rec['closed_at'] = ca

    # aggregates (small memory)
    dur_hours = []
    for rec in per_inc.values():
        if rec['category']: cat[rec['category']] += 1
        if rec['priority']: prio[rec['priority']] += 1
        if rec['opened_at'] and rec['resolved_at']:
            dur_hours.append(_hours(rec['resolved_at'] - rec['opened_at']))

    mttr = (sum(dur_hours) / len(dur_hours)) if dur_hours else None
    top = lambda c, k=5: [{'value': v, 'count': n} for v, n in c.most_common(k)]

    summary = {
        'rows_scanned': rows,
        'incidents': len(per_inc),
        'resolved_count': len(dur_hours),
        'approx_mttr_hours': mttr,
        'top_categories': top(cat),
        'by_priority': top(prio)
    }

    rows_scanned = summary.get('rows_scanned', 0)
    _json_log('aggregate_complete', rows_scanned=rows_scanned)
    _emit_rows_scanned(rows_scanned)

    return {
        'stage': 'logs_query',
        'ok': True,
        'summary': summary,
        'prev': event
    }
