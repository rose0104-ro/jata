#!/usr/bin/env python3
"""JATA collection server. Public deployment requires TLS, persistent storage and private admin access."""
import argparse, base64, hashlib, hmac, json, os, re, secrets, sqlite3, threading, time, io, zipfile, datetime
from xml.sax.saxutils import escape
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
 c.execute('UPDATE reward_counter SET last=MAX(last,COALESCE((SELECT MAX(number) FROM contacts),0),COALESCE((SELECT MAX(number) FROM rewards),0)) WHERE id=1')
 c.execute('CREATE TABLE IF NOT EXISTS interview_slots(id TEXT PRIMARY KEY)')
 c.execute("INSERT OR IGNORE INTO interview_slots SELECT id FROM records WHERE json_extract(payload,'$.records[0].mode')='interview'")
 c.execute('CREATE TABLE IF NOT EXISTS asset_titles(id TEXT PRIMARY KEY,title TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS reward_settings(id INTEGER PRIMARY KEY CHECK(id=1),gift_limit INTEGER NOT NULL,winner_count INTEGER)')
 c.execute('INSERT OR IGNORE INTO reward_settings VALUES (1,10,NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS lottery_winners(id TEXT PRIMARY KEY, number INTEGER UNIQUE NOT NULL, drawn_at TEXT NOT NULL, round TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS transcripts(id TEXT NOT NULL, question TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(id,question))')
 c.execute('CREATE TABLE IF NOT EXISTS live_transcribe_usage(day TEXT NOT NULL, journey TEXT NOT NULL, question TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,journey,question))')
 c.execute('CREATE TABLE IF NOT EXISTS live_transcribe_secret(id INTEGER PRIMARY KEY, secret TEXT NOT NULL)')
 c.execute('INSERT OR IGNORE INTO live_transcribe_secret VALUES (1,?)',(secrets.token_hex(32),))
 LIVE_SECRET=c.execute('SELECT secret FROM live_transcribe_secret WHERE id=1').fetchone()[0].encode()
try: os.chmod(DB,0o600)
except OSError: pass
def readiness():
 with sqlite3.connect(DB) as c: uploaded={i for (i,) in c.execute('SELECT id FROM assets')}
 return bool(NOTICE and ASSETS<=uploaded)
def participation_counts(c):
 counts=dict(c.execute("SELECT json_extract(payload,'$.records[0].mode'),COUNT(*) FROM records GROUP BY 1"))
 i=counts.get('interview',0); q=counts.get('survey',0); slots=c.execute("SELECT COUNT(*) FROM rewards WHERE kind='gift'").fetchone()[0]
 limit,winners=c.execute('SELECT gift_limit,winner_count FROM reward_settings WHERE id=1').fetchone()
 return {'interviewCompleted':i,'surveyCompleted':q,'totalCompleted':i+q,'giftLimit':limit,'giftIssued':slots,'giftRemaining':max(0,limit-slots),'lotteryWinnerCount':winners}
def public_counts():
 with sqlite3.connect(DB) as c:return participation_counts(c)
def contact_status(id):
 with sqlite3.connect(DB) as c:
  row=c.execute('SELECT kind,number FROM rewards WHERE id=?',(id,)).fetchone()
  if row:return {'rewardKind':row[0],'lotteryNumber':f'{row[1]:04d}' if row[1] is not None else None}
  legacy=c.execute('SELECT number FROM contacts WHERE id=?',(id,)).fetchone()
  return {'rewardKind':'legacy' if legacy else 'none','lotteryNumber':f'{legacy[0]:04d}' if legacy else None}
def snapshot(c):
 rs=[]
 for (raw,) in c.execute('SELECT payload FROM records ORDER BY rowid'):
  d=json.loads(raw);rs.extend(d['records'])
 lookup={r['id']:r for r in rs}
 for rid,k,t in c.execute('SELECT id,question,text FROM transcripts'):
  if rid in lookup and k in lookup[rid].get('answers',{}):lookup[rid]['answers'][k]['transcript']=t
 return rs

def draw_state(c):
 target=c.execute('SELECT winner_count FROM reward_settings WHERE id=1').fetchone()[0]
 eligible=c.execute("SELECT COUNT(*) FROM rewards WHERE kind='lottery' AND email<>'' AND number IS NOT NULL").fetchone()[0]
 rows=c.execute("SELECT w.id,w.number,w.drawn_at,w.round,r.email,json_extract(d.payload,'$.records[0].answers.S01.text') FROM lottery_winners w LEFT JOIN rewards r ON r.id=w.id LEFT JOIN records d ON d.id=w.id ORDER BY w.drawn_at,w.number").fetchall()
 return {'target':target,'eligible':eligible,'remaining':max(0,(target or 0)-len(rows)),'winners':[{'id':i,'number':n,'drawnAt':t,'round':b,'email':e or '', 'nickname':nick or ''} for i,n,t,b,e,nick in rows]}

def xlsx(sheets):
 # Native XLSX, strings stored as inline text (never interpreted as formulas).
 def col(n):
  out=''
  while n:n,a=divmod(n-1,26);out=chr(65+a)+out
  return out
 def txt(v):
  if isinstance(v,(dict,list)):v=json.dumps(v,ensure_ascii=False)
  return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(v if v is not None else ''))[:32767])
 ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main';rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships';pkg='http://schemas.openxmlformats.org/package/2006/relationships'
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))+'</Types>')
  z.writestr('_rels/.rels',f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
  z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{pkg}">'+''.join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+f'<Relationship Id="rId{len(sheets)+1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
  z.writestr('xl/styles.xml',f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF80566F"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
  for i,(name,rows) in enumerate(sheets,1):
   width=max((len(r) for r in rows),default=1);cells=[]
   for j,row in enumerate(rows,1):
    cells.append(f'<row r="{j}">'+''.join((f'<c r="{col(k)}{j}"><v>{v}</v></c>' if type(v) in (int,float) else f'<c r="{col(k)}{j}" s="{int(j==1)}" t="inlineStr"><is><t xml:space="preserve">{txt(v)}</t></is></c>') for k,v in enumerate(row,1))+'</row>')
   z.writestr(f'xl/worksheets/sheet{i}.xml',f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="{width}" width="25" customWidth="1"/></cols><sheetData>'+''.join(cells)+f'</sheetData><autoFilter ref="A1:{col(width)}{max(1,len(rows))}"/></worksheet>')
 return out.getvalue()

def research_sheets(rs):
 keys=sorted({k for r in rs for k in list(r.get('answers',{}))+list(r.get('survey',{}))})
 def val(a):
  if not isinstance(a,dict):return str(a or '')
  return '\n'.join(f'{k}: '+(json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else str(v)) for k,v in a.items() if k not in ('status','at','questionText','questionVersion') and v not in (None,'',[],{}))
 numeric=sorted({(k,sub) for r in rs for k,a in r.get('survey',{}).items() if isinstance(a,dict) and isinstance(a.get('value'),dict) for sub,v in a['value'].items() if type(v) in (int,float) or isinstance(v,dict) and type(v.get('rating')) in (int,float)}|{(k,'rating') for r in rs for k,a in r.get('answers',{}).items() if isinstance(a,dict) and type(a.get('rating')) in (int,float)})
 def score(r,k,sub):
  if sub=='rating':return r.get('answers',{}).get(k,{}).get('rating')
  v=r.get('survey',{}).get(k,{}).get('value',{});v=v.get(sub) if isinstance(v,dict) else None;return v.get('rating') if isinstance(v,dict) else v
 wide=[['研究編號','模式','版本','分組','完成時間']+keys+[k+'_'+sub+'（數值）' for k,sub in numeric]];long=[['研究編號','模式','題號','題目','狀態','原始回答','錄音檔代碼','AI逐字稿']]
 for r in rs:
  a={**r.get('answers',{}),**r.get('survey',{})};wide.append([r['id'],r['mode'],r.get('consent',{}).get('version'),r.get('group'),r.get('completedAt')]+[val(a.get(k)) for k in keys]+[score(r,k,sub) for k,sub in numeric])
  for k,v in a.items():
   if isinstance(v,dict):long.append([r['id'],r['mode'],k,v.get('questionText',''),v.get('status',''),val({x:y for x,y in v.items() if x not in ('audio','transcript')}),v.get('audio',''),v.get('transcript','')])
 return [('回答總表',wide),('逐題明細',long)]

def transcript_receipt(rid,k,data,text):
 return hmac.new(LIVE_SECRET,json.dumps([rid,k,hashlib.sha256(data.encode()).hexdigest(),text],ensure_ascii=False,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

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
  page='''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>恰塔研究者入口</title><style> *{box-sizing:border-box}body{margin:0;background:#f5e3eb;color:#5b4554;font:16px/1.65 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:20px}.panel{width:min(100%,440px);background:#fff8e9;border:2px solid #cda9bb;border-radius:20px;padding:28px;box-shadow:5px 6px #e7c5d6}h1{font-size:25px;margin:6px 0}small{color:#836579;letter-spacing:2px}label{display:block;font-weight:bold;margin:20px 0 6px}input,button{width:100%;font:inherit;padding:12px;border-radius:10px}input{border:1px solid #bc9dab;background:#fff}button{margin-top:14px;background:#a36e8c;color:#fff;border:0;font-weight:bold;cursor:pointer}.msg{color:#92384e;font-weight:bold}a{color:#80516c} </style></head><body><main class="panel"><small>JATA RESEARCH STUDIO</small><h1>恰塔研究者後台</h1><p>第一次登入請貼上 Railway 的 JATA_PRIVATE_LINK 金鑰。登入後，同一瀏覽器可直接按小恰塔進入後台（最多保留 30 天，清除瀏覽資料後須重新登入）。</p><form method="post" action="/admin/login"><label for="key">研究者金鑰</label><input id="key" name="key" type="password" required autocomplete="current-password" maxlength="128" autocapitalize="off" spellcheck="false"><button type="submit">登入研究後台</button></form><p class="msg" role="status">MESSAGE</p><a href="/">返回研究首頁</a></main></body></html>'''
  return self.respond(status,page.replace('MESSAGE',message).encode(),'text/html; charset=utf-8')
 def admin_grant(self):
  self.send_response(303);self.send_header('Location','/admin');self.send_header('Set-Cookie','jata_admin='+KEY+'; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000');self.send_header('Referrer-Policy','no-referrer');self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
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
   if p=='/api/status':return self.respond(200,{'app':'JATA-COLLECT-3','ticketMode':'shared-lottery-v16','formal':FORMAL,'ready':readiness() if FORMAL else False,'notice':NOTICE if FORMAL else '',**public_counts()})
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
   if p=='/api/admin/draw':
    if not self.auth():return
    with sqlite3.connect(DB) as c:return self.respond(200,draw_state(c))
   if p=='/api/admin/audio.zip':
    if not self.auth():return
    out=io.BytesIO()
    with sqlite3.connect(DB) as c,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
     for rid,raw in c.execute('SELECT id,payload FROM records'):
      d=json.loads(raw);r=d['records'][0]
      for k,a in r.get('answers',{}).items():
       if not isinstance(a,dict) or not re.fullmatch(r'[A-Z0-9_-]+',k):continue
       v=d.get('audio',{}).get(a.get('audio'),'');m=re.fullmatch(r'data:(audio/[\w.+-]+)(?:;codecs=[\w.+-]+)?;base64,([A-Za-z0-9+/=]+)',v)
       if m:
        ext={'audio/mp4':'m4a','audio/mpeg':'mp3','audio/ogg':'ogg','audio/wav':'wav','audio/x-wav':'wav','audio/x-m4a':'m4a'}.get(m[1],'webm');z.writestr(f'{rid}/{k}.{ext}',base64.b64decode(m[2]))
    return self.respond(200,out.getvalue(),'application/zip')
   if p in ('/api/admin/export.xlsx','/api/admin/rewards.xlsx'):
    if not self.auth():return
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN')
     if p.endswith('rewards.xlsx'):
      rows=c.execute("SELECT w.id,w.kind,w.email,w.number,w.gifts,json_extract(r.payload,'$.records[0].mode') FROM rewards w JOIN records r ON r.id=w.id WHERE w.kind!='none' ORDER BY w.rowid").fetchall()
      winners=draw_state(c)['winners'];sheets=[('贈禮及抽獎名單',[['研究編號','類別','Email','抽獎號碼','款式','模式']]+[[i,k,e,f'{n:04d}' if n else '',g,m] for i,k,e,n,g,m in rows]),('中獎紀錄',[['抽獎號碼','研究編號','暱稱','Email','抽出時間']]+[[f"{w['number']:04d}",w['id'],w['nickname'],w['email'],w['drawnAt']] for w in winners])]
     else:
      rs=snapshot(c);rid=parse_qs(urlsplit(self.path).query).get('id',[''])[0];sheets=research_sheets([r for r in rs if not rid or r['id']==rid])
    return self.respond(200,xlsx(sheets),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
   if p=='/api/admin/check':
    if not self.auth():return
    return self.respond(200,{'ok':True})
   if p=='/api/admin/reward-settings':
    if not self.auth():return
    return self.respond(200,public_counts())
   if p=='/api/admin/contacts':
    if not self.auth():return
    with sqlite3.connect(DB) as c:
     rows=c.execute("SELECT w.id,w.kind,w.email,w.gifts,w.number,json_extract(r.payload,'$.records[0].mode') FROM rewards w LEFT JOIN records r ON r.id=w.id WHERE w.kind!='none' ORDER BY w.rowid").fetchall()
     legacy=c.execute('SELECT number,id FROM contacts WHERE id NOT IN (SELECT id FROM rewards)').fetchall()
    return self.respond(200,{'contacts':[{'id':i,'kind':k,'email':e,'gifts':json.loads(g),'number':n,'mode':m} for i,k,e,g,n,m in rows]+[{'id':i,'kind':'legacy','number':n,'email':'','gifts':[]} for n,i in legacy]})
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
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN');rows=c.execute('SELECT payload FROM records ORDER BY rowid').fetchall();counts=participation_counts(c);rs=snapshot(c)
    audio={}
    for (raw,) in rows:
     d=json.loads(raw)
     if parse_qs(urlsplit(self.path).query).get('summary')!=['1']:audio.update(d.get('audio',{}))
    return self.respond(200,{'format':'JATA-RESEARCH-3','records':rs,'audio':audio,'counts':counts})
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
   if p=='/api/transcribe-answer':
    return self.respond(410,{'error':'自動轉錄已停用，原始錄音仍可保存。'})
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
    if version not in ('pixel-v9','pixel-v13','pixel-v17','pixel-v19'):raise ValueError('version')
    if version in ('pixel-v17','pixel-v19'):
     nick=answers.get('S01',{});gender=answers.get('S08',{})
     if not isinstance(nick,dict) or nick.get('status')!='answered' or not isinstance(nick.get('text'),str) or not 1<=len(nick['text'].strip())<=80:raise ValueError('nickname')
     if not isinstance(gender,dict) or gender.get('status')!='answered' or gender.get('text') not in ('男','女','不願透露'):raise ValueError('gender')
    if version in ('pixel-v13','pixel-v17','pixel-v19') and r['mode']=='interview':
     for k in ('Q14','Q15','Q16','Q17','Q18'):
      a=answers.get(k,{})
      if not isinstance(a,dict) or a.get('status')!='answered' or not isinstance(a.get('follow'),str) or sum(ch.isalnum() for ch in a['follow'])<4:raise ValueError('missing social main answer')
    if version=='pixel-v19':
     for k,opts in {'S03':('沒有使用','每天多次','每天約 1 次','每週 2–6 次','每週約 1 次','每月 1–3 次','每月不到 1 次'),'S09':('有','沒有')}.items():
      a=answers.get(k,{})
      if not isinstance(a,dict) or a.get('status')!='answered' or a.get('text') not in opts:raise ValueError('background')
    reward=d.get('reward',{});kind=reward.get('kind');email=reward.get('email','');gifts=reward.get('gifts',[])
    if kind not in ('none','gift','lottery') or not isinstance(email,str) or not isinstance(gifts,list):raise ValueError('reward')
    if kind=='none':
     if email or gifts:raise ValueError('skip')
    else:
     if len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('email')
     if kind=='gift':
      if r['mode']!='interview' or len(gifts)!=2 or any(not isinstance(g,str) or g not in {f'gift{i}' for i in range(1,6)} for g in gifts) or len(set(gifts))!=2:raise ValueError('gifts')
     elif gifts:raise ValueError('lottery')
    if FORMAL and r['mode']=='interview':
     for k in ('Q19','Q20','Q21'):
      a=answers[k]
      if a['status']=='answered' and not a.get('shopChoices'):raise ValueError('missing shop choice')
    audio={};allowed={f'audio:{id}:{k}' for k,a in answers.items() if isinstance(a,dict) and a.get('audio')==f'audio:{id}:{k}'}
    if r.get('consent',{}).get('recording'):
     for k,v in d.get('audio',{}).items():
      if k in allowed and isinstance(v,str) and re.match(r'^data:audio/[\w.+-]+(?:;codecs=[\w.+-]+)?;base64,',v) and len(v)<=42*1024*1024:audio[k]=v
    if r.get('rewardPolicy')=='v20' and allowed!=set(audio):raise ValueError('audio missing or invalid')
    client_token=d.get('contactToken','');client_token=client_token if isinstance(client_token,str) else '';r.pop('contactEmail',None);r['consent'].pop('contact',None);r['consent']['externalTranscription']=r['consent'].get('recording') is True and r['consent'].get('externalTranscription') is True
    # Accept only verified live AI text, bound to this exact audio, journey and question.
    for k,a in answers.items():
     if not isinstance(a,dict):continue
     text=a.get('transcript');receipt=a.pop('transcriptReceipt21',None);raw=audio.get(a.get('audio',''))
     if not (r['consent'].get('externalTranscription') and r['consent'].get('externalTranscriptionLive') is True and raw and isinstance(text,str) and 0<len(text)<=30000 and isinstance(receipt,str) and hmac.compare_digest(receipt,transcript_receipt(id,k,raw,text))):a.pop('transcript',None)
     a.pop('transcriptStatus21',None)
    d={'format':'JATA-RESEARCH-3','records':[r],'audio':audio}
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT id FROM records WHERE id=?',(id,)).fetchone()
     if not old:
      if kind=='lottery' and r['mode']=='interview' and participation_counts(c)['giftRemaining']>0 and d['records'][0].get('rewardPolicy')=='v20':
       return self.respond(409,{'error':'gift_available'})
      if kind=='gift' and participation_counts(c)['giftRemaining']==0:
       kind='lottery';gifts=[]
      if r['mode']=='interview':c.execute('INSERT OR IGNORE INTO interview_slots VALUES (?)',(id,))
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
    return self.respond(200,{'saved':True,'id':id,**contact_status(id),'contactToken':token,**public_counts()})
   if p=='/api/contact/status':
    d=self.payload();id=d.get('id','');token=d.get('token','')
    if not isinstance(id,str) or not isinstance(token,str):raise ValueError('token')
    with sqlite3.connect(DB) as c:row=c.execute('SELECT token FROM contact_tokens WHERE id=?',(id,)).fetchone()
    if not row or not hmac.compare_digest(row[0],token):return self.respond(403,{'error':'invalid token'})
    return self.respond(200,contact_status(id))
   if p=='/api/admin/draw':
    if not self.auth():return
    if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.respond(403,{'error':'cross-site'})
    if self.payload().get('confirm')!='DRAW':raise ValueError('confirmation')
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN IMMEDIATE');d=draw_state(c)
     pool=c.execute("SELECT id,number FROM rewards WHERE kind='lottery' AND email<>'' AND number IS NOT NULL AND id NOT IN (SELECT id FROM lottery_winners)").fetchall()
     if not d['remaining'] or not pool:return self.respond(409,{'error':'no remaining winners or entries'})
     batch=secrets.SystemRandom().sample(pool,min(len(pool),d['remaining']));at=datetime.datetime.now(datetime.timezone.utc).isoformat();round_id=secrets.token_hex(8)
     c.executemany('INSERT INTO lottery_winners VALUES (?,?,?,?)',[(i,n,at,round_id) for i,n in batch]);result=draw_state(c)
    return self.respond(200,result)
   if p=='/api/admin/transcribe':
    if not self.auth():return
    return self.respond(410,{'error':'自動轉錄已停用，請試聽或下載原始錄音。'})
   if p=='/api/admin/transcript':
    if not self.auth():return
    if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.respond(403,{'error':'cross-site'})
    d=self.payload();rid=d.get('id');k=d.get('question');text=d.get('text')
    if not isinstance(text,str) or not 0<len(text.strip())<=30000:raise ValueError('text')
    with sqlite3.connect(DB) as c:
     row=c.execute('SELECT payload FROM records WHERE id=?',(rid,)).fetchone()
     if not row or not json.loads(row[0])['records'][0].get('answers',{}).get(k,{}).get('audio'):raise ValueError('audio')
     c.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?)',(rid,k,text.strip()))
    return self.respond(200,{'text':text.strip()})
   if p=='/api/admin/reward-settings':
    if not self.auth():return
    if self.headers.get('Sec-Fetch-Site')=='cross-site':return self.respond(403,{'error':'cross-site'})
    d=self.payload();limit=d.get('giftLimit');winners=d.get('lotteryWinnerCount')
    if type(limit) is not int or not 0<=limit<=10000 or (winners is not None and (type(winners) is not int or not 0<=winners<=10000)):raise ValueError('reward settings')
    with sqlite3.connect(DB) as c:
     c.execute('BEGIN IMMEDIATE')
     c.execute('UPDATE reward_settings SET gift_limit=?,winner_count=? WHERE id=1',(limit,winners))
     result=participation_counts(c)
    return self.respond(200,result)
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
     for table in ('records','contacts','contact_tokens','rewards','interview_slots','lottery_winners','transcripts'):c.execute(f'DELETE FROM {table}')
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
     for table in ('records','contacts','contact_tokens','rewards','transcripts','lottery_winners'):c.execute(f'DELETE FROM {table} WHERE id=?',(id,))
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
