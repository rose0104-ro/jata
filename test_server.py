"""V9 isolated reward and privacy test; no real participant data."""
import os,sys,tempfile,subprocess,time,json,urllib.request,urllib.error,concurrent.futures
from pathlib import Path
ROOT=Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as tmp:
 env={**os.environ,'JATA_DATA_DIR':tmp,'JATA_FORMAL':'0','JATA_PRIVATE_LINK':'T'*48}
 p=subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--lan','--port','18765'],env=env,stdout=subprocess.DEVNULL)
 base='http://127.0.0.1:18765'
 def req(path,data=None,headers={}):
  r=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json',**headers})
  try:
   with urllib.request.urlopen(r,timeout=4) as f:return f.status,json.loads(f.read())
  except urllib.error.HTTPError as e:return e.code,None
 try:
  for i in range(40):
   try:
    if req('/api/status')[0]==200:break
   except OSError:time.sleep(.1)
  assert req('/api/admin/records')[0]==401 and req('/api/admin/contacts')[0]==401
  auth={'Cookie':'jata_admin='+'T'*48}
  assert req('/api/admin/check',headers=auth)[0]==200
  def submit(i,mode='survey',kind='lottery',gifts=None,email=None):
   return req('/api/submit',{'format':'JATA-RESEARCH-3','records':[{'id':f'JATA-TEST-{i}','mode':mode,'consent':{'participation':True,'version':'pixel-v9'},'completedAt':'test','answers':{},'survey':{},'materials':{}}],'contactToken':f'{i:064x}','reward':{'kind':kind,'email':email if email is not None else (f'test{i}@example.invalid' if kind!='none' else ''),'gifts':gifts or []}})
  assert submit(1,'interview','gift',['gift1','gift4'])[1]['lotteryNumber'] is None
  assert submit(2,'survey','none')[1]['lotteryNumber'] is None
  assert submit(3,'interview','gift',['gift1','gift1'])[0]==400
  assert submit(4,'survey','lottery',email='invalid')[0]==400
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex: results=list(ex.map(submit,range(5,13)))
  assert all(code==200 for code,_ in results)
  assert sorted(int(d['lotteryNumber']) for _,d in results)==list(range(1,9))
  assert submit(5)[1]['lotteryNumber']==results[0][1]['lotteryNumber']
  rows=req('/api/admin/contacts',headers=auth)[1]['contacts'];assert len(rows)==9
  gift=next(r for r in rows if r['kind']=='gift');assert gift['gifts']==['gift1','gift4'] and gift['email']=='test1@example.invalid'
  records=req('/api/admin/records',headers=auth)[1];assert len(records['records'])==10 and '@example.invalid' not in json.dumps(records)
  assert req('/api/contact/status',{'id':'JATA-TEST-5','token':'wrong'})[0]==403
  assert req('/api/admin/assets',{'assets':{'gift1':{'name':'test.png','data':'data:image/png;base64,aGVsbG8='}}},auth)[0]==200
  assert 'gift1' in req('/api/assets')[1]['assets']
  assert req('/api/admin/assets/title',{'id':'gift1','title':'恰塔甜點'},auth)[0]==200
  assert req('/api/admin/assets/title',{'id':'v1','title':'雨天大冒險'},auth)[0]==200
  assert req('/api/assets')[1]['titles']['gift1']=='恰塔甜點'
  assert req('/api/admin/records/clear',{'confirm':'CLEAR_ALL_RESEARCH'})[0]==401
  assert req('/api/admin/records/clear',{'confirm':'wrong'},auth)[0]==400
  assert req('/api/admin/records/clear',{'confirm':'CLEAR_ALL_RESEARCH'},auth)[0]==200
  assert req('/api/admin/records',headers=auth)[1]['records']==[]
  assert req('/api/admin/contacts',headers=auth)[1]['contacts']==[]
  assert req('/api/assets')[1]['titles']['v1']=='雨天大冒險'
  assert 'gift1' in req('/api/assets')[1]['assets']
  assert submit(20)[1]['lotteryNumber']=='0001'
  # Clear resets the gift limit too. Survey and overflow interview use one sequence.
  assert req('/api/status')[1]['giftRemaining']==10
  with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
   results=list(ex.map(lambda i:submit(i,'interview','gift',['gift1','gift2']),range(21,33)))
  assert all(code==200 for code,_ in results)
  assert sum(d['rewardKind']=='gift' for _,d in results)==10
  assert sorted(int(d['lotteryNumber']) for _,d in results if d['rewardKind']=='lottery')==[2,3]
  assert submit(33,'survey','lottery')[1]['lotteryNumber']=='0004'
  assert submit(34,'interview','lottery')[1]['lotteryNumber']=='0005'
  assert submit(35,'interview','none')[1]['lotteryNumber'] is None
  assert req('/api/status')[1]['interviewCompleted']==14
  assert req('/api/status')[1]['totalCompleted']==16
  assert req('/api/status')[1]['giftRemaining']==0
  rows=req('/api/admin/contacts',headers=auth)[1]['contacts']
  assert sum(r['kind']=='gift' for r in rows)==10
  assert any(r['mode']=='interview' and r['kind']=='lottery' for r in rows)
  assert submit(34,'interview','lottery')[1]['lotteryNumber']=='0005'
  # V17 demographic validation (existing V16 records still accepted above).
  def profile_submit(i,gender='不願透露',nick='測試代號',mode='survey'):
   a={'S01':{'status':'answered','text':nick},'S08':{'status':'answered','text':gender}}
   if mode=='interview':
    a.update({f'Q{n}':{'status':'answered','follow':'測試補充回答'} for n in range(14,19)})
   return req('/api/submit',{'format':'JATA-RESEARCH-3','records':[{'id':f'JATA-TEST-{i}','mode':mode,'consent':{'participation':True,'version':'pixel-v17'},'completedAt':'test','answers':a,'survey':{},'materials':{}}],'contactToken':f'{i:064x}','reward':{'kind':'none','email':'','gifts':[]}})
  assert profile_submit(40)[0]==200
  assert profile_submit(41,'男',mode='interview')[0]==200
  assert profile_submit(42,'')[0]==400
  assert profile_submit(43,nick='  ')[0]==400
  records=req('/api/admin/records',headers=auth)[1]['records']
  assert next(r for r in records if r['id']=='JATA-TEST-40')['answers']['S08']['text']=='不願透露'
  assert next(r for r in records if r['id']=='JATA-TEST-41')['answers']['S01']['text']=='測試代號'
  # V18 persisted settings: private writes, dynamic quota, and no renumbering.
  assert req('/api/admin/reward-settings')[0]==401
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':5})[0]==401
  assert req('/api/admin/reward-settings',{'giftLimit':-1,'lotteryWinnerCount':5},auth)[0]==400
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':True},auth)[0]==400
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':5},{**auth,'Sec-Fetch-Site':'cross-site'})[0]==403
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':5},auth)[0]==200
  assert req('/api/status')[1]['giftLimit']==2 and req('/api/status')[1]['lotteryWinnerCount']==5
  assert submit(34,'interview','lottery')[1]['lotteryNumber']=='0005'
  assert req('/api/admin/records/clear',{'confirm':'CLEAR_ALL_RESEARCH'},auth)[0]==200
  assert req('/api/status')[1]['giftRemaining']==2 and req('/api/status')[1]['lotteryWinnerCount']==5
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
   changed=list(ex.map(lambda i:submit(i,'interview','gift',['gift1','gift2']),range(50,55)))
  assert sum(d['rewardKind']=='gift' for _,d in changed)==2
  assert sorted(int(d['lotteryNumber']) for _,d in changed if d['rewardKind']=='lottery')==[1,2,3]
  assert req('/api/admin/reward-settings',{'giftLimit':6,'lotteryWinnerCount':None},auth)[1]['giftRemaining']==4
  assert submit(56,'interview','gift',['gift1','gift2'])[1]['rewardKind']=='gift'
  assert req('/api/status')[1]['lotteryWinnerCount'] is None
  assert 'gift1' in req('/api/assets')[1]['assets']
  # Restart the server, retaining the same SQLite data directory.
  p.terminate();p.wait(timeout=5)
  p=subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--lan','--port','18765'],env=env,stdout=subprocess.DEVNULL)
  for i in range(40):
   try:
    if req('/api/status')[0]==200:break
   except OSError:time.sleep(.1)
  assert req('/api/status')[1]['giftLimit']==6 and req('/api/status')[1]['giftRemaining']==3
  assert req('/api/status')[1]['lotteryWinnerCount'] is None
  assert len(req('/api/admin/contacts',headers=auth)[1]['contacts'])==6
  # V19 new background fields and authoritative dashboard snapshot.
  def v19_submit(i,mode='survey',freq='每天多次',ip='有',email='test@example.invalid',kind='lottery'):
   a={'S01':{'status':'answered','text':'測試暱稱'},'S08':{'status':'answered','text':'不願透露'},'S03':{'status':'answered','text':freq},'S09':{'status':'answered','text':ip}}
   if mode=='interview':a.update({f'Q{n}':{'status':'answered','follow':'測試補充回答'} for n in range(14,19)})
   return req('/api/submit',{'format':'JATA-RESEARCH-3','records':[{'id':f'JATA-TEST-{i}','mode':mode,'consent':{'participation':True,'version':'pixel-v19'},'completedAt':'test','answers':a,'survey':{},'materials':{}}],'contactToken':f'{i:064x}','reward':{'kind':kind,'email':email,'gifts':[]}})
  assert v19_submit(61)[0]==200
  assert v19_submit(62,'interview',freq='沒有使用',ip='沒有',email='',kind='none')[0]==200
  assert v19_submit(63,ip='')[0]==400
  assert v19_submit(64,freq='')[0]==400
  assert v19_submit(65,email='')[0]==400
  snapshot=req('/api/admin/records?summary=1',headers=auth)[1];status=req('/api/status')[1]
  assert snapshot['counts']=={k:status[k] for k in snapshot['counts']}
  assert snapshot['counts']['totalCompleted']==len(snapshot['records'])
  assert snapshot['counts']['interviewCompleted']==sum(r['mode']=='interview' for r in snapshot['records'])
  assert next(r for r in snapshot['records'] if r['id']=='JATA-TEST-61')['answers']['S09']['text']=='有'
  assert 'test@example.invalid' not in json.dumps(snapshot)
  # V20: existing lottery participants do not consume future gift slots; private durable draw.
  assert req('/api/admin/records/clear',{'confirm':'CLEAR_ALL_RESEARCH'},auth)[0]==200
  assert req('/api/admin/reward-settings',{'giftLimit':1,'lotteryWinnerCount':2},auth)[0]==200
  assert submit(100,'interview','gift',['gift1','gift2'])[1]['rewardKind']=='gift'
  assert submit(101,'interview','gift',['gift1','gift2'])[1]['rewardKind']=='lottery'
  assert submit(102,'survey','lottery')[1]['lotteryNumber']=='0002'
  assert submit(103,'interview','none')[0]==200
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':2},auth)[1]['giftRemaining']==1
  assert submit(104,'interview','gift',['gift1','gift2'])[1]['rewardKind']=='gift'
  assert req('/api/status')[1]['giftRemaining']==0
  assert req('/api/admin/draw')[0]==401
  assert req('/api/admin/draw',{'confirm':'DRAW'})[0]==401
  assert req('/api/admin/draw',{'confirm':'wrong'},auth)[0]==400
  d=req('/api/admin/draw',{'confirm':'DRAW'},auth)[1];assert {w['number'] for w in d['winners']}=={1,2}
  assert req('/api/admin/draw',{'confirm':'DRAW'},auth)[0]==409
  assert req('/api/status')[1].get('winners') is None
  assert submit(105)[1]['lotteryNumber']=='0003'
  assert req('/api/admin/reward-settings',{'giftLimit':2,'lotteryWinnerCount':3},auth)[0]==200
  d=req('/api/admin/draw',{'confirm':'DRAW'},auth)[1];assert {w['number'] for w in d['winners']}=={1,2,3}
  # Original audio stored, manual transcript independent, no external transfer without consent.
  akey='audio:JATA-TEST-106:Q01';payload={'format':'JATA-RESEARCH-3','records':[{'id':'JATA-TEST-106','mode':'interview','consent':{'participation':True,'recording':True,'version':'pixel-v9'},'completedAt':'test','answers':{'Q01':{'status':'answered','audio':akey,'transcript':'不應顯示的舊文字','transcriptReceipt21':'invalid'}},'survey':{},'materials':{},'rewardPolicy':'v20'}],'audio':{akey:'data:audio/webm;base64,aGVsbG8='},'contactToken':f'{106:064x}','reward':{'kind':'none','email':'','gifts':[]}}
  assert req('/api/submit',payload)[0]==200
  assert req('/api/admin/transcribe',{'id':'JATA-TEST-106','question':'Q01'},auth)[0]==410
  assert req('/api/transcribe-answer',{'consent':{'participation':True,'recording':True,'externalTranscription':True,'externalTranscriptionLive':True}})[0]==410
  assert req('/api/admin/transcribe',{'id':'JATA-TEST-106','question':'Q01'})[0]==401
  assert req('/api/admin/transcript',{'id':'JATA-TEST-106','question':'Q01','text':'測試逐字稿'},auth)[0]==410
  snap=req('/api/admin/records?summary=1',headers=auth)[1];assert 'transcript' not in next(r for r in snap['records'] if r['id']=='JATA-TEST-106')['answers']['Q01'];assert req('/api/status')[1]['version']=='V25'
  with urllib.request.urlopen(urllib.request.Request(base+'/api/admin/audio?id=JATA-TEST-106&key='+akey,headers=auth)) as f:assert f.read()==b'hello'
  assert snap['audio']=={} and snap['counts']['totalCompleted']==len(snap['records'])
  import zipfile,io,xml.etree.ElementTree as ET
  for endpoint in ('export.xlsx','rewards.xlsx'):
   r=urllib.request.Request(base+'/api/admin/'+endpoint,headers=auth)
   with urllib.request.urlopen(r) as f:blob=f.read();assert f.headers['Content-Type'].endswith('spreadsheetml.sheet')
   with zipfile.ZipFile(io.BytesIO(blob)) as z:
    assert z.testzip() is None
    for n in z.namelist():ET.fromstring(z.read(n))
    x=z.read('xl/worksheets/sheet2.xml').decode();assert ('錄音檔代碼' if endpoint=='export.xlsx' else '0003') in x;assert 'AI逐字稿' not in x and '不應顯示的舊文字' not in x
  print('PASS: V20 additional gift quota, private persistent draw without duplicates, original audio/manual transcript, native XLSX structure.')
  print('PASS: V19 background validation, email opt-out, server dashboard counts, private email separation.')
  print('PASS: private adjustable reward settings, quota enforcement, saved settings survive restart, prior tickets unchanged.')
  print('PASS: V17 survey/interview profile validation, saved demographics, legacy compatibility.')
  print('PASS: simultaneous interview cap 10, shared survey/interview sequence, counts, opt-out, retry.')
  print('PASS: editable titles, auth/confirmation, reset records/contacts/counter, assets and titles preserved.')
  print('PASS: gift x2, opt-out, concurrent 1–8 lottery, email validation, idempotency, email separated, private admin, gift asset upload.')
 finally:p.terminate();p.wait(timeout=5)
