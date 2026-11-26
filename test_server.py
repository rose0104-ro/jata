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
  print('PASS: editable titles, auth/confirmation, reset records/contacts/counter, assets and titles preserved.')
  print('PASS: gift x2, opt-out, concurrent 1–8 lottery, email validation, idempotency, email separated, private admin, gift asset upload.')
 finally:p.terminate();p.wait(timeout=5)
