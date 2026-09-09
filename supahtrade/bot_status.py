"""Read-only, timestamped bot status. Missing data never means zero profit."""
import json, time, sqlite3
from contextlib import closing
from pathlib import Path

def read_status(path, now=None):
    now = time.time() if now is None else now
    try:
        p = Path(path)
        if p.stat().st_size > 2000000:
            raise ValueError('Oversized status')
        s = json.loads(p.read_text(encoding='utf-8'))
        at = s['at']
        if isinstance(at, bool) or not isinstance(at, (int, float)) or (not 0 <= now - at):
            raise ValueError('Invalid timestamp')
        return {'available': True, 'age_seconds': round(now - at), 'stale': now - at > 120, **{k: s.get(k) for k in ('at', 'armed', 'status', 'mode', 'error', 'portfolio', 'account', 'decisions', 'selected', 'model_calibrated', 'exit_policy', 'held_reviews', 'held_review_policy')}}
    except Exception:
        return {'available': False, 'status': 'unavailable', 'stale': True}

def independent_status(root):
    root = Path(root)
    status = read_status(root / 'status.json')
    status['decisions_historical'] = False
    if status.get('decisions'):
        status['decisions_at'] = status.get('at')
        return status
    try:
        with closing(sqlite3.connect((root / 'signals.sqlite').resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute('SELECT at,report FROM independent_cycles ORDER BY id DESC LIMIT 50').fetchall()
        for (at, raw) in rows:
            report = json.loads(raw)
            if isinstance(report.get('decisions'), list) and report['decisions']:
                status.update(decisions=report['decisions'], decisions_at=at, decisions_historical=True)
                break
    except (sqlite3.Error, ValueError, OSError):
        pass
    return status

def overview(root, weather_root=None, independent_weather_root=None):
    root = Path(root)
    weather = Path(weather_root) if weather_root is not None else root / 'data/us-weather-live-v1'
    independent = Path(independent_weather_root) if independent_weather_root is not None else root / 'data/us-weather-independent-observer-v1'
    combined = root / 'data/us-weather-combined-v1'
    coordinated = False
    try:
        config = json.loads((combined / 'combined.json').read_text())
        coordinated = config.get('version') == 1 and config.get('live') is True
    except (OSError, ValueError):
        pass
    if coordinated:
        weather = combined / 'reference'
        independent = combined / 'independent'
    from .weather_feedback import review
    return {'polymarket': {**read_status(weather / 'status.json'), 'journal_root': str(weather)}, 'independent_weather': {**independent_status(independent), 'journal_root': str(independent)}, 'combined': {'enabled': coordinated, 'journal_root': str(combined) if coordinated else None, 'sizing': config.get('sizing', 'cumulative_10') if coordinated else None, 'accounting': 'Shared portfolio; never add the two strategy totals together.' if coordinated else None}, 'feedback': review(combined) if coordinated else {'available': False}}
