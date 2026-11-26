#!/usr/bin/env python3
"""JATA collection server. Public deployment requires TLS, persistent storage and private admin access."""
import argparse, base64, hashlib, hmac, json, os, re, secrets, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from http.cookies import SimpleCookie
ROOT=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--lan',action='store_true');parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8765')));args=parser.parse_args()
FORMAL=os.environ.get('JATA_FORMAL')=='1'
KEY=os.environ.get('JATA_PRIVATE_LINK','')
if args.lan and (not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',KEY)):
 raise SystemExit('公開部署必須設定至少 40 字元的隨機 JATA_PRIVATE_LINK，勿分享給受訪者。')
NOTICE=os.environ.get('JATA_RESEARCH_NOTICE','').strip()
ATTEMPTS={}
AUTH_LOCK=threading.Lock()
DATA=Path(os.environ.get('JATA_DATA_DIR',str(ROOT/'private_data')));DATA.mkdir(parents=True,exist_ok=True,mode=0o700);DB=DATA/'research.sqlite3'
ASSETS={'character','emotion','goods',*(f'v{i}' for i in range(1,15)),*(f'w{i}' for i in range(1,11)),*(f'gift{i}' for i in range(1,6))}
with sqlite3.connect(DB) as c:
 c.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, payload TEXT NOT NULL)');c.execute('CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,name TEXT,mime TEXT,content BLOB,version TEXT)')
with sqlite3.connect(DB) as c:
 c.execute('CREATE TABLE IF NOT EXISTS contacts(number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,email TEXT NOT NULL,status TEXT NOT NULL DEFAULT "pending")')
with sqlite3.connect(DB) as c:
 c.execute('CREATE TABLE IF NOT EXISTS contact_tokens(id TEXT PRIMARY KEY, token TEXT NOT NULL)')
# V9: separate contact data and lottery counter. Older ticket records are preserved.
with sqlite3.connect(DB) as c:
 c.execute('CREATE TABLE IF NOT EXISTS rewards(id TEXT PRIMARY KEY,kind TEXT NOT NULL,email TEXT NOT NULL,gifts TEXT NOT NULL,number INTEGER UNIQUE)')
 c.execute('CREATE TABLE IF NOT EXISTS reward_counter(id INTEGER PRIMARY KEY CHECK(id=1),last INTEGER NOT NULL)')
 c.execute('INSERT OR IGNORE INTO reward_counter VALUES (1,0)')
 c.execute('CREATE TABLE IF NOT EXISTS asset_titles(id TEXT PRIMARY KEY,title TEXT NOT NULL)')
try: os.chmod(DB,0o600)
except OSError: pass
def readiness():
 with sqlite3.connect(DB) as c: uploaded={i for (i,) in c.execute('SELECT id FROM assets')}
 return bool(NOTICE and ASSETS<=uploaded)
def contact_status(id):
 with sqlite3.connect(DB) as c:
  row=c.execute('SELECT kind,number FROM rewards WHERE id=?',(id,)).fetchone()
  if row:return {'rewardKind':row[0],'lotteryNumber':f'{row[1]:04d}' if row[1] is not None else None}
  legacy=c.execute('SELECT number FROM contacts WHERE id=?',(id,)).fetchone()
  return {'rewardKind':'legacy' if legacy else 'none','lotteryNumber':f'{legacy[0]:04d}' if legacy else None}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,fmt,*args): pass
 def respond(self,status,data,mime='application/json'):
  if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(data)
 def auth(self,quiet=False):
  # Local-only process is safe for the researcher; public mode requires the private link's cookie.
  if not args.lan:return True
  try:
   cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));val=cookie['jata_admin'].value if 'jata_admin' in cookie else ''
   if hmac.compare_digest(val,KEY):return True
  except Exception:pass
  if not quiet:self.respond(401,{'error':'private researcher access required'})
  return False
 def admin_login(self, status=200, message=''):
  # Never include the credential in the URL or the returned HTML.
  page='''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>恰塔研究者入口</title><style> *{box-sizing:border-box}body{margin:0;background:#f5e3eb;color:#5b4554;font:16px/1.65 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:20px}.panel{width:min(100%,440px);background:#fff8e9;border:2px solid #cda9bb;border-radius:20px;padding:28px;box-shadow:5px 6px #e7c5d6}h1{font-size:25px;margin:6px 0}small{color:#836579;letter-spacing:2px}label{display:block;font-weight:bold;margin:20px 0 6px}input,button{width:100%;font:inherit;padding:12px;border-radius:10px}input{border:1px solid #bc9dab;background:#fff}button{margin-top:14px;background:#a36e8c;color:#fff;border:0;font-weight:bold;cursor:pointer}.msg{color:#92384e;font-weight:bold}a{color:#80516c} </style></head><body><main class="panel"><small>JATA RESEARCH STUDIO</small><h1>恰塔研究者後台</h1><p>第一次登入請貼上 Railway 的 JATA_PRIVATE_LINK 金鑰。登入後，同一瀏覽器可直接按小恰塔進入後台。</p><form method="post" action="/admin/login"><label for="key">研究者金鑰</label><input id="key" name="key" type="password" required autocomplete="current-password" maxlength="128" autocapitalize="off" spellcheck="false"><button type="submit">登入研究後台</button></form><p class="msg" role="status">MESSAGE</p><a href="/">返回研究首頁</a></main></body></html>'''
  return self.respond(status,page.replace('MESSAGE',message).encode(),'text/html; charset=utf-8')
 def admin_grant(self):
  self.send_response(303);self.send_header('Location','/admin');self.send_header('Set-Cookie','jata_admin='+KEY+'; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800');self.send_header('Referrer-Policy','no-referrer');self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
 def payload(self):
  size=int(self.headers.get('Content-Length','0'))
  if not 0<size<=120*1024*1024:raise ValueError('request size')
  if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('content type')
  return json.loads(self.rfile.read(size))
 def do_GET(self):
  p=urlsplit(self.path).path
  try:
   if p in ('/','/index.html'):return self.respond(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
   if args.lan and p.startswith('/private/') and hmac.compare_digest(p[len('/private/'):],KEY):
    return self.admin_grant()
   if p in ('/admin','/admin.html','/admin/login'):
    if not self.auth(quiet=True):return self.admin_login()
    return self.respond(200,(ROOT/'admin.html').read_bytes(),'text/html; charset=utf-8')
   if p=='/api/status':return self.respond(200,{'app':'JATA-COLLECT-3','ticketMode':'screenshot-reward-v9','formal':FORMAL,'ready':readiness() if FORMAL else False,'notice':NOTICE if FORMAL else ''})
   if p=='/api/assets':
    with sqlite3.connect(DB) as c:
     rows=c.execute('SELECT id,name,version FROM assets').fetchall()
     titles=dict(c.execute('SELECT id,title FROM asset_titles'))
    return self.respond(200,{'assets':{i:{'name':n,'version':v} for i,n,v in rows},'titles':titles})
   if p.startswith('/api/asset/'):
    id=p.rsplit('/',1)[-1]
    with sqlite3.connect(DB) as c: row=c.execute('SELECT mime,content FROM assets WHERE id=?',(id,)).fetchone()
    if not row:return self.respond(404,{'error':'missing'})
    mime,blob=row
    if mime.startswith('video/'):
     total=len(blob);start=0;end=total-1;status=200;range_header=self.headers.get('Range')
     if range_header:
      m=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
      if not m or not (m[1] or m[2]):return self.respond(416,{'error':'range'})
      if m[1]:start=int(m[1]);end=min(end,int(m[2])) if m[2] else end
      else:start=max(0,total-int(m[2]))
      if start>end or start>=total:
       self.send_response(416);self.send_header('Content-Range',f'bytes */{total}');self.send_header('Content-Length','0');self.end_headers();return
      status=206
     self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
     if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{total}')
     self.end_headers();self.wfile.write(blob[start:end+1]);return
    return self.respond(200,blob,mime)
   if p=='/api/admin/check':
    if not self.auth():return
    return self.respond(200,{'ok':True})
   if p=='/api/admin/contacts':
    if not self.auth():return
    with sqlite3.connect(DB) as c:
     rows=c.execute("SELECT id,kind,email,gifts,number FROM rewards WHERE kind!='none' ORDER BY rowid").fetchall()
     legacy=c.execute('SELECT number,id FROM contacts WHERE id NOT IN (SELECT id FROM rewards)').fetchall()
    return self.respond(200,{'contacts':[{'id':i,'kind':k,'email':e,'gifts':json.loads(g),'number':n} for i,k,e,g,n in rows]+[{'id':i,'kind':'legacy','number':n,'email':'','gifts':[]} for n,i in legacy]})
   if p=='/api/admin/audio':
    if not self.auth():return
    q=parse_qs(urlsplit(self.path).query); rid=q.get('id',[''])[0]; key=q.get('key',[''])[0]
    with sqlite3.connect(DB) as c: row=c.execute('SELECT payload FROM records WHERE id=?',(rid,)).fetchone()
    if not row:return self.respond(404,{'error':'missing'})
    val=json.loads(row[0]).get('audio',{}).get(key,'')
    match=re.fullmatch(r'data:(audio/[A-Za-z0-9.+-]+(?:;codecs=[A-Za-z0-9.+-]+)?);base64,([A-Za-z0-9+/=]+)',val)
    if not match:return self.respond(404,{'error':'missing'})
    return self.respond(200,base64.b64decode(match[2]),match[1])
   if p=='/api/admin/records':
    if not self.auth():return
    with sqlite3.connect(DB) as c: rows=c.execute('SELECT payload FROM records ORDER BY rowid').fetchall()
    rs=[];audio={}
    for (raw,) in rows:
     d=json.loads(raw);rs.extend(d['records'])
     if parse_qs(urlsplit(self.path).query).get('summary')!=['1']:audio.update(d.get('audio',{}))
    return self.respond(200,{'format':'JATA-RESEARCH-3','records':rs,'audio':audio})
   return self.respond(404,{'error':'not found'})
  except Exception:self.respond(500,{'error':'server error'})
 def do_POST(self):
  p=urlsplit(self.path).path
  try:
   if p=='/admin/login':
    if not args.lan:return self.admin_grant() if KEY else self.respond(400,{'error':'local mode: open /admin'})
    if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.admin_login(403,'請從本站的登入頁操作。')
    n=int(self.headers.get('Content-Length','0'))
    if not 0<n<=1024 or self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':return self.admin_login(400,'請從本站的登入頁輸入金鑰。')
    val=parse_qs(self.rfile.read(n).decode('utf-8'),keep_blank_values=True).get('key',[''])[0].strip()
    # Limit failed attempts globally without relying on spoofable proxy headers.
    with AUTH_LOCK:
     now=time.monotonic();recent=[t for t in ATTEMPTS.get('admin',[]) if now-t<60]
     ATTEMPTS['admin']=recent
     if len(recent)>=12:return self.admin_login(429,'嘗試次數過多，請一分鐘後再試。')
     if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',val) or not hmac.compare_digest(val,KEY):
      recent.append(now);return self.admin_login(401,'金鑰不正確，請確認 Railway 的 JATA_PRIVATE_LINK 設定。')
     ATTEMPTS.pop('admin',None)
    return self.admin_grant()
   if p=='/api/submit':
    if FORMAL and not readiness():return self.respond(503,{'error':'research not open'})
    d=self.payload();rs=d.get('records',[])
    if d.get('format')!='JATA-RESEARCH-3' or len(rs)!=1:raise ValueError('format')
    r=rs[0];id=r.get('id','');answers=r.get('answers',{});survey=r.get('survey',{})
    if not re.fullmatch(r'JATA-[A-Z0-9-]{1,80}',id) or r.get('mode') not in ('interview','survey') or not r.get('consent',{}).get('participation') or not r.get('completedAt') or not isinstance(answers,dict) or not isinstance(survey,dict):raise ValueError('record')
    if FORMAL:
     expected_survey={f'P{i:02d}' for i in range(1,14)}
     expected_interview={f'Q{i:02d}' for i in list(range(1,6))+list(range(10,31))}|{f'Q{6+i%4:02d}_V{i+1}' for i in range(8)}|{f'S{i:02d}' for i in range(1,8)}
     group=survey if r['mode']=='survey' else answers;expected=expected_survey if r['mode']=='survey' else expected_interview
     if not all(isinstance(group.get(k),dict) and group[k].get('status')=='answered' for k in expected):raise ValueError('incomplete')
     if r['mode']=='interview' and r.get('consent',{}).get('version')=='pixel-v8':
      for k in (f'Q{6+i%4:02d}_V{i+1}' for i in range(8)):
       a=answers[k]
       if a['status']=='answered' and len(re.sub(r'[^\w]','',str(a.get('follow',''))))<4:raise ValueError('missing second main answer')
      for k in ('Q19','Q20','Q21'):
       a=answers[k]
       if a['status']=='answered' and not a.get('shopChoices'):raise ValueError('missing shop choice')
    version=r.get('consent',{}).get('version')
    if version not in ('pixel-v9','pixel-v13'):raise ValueError('version')
    if version=='pixel-v13' and r['mode']=='interview':
     for k in ('Q14','Q15','Q16','Q17','Q18'):
      a=answers.get(k,{})
      if not isinstance(a,dict) or a.get('status')!='answered' or not isinstance(a.get('follow'),str) or sum(ch.isalnum() for ch in a['follow'])<4:raise ValueError('missing social main answer')
    reward=d.get('reward',{});kind=reward.get('kind');email=reward.get('email','');gifts=reward.get('gifts',[])
    if kind not in ('none','gift','lottery') or not isinstance(email,str) or not isinstance(gifts,list):raise ValueError('reward')
    if kind=='none':
     if email or gifts:raise ValueError('skip')
    else:
     if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('email')
     if kind=='gift':
      if r['mode']!='interview' or len(gifts)!=2 or any(not isinstance(g,str) or g not in {f'gift{i}' for i in range(1,6)} for g in gifts) or len(set(gifts))!=2:raise ValueError('gifts')
     elif r['mode']!='survey' or gifts:raise ValueError('lottery')
    if FORMAL and r['mode']=='interview':
     for k in ('Q19','Q20','Q21'):
      a=answers[k]
      if a['status']=='answered' and not a.get('shopChoices'):raise ValueError('missing shop choice')
    audio={};allowed={f'audio:{id}:{k}' for k,a in answers.items() if isinstance(a,dict) and a.get('audio')==f'audio:{id}:{k}'}
    if r.get('consent',{}).get('recording'):
     for k,v in d.get('audio',{}).items():
      if k in allowed and isinstance(v,str) and re.match(r'^data:audio/[\w.+-]+(?:;codecs=[\w.+-]+)?;base64,',v) and len(v)<=42*1024*1024:audio[k]=v
    client_token=d.get('contactToken','');client_token=client_token if isinstance(client_token,str) else '';r.pop('contactEmail',None);r['consent'].pop('contact',None)
    d={'format':'JATA-RESEARCH-3','records':[r],'audio':audio}
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT id FROM records WHERE id=?',(id,)).fetchone()
     if not old:
      c.execute('INSERT INTO records VALUES (?,?)',(id,json.dumps(d,ensure_ascii=False)))
      number=None
      if kind=='lottery':
       c.execute('UPDATE reward_counter SET last=last+1 WHERE id=1');number=c.execute('SELECT last FROM reward_counter WHERE id=1').fetchone()[0]
      c.execute('INSERT INTO rewards VALUES (?,?,?,?,?)',(id,kind,email,json.dumps(gifts),number))
     tokenrow=c.execute('SELECT token FROM contact_tokens WHERE id=?',(id,)).fetchone()
     # Token is issued only on first submission; authenticated duplicate contact requests use this token.
     requested=d.get('contactToken','')
     # d has been normalized above; read token from a separately captured request field.
     requested=client_token
     if not old:
      token=requested if re.fullmatch(r'[0-9a-f]{64}',requested) else secrets.token_urlsafe(32)
      c.execute('INSERT OR REPLACE INTO contact_tokens VALUES (?,?)',(id,token))
     else:
      if not tokenrow or not hmac.compare_digest(tokenrow[0],requested):return self.respond(409,{'error':'submission already exists'})
      token=requested
    return self.respond(200,{'saved':True,'id':id,**contact_status(id),'contactToken':token})
   if p=='/api/contact/status':
    d=self.payload();id=d.get('id','');token=d.get('token','')
    if not isinstance(id,str) or not isinstance(token,str):raise ValueError('token')
    with sqlite3.connect(DB) as c:row=c.execute('SELECT token FROM contact_tokens WHERE id=?',(id,)).fetchone()
    if not row or not hmac.compare_digest(row[0],token):return self.respond(403,{'error':'invalid token'})
    return self.respond(200,contact_status(id))
   if p=='/api/admin/assets/title':
    if not self.auth():return
    d=self.payload();id=d.get('id');title=d.get('title')
    if id not in ASSETS or not isinstance(title,str) or not 1<=len(title.strip())<=100:raise ValueError('title')
    with sqlite3.connect(DB) as c:c.execute('INSERT OR REPLACE INTO asset_titles VALUES (?,?)',(id,title.strip()))
    return self.respond(200,{'ok':True})
   if p=='/api/admin/records/clear':
    if not self.auth():return
    if self.payload().get('confirm')!='CLEAR_ALL_RESEARCH':raise ValueError('confirmation')
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN IMMEDIATE')
     for table in ('records','contacts','contact_tokens','rewards'):c.execute(f'DELETE FROM {table}')
     c.execute('UPDATE reward_counter SET last=0 WHERE id=1')
     c.execute("DELETE FROM sqlite_sequence WHERE name='contacts'")
    return self.respond(200,{'ok':True})
   if p=='/api/admin/assets/remove':
    if not self.auth():return
    id=self.payload().get('id');
    if id not in ASSETS:raise ValueError('asset')
    with sqlite3.connect(DB) as c:c.execute('DELETE FROM assets WHERE id=?',(id,))
    return self.respond(200,{'ok':True})
   if p=='/api/admin/records/remove':
    if not self.auth():return
    id=self.payload().get('id');
    if not isinstance(id,str):raise ValueError('id')
    with sqlite3.connect(DB) as c:
     for table in ('records','contacts','contact_tokens','rewards'):c.execute(f'DELETE FROM {table} WHERE id=?',(id,))
    return self.respond(200,{'ok':True})
   if p=='/api/admin/assets':
    if not self.auth():return
    d=self.payload();rows=[]
    for id,obj in d.get('assets',{}).items():
     if id not in ASSETS:continue
     u=obj.get('data','');match=re.fullmatch(r'data:(image/(?:png|jpeg|webp|gif)|video/(?:mp4|webm|quicktime));base64,([A-Za-z0-9+/=\r\n]+)',u)
     if not match:raise ValueError('use PNG, JPEG, WebP, GIF, MP4 or WebM')
     mime=match[1];blob=base64.b64decode(match[2],validate=True)
     if (id.startswith('v') and not mime.startswith('video/')) or (not id.startswith('v') and not mime.startswith('image/')):raise ValueError('type')
     rows.append((id,str(obj.get('name',id))[:200],mime,blob,hashlib.sha256(blob).hexdigest()[:16]))
    with sqlite3.connect(DB) as c: c.executemany('INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?)',rows)
    return self.respond(200,{'saved':len(rows)})
   return self.respond(404,{'error':'not found'})
  except (ValueError,TypeError,KeyError,AttributeError):self.respond(400,{'error':'invalid request'})
  except Exception:self.respond(500,{'error':'server error'})
if __name__=='__main__':
 print(f'參與者網站：http://127.0.0.1:{args.port}/\n研究後台：'+('請使用專屬連結 /private/<JATA_PRIVATE_LINK>' if args.lan else f'http://127.0.0.1:{args.port}/admin')+f'\n資料位置：{DB}',flush=True)
 ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1',args.port),Handler).serve_forever()
