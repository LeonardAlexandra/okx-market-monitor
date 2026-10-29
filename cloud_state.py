"""Encrypted cloud checkpoints; no secrets or plaintext artifacts are printed."""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import zipfile
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parent
DB=ROOT/'data/live-public-signals.sqlite'


def gh_json(args):
    p=subprocess.run(['gh','api',*args],capture_output=True,text=True,timeout=45)
    if p.returncode:raise RuntimeError('GITHUB_API_FAILED')
    return json.loads(p.stdout)


def latest(repo):
    page=gh_json([f'repos/{repo}/actions/artifacts?per_page=100'])
    items=[x for x in page['artifacts'] if x['name'].startswith('state-') and not x['expired']]
    if not items:return None
    return max(items,key=lambda x:(x['created_at'],x['id']))


def decrypt_to(token,key,folder):
    raw=Fernet(key.encode()).decrypt(token)
    if len(raw)>100000000:raise ValueError('STATE_TOO_LARGE')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            p=Path(item.filename)
            if p.is_absolute() or '..' in p.parts or p.parts[0] not in ['data','reports']:
                raise ValueError('UNSAFE_STATE_PATH')
            target=folder/p;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            target.write_bytes(archive.read(item));target.chmod(0o600)


def fetch_state(repo,key,folder):
    item=latest(repo)
    if not item:return False
    p=subprocess.run(['gh','api',f"repos/{repo}/actions/artifacts/{item['id']}/zip"],capture_output=True,timeout=60)
    if p.returncode:raise RuntimeError('ARTIFACT_DOWNLOAD_FAILED')
    with zipfile.ZipFile(io.BytesIO(p.stdout)) as z:
        names=z.namelist()
        if names!=['state.enc']:raise ValueError('UNEXPECTED_ARTIFACT_CONTENTS')
        decrypt_to(z.read('state.enc'),key,folder)
    return True


def save():
    buffer=io.BytesIO()
    if DB.exists():
        if not os.environ.get('MARKET_REPOSITORY'):
            hot=sqlite3.connect(DB);cutoff=int(time.time()*1000)-2*86400000
            hot.execute('DELETE FROM market_snapshots WHERE ts<?',(cutoff,))
            hot.execute('DELETE FROM scans WHERE run_id IN (SELECT id FROM runs WHERE ts<?)',(cutoff,))
            hot.execute('DELETE FROM runs WHERE ts<?',(cutoff,))
            hot.execute('DELETE FROM oi WHERE ts<?',(cutoff,))
            old=int(time.time()*1000)-7*86400000
            hot.execute('DELETE FROM email_outbox WHERE expires<?',(old,))
            hot.execute('DELETE FROM plans WHERE expires<?',(old,))
            hot.commit();hot.close()
            for report in (ROOT/'reports/live').glob('*.json'):
                if report.name=='watchlist.latest.json':continue
                try:
                    ts=datetime.strptime(report.stem.split('-')[0],'%Y%m%dT%H%M%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000
                    if ts<cutoff:report.unlink()
                except ValueError:pass
        source=sqlite3.connect(DB);backup=sqlite3.connect(ROOT/'data/checkpoint.sqlite')
        source.backup(backup);backup.close();source.close()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        if DB.exists():z.write(ROOT/'data/checkpoint.sqlite','data/live-public-signals.sqlite')
        # Account review JSON/Markdown stays encrypted even in public repository artifacts.
        for p in (ROOT/'reports').rglob('*') if (ROOT/'reports').exists() else []:
            if p.is_file():z.write(p,str(p.relative_to(ROOT)))
    (ROOT/'state.enc').write_bytes(Fernet(os.environ['STATE_ENCRYPTION_KEY'].encode()).encrypt(buffer.getvalue()))


def restore():
    repo=os.environ['GITHUB_REPOSITORY']
    if not fetch_state(repo,os.environ['STATE_ENCRYPTION_KEY'],ROOT):
        if os.environ.get('ALLOW_BOOTSTRAP')!='true':raise RuntimeError('STATE_MISSING_FAIL_CLOSED')
        (ROOT/'data').mkdir(exist_ok=True)
    if os.environ.get('MARKET_REPOSITORY'):
        temp=ROOT/'market-import';temp.mkdir(exist_ok=True)
        if not fetch_state(os.environ['MARKET_REPOSITORY'],os.environ['MARKET_STATE_KEY'],temp):
            raise RuntimeError('MARKET_STATE_MISSING')
        src=sqlite3.connect(temp/'data/live-public-signals.sqlite');dst=sqlite3.connect(DB)
        for table in ['runs','scans','oi','plans','email_outbox','universe','market_snapshots']:
            schema=src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if not schema:continue
            dst.execute(schema[0].replace('CREATE TABLE ','CREATE TABLE IF NOT EXISTS ',1))
            rows=src.execute('SELECT * FROM '+table).fetchall()
            if rows:dst.executemany('INSERT OR REPLACE INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        dst.commit();dst.close();src.close()


def configure():
    if os.environ.get('SMTP_SENDER'):
        folder=Path.home()/'.okx';folder.mkdir(mode=0o700,exist_ok=True)
        path=folder/'alerts-smtp.json'
        path.write_text(json.dumps({'sender':os.environ['SMTP_SENDER'],'authorization_code':os.environ['SMTP_AUTH_CODE']}));path.chmod(0o600)
    cfg=json.loads((ROOT/'monitor.live.json').read_text())
    cfg['proxy']=None
    cfg['notifications']['recipient']=os.environ.get('SMTP_RECIPIENT','')
    cfg['notifications']['enabled']=bool(os.environ.get('SMTP_AUTH_CODE'))
    cfg['risk']['equity_usdt']=float(os.environ.get('STRATEGY_CAPITAL_USDT','200'))
    (ROOT/'monitor.live.json').write_text(json.dumps(cfg))


def claim(kind):
    configure();db=sqlite3.connect(DB);batch=[]
    if kind=='market':
        batch=[r[0] for r in db.execute("SELECT id FROM email_outbox WHERE state='PENDING' AND expires>?",(int(time.time()*1000),))]
        for ident in batch:db.execute("UPDATE email_outbox SET state='SENDING' WHERE id=? AND state='PENDING'",(ident,))
    else:
        keys=db.execute('SELECT id FROM daily_reviews ORDER BY end DESC LIMIT 1').fetchall()
        db.execute('CREATE TABLE IF NOT EXISTS daily_delivery(id TEXT PRIMARY KEY,state TEXT)')
        for (ident,) in keys:
            if db.execute("INSERT OR IGNORE INTO daily_delivery VALUES(?,'SENDING')",(ident,)).rowcount:batch.append(ident)
    db.commit();db.close();(ROOT/'current-batch.json').write_text(json.dumps({'kind':kind,'ids':batch}))


def send(kind):
    configure();batch=json.loads((ROOT/'current-batch.json').read_text())
    if batch['kind']!=kind:raise ValueError('WRONG_BATCH')
    db=sqlite3.connect(DB)
    if kind=='market':
        for ident in batch['ids']:db.execute("UPDATE email_outbox SET state='PENDING' WHERE id=? AND state='SENDING'",(ident,))
        db.commit();db.close()
        from email_alerts import send_live_pending
        result=send_live_pending(DB,json.loads((ROOT/'monitor.live.json').read_text()))
    else:
        from daily_review import deliver,markdown
        result={'sent':False}
        for ident in batch['ids']:
            row=db.execute('SELECT report FROM daily_reviews WHERE id=?',(ident,)).fetchone()
            if not row:raise RuntimeError('REVIEW_MISSING')
            db.execute("DELETE FROM daily_delivery WHERE id=? AND state='SENDING'",(ident,));db.commit()
            result=deliver(db,ident,markdown(json.loads(row[0])),os.environ['SMTP_RECIPIENT'])
        db.close()
    print(json.dumps(result))
    if result.get('blocked') and result['blocked']!='ALREADY_ATTEMPTED_NO_AUTOMATIC_RESEND':raise RuntimeError('DELIVERY_BLOCKED')


def prune():
    repo=os.environ['GITHUB_REPOSITORY']
    items=gh_json([f'repos/{repo}/actions/artifacts?per_page=100'])['artifacts']
    states=sorted([x for x in items if x['name'].startswith('state-')],key=lambda x:(x['created_at'],x['id']),reverse=True)
    # Keep ten full encrypted checkpoints; all unique history remains in the newest database.
    for item in states[10:]:
        p=subprocess.run(['gh','api','--method','DELETE',f"repos/{repo}/actions/artifacts/{item['id']}"],capture_output=True)
        if p.returncode:raise RuntimeError('OLD_CHECKPOINT_CLEANUP_FAILED')


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['restore','configure','save','claim','send','prune']);ap.add_argument('--kind',choices=['market','daily'],default='market');a=ap.parse_args()
    try:
        if a.action=='restore':restore()
        elif a.action=='configure':configure()
        elif a.action=='save':save()
        elif a.action=='claim':claim(a.kind)
        elif a.action=='prune':prune()
        else:send(a.kind)
    except Exception as e:
        print(json.dumps({'status':'FAILED','error_type':type(e).__name__}));raise SystemExit(1)
