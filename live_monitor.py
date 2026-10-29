"""Public live-market monitoring; no account API, credentials, or trading methods."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from engine import BARS, analyze, make_plan, number, trend_direction
from workflow import connect, oi_evidence

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'monitor.live.json'
VERSION = '0.4.0'
DAY = 86400000


def load_config(path=CONFIG):
    c = json.loads(Path(path).read_text())
    if c.get('environment') != 'live_public' or c.get('execution') != 'MANUAL_ALERT_ONLY':
        raise ValueError('PUBLIC_MONITOR_ONLY')
    if not 1 <= c['top_n'] <= 200 or not 35 <= c['candle_limit'] <= 300:
        raise ValueError('INVALID_SCAN_LIMIT')
    if c['universe_refresh_hours'] != 24 or c['fast_min_volume_usdt'] < 100000000 or c['fast_excursion_pct'] < 10:
        raise ValueError('INVALID_UNIVERSE_RULES')
    return c


class PublicMarket:
    ROUTES = {'instruments': '/api/v5/public/instruments', 'tickers': '/api/v5/market/tickers',
              'ticker': '/api/v5/market/ticker', 'candles': '/api/v5/market/candles',
              'open-interest': '/api/v5/public/open-interest', 'funding-rate': '/api/v5/public/funding-rate'}

    def __init__(self, proxy):
        # This client has no account or order routes and never reads OKX credentials.
        if proxy is not None:
            raise ValueError('UNREVIEWED_PROXY')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def get(self, action, **params):
        if action not in self.ROUTES:
            raise ValueError('NON_PUBLIC_ROUTE_BLOCKED')
        url = 'https://www.okx.com' + self.ROUTES[action] + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'OKX-public-monitor/0.3'})
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=20) as response:
                    payload = json.load(response)
                break
            except (OSError, http.client.IncompleteRead):
                if attempt == 2:
                    raise RuntimeError('PUBLIC_GET_FAILED:' + action) from None
                time.sleep(attempt + 1)
        if payload.get('code') != '0' or not isinstance(payload.get('data'), list):
            raise ValueError('PUBLIC_API_ERROR')
        time.sleep(.12)
        return payload['data']


def fast_metrics(t, now):
    ts = int(t['ts'])
    o, high, low, last = [number(t[k]) for k in ('open24h', 'high24h', 'low24h', 'last')]
    if not 0 <= now - ts <= 120000 or min(o, high, low, last) <= 0 or low > high:
        raise ValueError('INVALID_UNIVERSE_TICKER')
    return {'turnover_usdt_estimate': number(t['volCcy24h']) * last,
            'turnover_basis': 'base_volume24h_times_last_price_estimate_not_exact_quote_turnover',
            'high_excursion_pct': (high/o-1)*100, 'low_excursion_pct': (low/o-1)*100,
            'current_change_pct': (last/o-1)*100, 'ticker_ts': ts}


def refresh_universe(db, market, specs, ticks, cfg, now):
    db.execute('CREATE TABLE IF NOT EXISTS universe(id INTEGER PRIMARY KEY CHECK(id=1), updated INTEGER, payload TEXT)')
    previous = db.execute('SELECT updated,payload FROM universe WHERE id=1').fetchone()
    if previous and 0 <= now-previous[0] < DAY:
        result = json.loads(previous[1]); result['refreshed'] = False
        return result
    major = set(cfg['groups']['major']['symbols'])
    members, errors = [], []
    for t in ticks:
        name = t['instId']
        if name not in specs or name.split('-')[0] in major:
            continue
        try:
            m = fast_metrics(t, now)
            excursion = max(m['high_excursion_pct'], -m['low_excursion_pct'])
            if m['turnover_usdt_estimate'] >= cfg['fast_min_volume_usdt'] and excursion + 1e-9 >= cfg['fast_excursion_pct']:
                category = str(specs[name].get('instCategory', ''))
                # OKX public metadata does not reliably distinguish MEME from other crypto.
                label = 'TradFi' if category in ['3','4','5','6','7'] else 'crypto-unclassified'
                members.append({'inst_id': name, 'category': label, 'inst_category': category, **m})
        except (ValueError, KeyError, TypeError):
            errors.append(name)
    if errors:
        raise ValueError('INCOMPLETE_UNIVERSE_SNAPSHOT')
    members.sort(key=lambda x: -x['turnover_usdt_estimate'])
    result = {'updated_at': now, 'next_refresh_at': now+DAY, 'refreshed': True, 'members': members,
              'rule': '24h estimated turnover>=100m and 24h high/open or low/open excursion>=10%',
              'classification': 'TradFi tagged by OKX; crypto unclassified, not automatically claimed to be MEME'}
    db.execute('INSERT OR REPLACE INTO universe VALUES(1,?,?)', (now,json.dumps(result)))
    db.commit()
    return result


@contextmanager
def run_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as lock:
        path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def bucket(spec):
    symbol = spec['instId'].split('-')[0]
    if str(spec.get('instCategory')) not in ['3','4','5','6','7']:
        return 'crypto-beta'
    if symbol in ['SNDK','SOXL','SKHYNIX','MU','NVDA','AMD']:
        return 'semiconductors'
    return 'tradfi-category:' + str(spec.get('instCategory'))


def run(cfg, market, db_path, out_dir, top=None):
    now = int(time.time()*1000)
    db = connect(db_path)
    try:
        specs = {x['instId']: x for x in market.get('instruments', instType='SWAP')
                 if x.get('state') == 'live' and x.get('settleCcy') == 'USDT' and x.get('ctType') == 'linear'}
        ticks = market.get('tickers', instType='SWAP')
        now = int(time.time()*1000)
        universe = refresh_universe(db,market,specs,ticks,cfg,now)
        majors = [s+'-USDT-SWAP' for s in cfg['groups']['major']['symbols'] if s+'-USDT-SWAP' in specs]
        names = majors+[x['inst_id'] for x in universe['members'] if x['inst_id'] in specs]
        names = names[:top or cfg['top_n']]
        digest = hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest()[:12]
        run_id = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S')+'-'+str(time.time_ns())[-6:]
        report = {'run_id':run_id,'version':VERSION,'environment':'live_public','execution':'MANUAL_ALERT_ONLY',
                  'account_accessed':False,'orders_placed':0,'universe':universe,'scanned':len(names),'results':[], 'plans':[],
                  'portfolio_status':'NOT_RECONCILED: indicative single-plan sizes only; no daily loss/portfolio enforcement'}
        db.execute('INSERT INTO runs VALUES(?,?,?,?)',(run_id,now,VERSION,json.dumps(cfg)))
        db.execute('CREATE TABLE IF NOT EXISTS market_snapshots(run_id TEXT PRIMARY KEY,ts INTEGER,payload TEXT)')
        snapshot={'tickers':[t for t in ticks if t['instId'] in specs],'scanned_symbols':names}
        db.execute('INSERT INTO market_snapshots VALUES(?,?,?)',(run_id,now,json.dumps(snapshot)))
        try:
            oi = {x['instId']:x for x in market.get('open-interest',instType='SWAP')}
        except (OSError, ValueError, RuntimeError):
            oi = {}
            report['oi_status'] = 'UNAVAILABLE_NOT_AN_ENTRY_GATE'
        for name in names:
            group = 'major' if name in majors else 'fast'
            local = {**cfg, 'bar':cfg['groups'][group]['entry_bar']}
            try:
                raw = market.get('candles', instId=name, bar=local['bar'], limit=cfg['candle_limit'])
                current = int(time.time()*1000)
                r = analyze(name,raw,local,current)
                r['group'] = group
                # Always journal full filtering results; only request higher timeframe for candidates.
                if r['signal']:
                    trendbar = cfg['groups'][group]['trend_bar']
                    trend = trend_direction(market.get('candles',instId=name,bar=trendbar,limit=cfg['candle_limit']),trendbar,local,int(time.time()*1000))
                    r['trend'] = trend
                    if trend['direction'] != r['side']:
                        r.update(signal=False, reason='HIGHER_TIMEFRAME_NOT_ALIGNED')
                if r['signal']:
                    r['signal_id'] += '-live-'+digest
                    old = db.execute('SELECT plan FROM plans WHERE id=?',(r['signal_id'],)).fetchone()
                    if old:
                        report['plans'].append(json.loads(old[0]))
                    else:
                        t = market.get('ticker',instId=name)[0]
                        current = int(time.time()*1000)
                        bid,ask = number(t['bidPx']),number(t['askPx'])
                        if not 0 <= current-int(t['ts']) <= 30000 or not 0 < bid <= ask:
                            raise ValueError('STALE_OR_INVALID_QUOTE')
                        if (ask-bid)/((ask+bid)/2)*10000 > cfg['max_spread_bps']:
                            raise ValueError('SPREAD_TOO_WIDE')
                        if current >= r['closed_at']+BARS[local['bar']]:
                            raise ValueError('SIGNAL_EXPIRED')
                        # Deliberately illustrative standalone budget. Never assert account capacity.
                        risk = {**cfg['risk'], 'existing_risk_usdt':0, 'existing_gross_usdt':0,
                                'available_margin_usdt':cfg['risk']['equity_usdt']}
                        p = make_plan(r,specs[name],t,local,risk)
                        p.update(environment='live_public', version=VERSION, group=group, entry_bar=local['bar'],
                                 created_at=current, trend=r['trend'], correlation_bucket=bucket(specs[name]),
                                 risk_scope='INDICATIVE_SINGLE_PLAN_NOT_RECONCILED_WITH_ACCOUNT',
                                 blockers=['MANUAL_ACCOUNT_RISK_RECHECK_REQUIRED'],
                                 portfolio_limits_enforced=False, daily_loss_guard_enforced=False,
                                 reason_details={k:r.get(k) for k in ['ma20','macd_hist','rsi','vol_ratio','price_change_pct','long_checks','short_checks']})
                        p['oi'] = oi_evidence(db,oi.get(name),current,BARS[local['bar']])
                        db.execute('INSERT INTO plans(id,created,expires,plan) VALUES(?,?,?,?)',(p['signal_id'],current,p['expires_at'],json.dumps(p)))
                        if p['quantity'] is not None:
                            db.execute('INSERT OR IGNORE INTO email_outbox(id,created,expires,payload) VALUES(?,?,?,?)',
                                       (p['signal_id'],current,p['expires_at'],json.dumps(p)))
                        report['plans'].append(p)
                report['results'].append(r)
            except (ValueError,KeyError,IndexError,OSError,RuntimeError) as e:
                report['results'].append({'inst_id':name,'signal':False,'reason':'DATA_ERROR:'+type(e).__name__})
            db.execute('INSERT INTO scans VALUES(?,?,?)',(run_id,name,json.dumps(report['results'][-1])))
            db.commit()
        out = Path(out_dir); out.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=out/(run_id+'.json');path.write_text(json.dumps(report,ensure_ascii=False,indent=2));path.chmod(0o600)
        snapshot=out/'watchlist.latest.json';snapshot.write_text(json.dumps(universe,ensure_ascii=False,indent=2));snapshot.chmod(0o600)
        return {'report':str(path),'scanned':len(names),'watchlist_fast':len(universe['members']),
                'candidates':len(report['plans']),'errors':sum(x['reason'].startswith('DATA_ERROR') for x in report['results']),
                'orders_placed':0}
    finally:
        db.close()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',default=str(CONFIG));ap.add_argument('--top',type=int)
    ap.add_argument('--send',action='store_true')
    args=ap.parse_args()
    cfg=load_config(args.config)
    if args.top is not None and not 1<=args.top<=200:
        raise ValueError('INVALID_TOP')
    db_path=ROOT/'data/live-public-signals.sqlite'
    with run_lock(ROOT/'data/live-public-monitor.lock'):
        result=run(cfg,PublicMarket(cfg['proxy']),db_path,ROOT/'reports/live',args.top)
        if args.send:
            from email_alerts import send_live_pending
            result['email']=send_live_pending(db_path,cfg)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'status':'FAILED','error_type':type(e).__name__,'orders_placed':0}))
        raise SystemExit(1)
