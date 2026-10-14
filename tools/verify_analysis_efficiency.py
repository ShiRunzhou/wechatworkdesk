"""Small paid A/B smoke test in temporary data; never publishes into the user's desk."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workdesk.store import Store
from workdesk.cloud import CloudPipeline
from workdesk.flow_config import Settings
from workdesk.model_api import ModelAPI

TEXT='''# 测试通知群
## 2026-10-12T09:00:00+08:00 黄秘书
消息标识: test:1
请各专业主任于10月20日前将本专业实训设备需求汇总表交给黄秘书。
## 2026-10-12T10:00:00+08:00 黄秘书
消息标识: test:2
实训设备需求汇总表截止时间改为10月22日，收件人不变。
## 2026-10-12T11:00:00+08:00 院长
消息标识: test:3
请施润周10月16日参加专业主任教学会议，地点学院会议室。
## 2026-10-12T12:00:00+08:00 团委
消息标识: test:4
本周有自愿讲座报名，全院老师自愿参加，无需专业主任组织或转达。
'''

def main():
    p=argparse.ArgumentParser();p.add_argument('--settings-data',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    settings=Settings(Path(a.settings_data));config=settings.public();key=settings.key();reports={}
    for name,efficient in [('thinking_baseline',False),('efficient',True)]:
        with tempfile.TemporaryDirectory(prefix='wcwd-analysis-ab-') as temp:
            root=Path(temp);store=Store(root/'data');obj=store.add_object('测试通知群')
            store.save_memory({'profile':'我叫施润周，是休闲服务与管理专业主任，负责专业教学运行与实训需求汇总。通知群面向全院教师，没有明确要求时不需我重复转达。'})
            store.save_memory(dict(object_id=obj['id'],content='自愿讲座报名无需列出',kind='exclude',match_terms=['自愿讲座'],exceptions='明确要求本人组织、转达或参加时仍列出',state='active'))
            configs=[]
            def factory(c,k):configs.append(copy.deepcopy(c));return ModelAPI(c,key)
            flow=CloudPipeline(store,provider_factory=factory)
            saved={k:config[k] for k in ('base_url','model','extra_parameters','json_mode','output_parameter','temperature','timeout')}
            flow.settings.save({**saved,'efficient_mode':efficient,'max_tokens':8192,'lookup_rounds':1,'max_model_calls':8,'max_total_tokens':100000})
            md=root/'messages.md';md.write_text(TEXT,'utf8');flow.upload(md,'messages.md',obj['id'])
            payload=dict(sessions=[obj['id']],mode='api',range='custom',start='2026-10-12',end='2026-10-12')
            started=time.monotonic();j=flow.create(payload,False);flow.run(j['id']);j=flow.job(j['id'])
            result=dict(status=j['status'],error=j['error'],seconds=round(time.monotonic()-started,2),calls=j.get('request_count',0),usage=j['usage'],warnings=j['warnings'],
                        matters=[{k:m[k] for k in ('title','deadline','owner','detail','action','questions')} for m in store.dashboard()['matters']])
            combined=json.dumps(result['matters'],ensure_ascii=False)
            result['quality']={'requirements_present':'实训' in combined and '22' in combined,'meeting_present':'会议' in combined and '16' in combined,
                               'voluntary_lecture_excluded':not any('讲座' in m['title'] for m in result['matters'])}
            if j['status']=='completed':
                repeat=flow.create(payload,False);flow.run(repeat['id']);repeat=flow.job(repeat['id'])
                result['identical_repeat']={'status':repeat['status'],'additional_calls':repeat.get('request_count',0),'usage':repeat['usage']}
                if repeat['status']!='completed' or repeat.get('request_count',0):raise RuntimeError('重复资料未成功复用')
            reports[name]=result;Path(a.out).write_text(json.dumps(reports,ensure_ascii=False,indent=2),'utf8')
            print(json.dumps({'case':name,**result},ensure_ascii=False),flush=True)
            if result['status']!='completed' or not all(result['quality'].values()):raise RuntimeError('小样本分析未通过；请查看私有测试报告')
    print('Isolated real API smoke test passed; no production events changed.',flush=True)

if __name__=='__main__':main()
