import asyncio,json,os
from pathlib import Path
import pytest
from oopz_capture.analyzer.backend import BackendError
from oopz_capture.controller import ControllerService,START_FLOW_SCHEMA
from oopz_capture.feishu_gateway import FeishuGateway,FeishuGatewayConfig
from oopz_capture.feishu_protocol import FeishuInbound,synthetic_controller_id
from oopz_capture.send_request import enqueue_send_request,list_send_requests
@pytest.fixture
def cfg(tmp_path,monkeypatch):
 for k in list(os.environ):
  if k.startswith(('OOPZ_','ANALYZER_')):monkeypatch.delenv(k)
 for k,v in {'OOPZ_CAPTURE_ONLY':'true','OOPZ_FEISHU_APP_ID':'test-app','OOPZ_FEISHU_APP_SECRET':'test-secret','OOPZ_FEISHU_ADMIN_CHAT_ID':'oc_test','OOPZ_CAPTURE_ONLY_STATE_ROOT':str(tmp_path/'capture-state'),'OOPZ_CAPTURE_ONLY_OUTPUT_ROOT':str(tmp_path/'capture-output'),'OOPZ_FEISHU_STATE_ROOT':str(tmp_path/'normal-state'),'OOPZ_OUTPUT_ROOT':str(tmp_path/'normal-output'),'OOPZ_DEVICE':'cuda:0'}.items():monkeypatch.setenv(k,v)
 return FeishuGatewayConfig.from_env()
class Channel:
 def __init__(self):self.sent=[]
 async def send(self,to,message,opts=None):self.sent.append((to,message));return type('Result',(),{'success':True})()
class Controller:
 def __init__(self):self.received=[]
 async def handle(self,raw):self.received.append(raw);return {'text':'ok'}
def forbidden(*a,**k):pytest.fail('Forbidden analysis/model call')
def test_no_consent_no_analyzer_gateway_ready(cfg):
 assert cfg.capture_only and cfg.controller_config.capture_only and not cfg.controller_config.consent_confirmed
 assert cfg.controller_config.device=='cpu' and cfg.controller_config.retain_audio
def test_default_requires_analyzer(cfg,monkeypatch):
 monkeypatch.setenv('OOPZ_CAPTURE_ONLY','false')
 with pytest.raises(BackendError,match='OOPZ_ANALYZER_CLI'):FeishuGatewayConfig.from_env()
@pytest.mark.parametrize('kind',['missing','relative','used','overlap','normal'])
def test_unsafe_roots(cfg,monkeypatch,kind):
 if kind=='missing':monkeypatch.delenv('OOPZ_CAPTURE_ONLY_STATE_ROOT')
 elif kind=='relative':monkeypatch.setenv('OOPZ_CAPTURE_ONLY_STATE_ROOT','relative')
 elif kind=='used':cfg.state_root.mkdir();(cfg.state_root/'old.json').write_text('{}')
 elif kind=='overlap':monkeypatch.setenv('OOPZ_CAPTURE_ONLY_OUTPUT_ROOT',str(cfg.state_root/'nested'))
 else:monkeypatch.setenv('OOPZ_CAPTURE_ONLY_STATE_ROOT',str(cfg.state_root.parent/'normal-state'))
 with pytest.raises(ValueError):FeishuGatewayConfig.from_env()
def test_outbox_and_cleanup_disabled(cfg):
 ch=Channel();g=FeishuGateway(cfg,ch,controller=Controller())
 for source in ['digest:image','analysis_error','lifecycle','other']:enqueue_send_request(cfg.state_root,target_type='private',target_id='x',text='DO NOT SEND',source=source)
 async def run():
  for method in ['drain_outbox','cleanup_expired_sessions']:assert await getattr(g,method)()==0
 asyncio.run(run());assert not ch.sent;assert len(list_send_requests(cfg.state_root,statuses={'pending'}))==4
def test_dangerous_commands_blocked(cfg):
 c,ch=Controller(),Channel();g=FeishuGateway(cfg,ch,controller=c)
 async def run():
  for i,t in enumerate(['是','开始分析','待分析','最近图片','最近报告','删除会话','设置 OOPZ_DEVICE=cuda:0']):await g.handle_message(FeishuInbound(f'm{i}','oc_test','ou_test',t))
  for i,a in enumerate(['analysis_yes','analysis_no','digest:send:abc','pending:analyze:abc','delete:confirm:abc','publication:yes:abc']):await g.handle_card_action(action_id=a,open_id='ou_test',event_id=f'c{i}',chat_id='oc_test')
 asyncio.run(run());assert not c.received;assert len(ch.sent)==13
def test_start_status_stop_selection(cfg):
 c,ch=Controller(),Channel();g=FeishuGateway(cfg,ch,controller=c)
 async def run():
  for i,t in enumerate(['开始录音 5分钟','状态','停止']):await g.handle_message(FeishuInbound(f'm{i}','oc_test','ou_test',t))
  await g.handle_card_action(action_id='selection:1',open_id='ou_test',event_id='sel',chat_id='oc_test')
 asyncio.run(run());assert [x['text'] for x in c.received]==['/oopz 开始 5m','/oopz 状态','/oopz 离开','1']
def test_model_analysis_blocked_in_capture_only(cfg):
 s=ControllerService(cfg.controller_config,analysis_runner=forbidden)
 p=s.output_root/'test-session';p.mkdir(parents=True);(p/'lifecycle.json').write_text(json.dumps({'status':'ready_for_analysis'}))
 s._state['last_job']={'session_id':'test-session','status':'capture_transcription_completed'};s._reconcile_last_job();assert s._state['last_job']['status']=='capture_transcription_completed'
 async def run():
  assert not s._start_analysis_and_deliver(Path('unused'));await s._analyze_and_deliver(Path('unused'))
 asyncio.run(run())
def test_channel_selection_waits_for_initiator_token(cfg,monkeypatch):
 s=ControllerService(cfg.controller_config);ch=Channel();g=FeishuGateway(cfg,ch,controller=s)
 owner=synthetic_controller_id('ou_initiator')
 s._save_start_flow({'schema_version':START_FLOW_SCHEMA,'admin_id':owner,'stage':'awaiting_channel_selection','selected_area':{'area_id':'a','name':'area'},'channels':[{'channel_id':'c','display_name':'voice'}],'max_runtime_seconds':300})
 calls=[]
 async def launch(message,command,**kw):calls.append(kw);return {'text':'started','message_id':message.message_id}
 monkeypatch.setattr(s,'_launch_capture',launch)
 async def run():
  await g.handle_card_action(action_id='selection:1',open_id='ou_initiator',event_id='pick',chat_id='oc_test');assert not calls
  f=s._load_start_flow();assert f['stage']=='awaiting_recording_consent';token=f['consent_token'];assert '已告知参与者并开始录音' in json.dumps(ch.sent,ensure_ascii=False)
  for who,t,ev in [('ou_other',token,'other'),('ou_initiator','0'*32,'stale')]:
   await g.handle_card_action(action_id='capture_consent:'+t,open_id=who,event_id=ev,chat_id='oc_test');assert not calls
  await g.handle_card_action(action_id='capture_consent:'+token,open_id='ou_initiator',event_id='confirm',chat_id='oc_test');assert len(calls)==1 and calls[0]['consent_confirmed'] is True
  await g.handle_card_action(action_id='capture_consent:'+token,open_id='ou_initiator',event_id='replay',chat_id='oc_test');assert len(calls)==1
 asyncio.run(run())
def test_capture_direct_call_requires_consent(cfg):
 s=ControllerService(cfg.controller_config)
 async def run():
  with pytest.raises(ValueError,match='explicit initiator confirmation'):await s._launch_capture(None,'start_capture',area_id='a',channel_id='c',area_name='area',channel_name='voice',max_runtime=300)
 asyncio.run(run());assert s._state.get('active') is None
def test_completion_never_calls_model_without_requester(cfg):
 from oopz_capture.continuous import ContinuousRequest
 async def loader(*a):return object()
 async def capture(config,request,*,output_root,device,session_id):
  p=output_root/session_id;(p/'handoff').mkdir(parents=True);(p/'handoff'/'analyzer_request.json').write_text('{}');(p/'lifecycle.json').write_text(json.dumps({'status':'ready_for_analysis','stop_reason':'test_done'}));return p
 s=ControllerService(cfg.controller_config,config_loader=loader,capture_runner=capture,analysis_runner=forbidden)
 async def run():
  req=ContinuousRequest(request_id='test',area_id='a',channel_id='c',consent_confirmed=True,requested_by={})
  s._state['active']={'session_id':'test-session'};await s._run_session('test-session',req)
 asyncio.run(run());assert s._state['last_job']['status']=='capture_transcription_completed'
 assert not list_send_requests(cfg.state_root,statuses={'pending'})

from datetime import datetime, timedelta, timezone
@pytest.mark.parametrize('case',['expired','wrong_chat','cancelled'])
def test_rejects_unusable_consent(cfg, monkeypatch, case):
 s=ControllerService(cfg.controller_config);g=FeishuGateway(cfg,Channel(),controller=s)
 token='a'*32
 s._save_start_flow({'schema_version':START_FLOW_SCHEMA,'admin_id':synthetic_controller_id('owner'),'stage':'awaiting_recording_consent','consent_token':token,'selected_area':{'area_id':'a','name':'area'},'selected_channel':{'channel_id':'c','display_name':'voice'}})
 if case=='expired':
  data=s._load_start_flow();data['updated_at']=(datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat();s._start_flow_path.write_text(json.dumps(data))
 elif case=='cancelled':s._clear_start_flow()
 async def forbidden(*a,**k):pytest.fail('Unusable consent launched capture')
 monkeypatch.setattr(s,'_launch_capture',forbidden)
 asyncio.run(g.handle_card_action(action_id='capture_consent:'+token,open_id='owner',event_id='evt',chat_id='wrong' if case=='wrong_chat' else 'oc_test'))
