import json
from pathlib import Path
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
CREDENTIALS=Path.home()/".okx/alerts-smtp.json"

def live_message(plan, sender, recipient):
    for address in [sender,recipient]:
        if not re.fullmatch(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+',address):
            raise ValueError('INVALID_EMAIL')
    if plan.get('environment') != 'live_public' or plan.get('quantity') is None:
        raise ValueError('LIVE_PUBLIC_PLAN_REQUIRED')
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expires=datetime.fromtimestamp(plan['expires_at']/1000,ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间')
    m=EmailMessage();m['From']=sender;m['To']=recipient
    side='做多' if plan['side']=='long' else '做空'
    m['Subject']=f"[OKX实盘行情·手动参考] {plan['inst_id']} {side}"
    m.set_content(f"""实盘公开行情监控；未访问交易账户、未下单。
标的：{plan['inst_id']}；方向：{side}
趋势周期：{plan['trend']['bar']}；交易周期：{plan['entry_bar']}
参考入场：{plan['entry_reference']}；止损：{plan['stop']}；目标：{plan['target']}
单笔参考数量：{plan['quantity']} 张；名义金额：{plan.get('notional_usdt',0):.2f} U
单笔计划风险含成本估计：{plan.get('risk_usdt',0):.2f} U；净盈亏比估计：{plan.get('net_reward_risk',0):.2f}
有效期：{expires}

下单原因：五项量价条件同时满足，并与高周期方向一致。
交易周期具体指标：{json.dumps(plan.get('reason_details',{}),ensure_ascii=False)}
趋势指标：{json.dumps(plan['trend'],ensure_ascii=False)}
OI证据：{json.dumps(plan.get('oi',{}),ensure_ascii=False)}

数量只基于200U独立预算和2%单笔风险测算；费率0.05%为配置估计，不是账户真实费率。
未核对你现有持仓、挂单和保护单，不代表账户剩余可开数量；总风险8%、相关6%、名义100%、日亏8%需结合账户另行核对，不是本公开行情监控已自动执行的限制。
禁止将多封邮件数量直接相加下单。请手动核对实时报价、实际费率、已有敞口与强平价；止损不保证成交损失上限。
信号不是已验证的高胜率结论。此程序不会下单、平仓或调整杠杆。
信号编号：{plan['signal_id']}；版本：{plan['version']}
""")
    return m

def send_live_pending(db_path,cfg):
    if cfg.get('environment')!='live_public' or cfg.get('execution')!='MANUAL_ALERT_ONLY':
        raise ValueError('PUBLIC_MONITOR_ONLY')
    from workflow import connect
    db=connect(db_path)
    try:
        db.execute("UPDATE email_outbox SET state='EXPIRED' WHERE state='PENDING' AND expires<=?",(int(time.time()*1000),));db.commit()
        if not cfg.get('notifications',{}).get('enabled'):
            return {'sent':0,'blocked':'EMAIL_DISABLED'}
        if not CREDENTIALS.is_file() or CREDENTIALS.is_symlink() or CREDENTIALS.stat().st_mode & 0o077:
            return {'sent':0,'blocked':'PRIVATE_SMTP_CREDENTIAL_REQUIRED'}
        cred=json.loads(CREDENTIALS.read_text())
        if cred.get('sender') != cfg['notifications']['recipient']:
            raise ValueError('UNREVIEWED_EMAIL_DESTINATION')
        pending=db.execute("SELECT id,payload FROM email_outbox WHERE state='PENDING' ORDER BY created").fetchall()
        sent=0
        for identity,payload in pending:
            plan=json.loads(payload)
            if plan.get('environment')!='live_public' or plan.get('quantity') is None:
                continue
            m=live_message(plan,cred['sender'],cfg['notifications']['recipient'])
            claim=db.execute("UPDATE email_outbox SET state='SENDING' WHERE id=? AND state='PENDING' AND expires>?",(identity,int(time.time()*1000)));db.commit()
            if claim.rowcount!=1:
                continue
            try:
                with smtplib.SMTP_SSL('smtp.qq.com',465,timeout=20,context=ssl.create_default_context()) as smtp:
                    smtp.login(cred['sender'],cred['authorization_code'])
                    refused=smtp.send_message(m)
                    if refused:
                        raise smtplib.SMTPException('RECIPIENT_REFUSED')
            except (OSError,smtplib.SMTPException):
                db.execute("UPDATE email_outbox SET state='UNCERTAIN' WHERE id=?",(identity,));db.commit()
                return {'sent':sent,'blocked':'SMTP_UNCERTAIN_NO_AUTOMATIC_RETRY'}
            db.execute("UPDATE email_outbox SET state='SENT' WHERE id=?",(identity,));db.commit();sent+=1
        return {'sent':sent,'pending_initial':len(pending)}
    finally:
        db.close()
