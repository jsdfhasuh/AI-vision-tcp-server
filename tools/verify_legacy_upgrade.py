"""Verify upgrade using an actual unpacked v1.1 release, not the current Store.
Usage: python tools/verify_legacy_upgrade.py PATH_TO_OLD_RELEASE
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from competition.storage import Store
from competition.dbtools import inspect_database


def main():
    old=Path(sys.argv[1]).resolve()
    code='''
import json,sys
from pathlib import Path
from competition.storage import Store
from competition.core import Engine
from competition import protocol
from test_system import FakePeer
store=Store(Path(sys.argv[1]));engine=Engine(store)
session=engine.new_session('旧版升级验证演示队','vision-01','packaging','DEMO')
peer=FakePeer()
engine.receive(peer,protocol.encode(dict(v=1,type='hello',msg_id='h',client_id='vision-01',access_code='DEMO')))
engine.receive(peer,protocol.encode(dict(v=1,type='target_ack',msg_id='a',session_id=session['id'],target_revision=1)))
for i in range(4):
    rid=engine.start_round('升级测试样件','NG')
    if i<3:engine.receive(peer,protocol.encode(dict(v=1,type='result',msg_id=str(i),session_id=session['id'],round_id=rid,target_revision=1,verdict='NG')))
print(json.dumps({'sid':session['id'],'rows':store.rows(session['id'])},ensure_ascii=False))
store.close()
'''
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'competition.sqlite3'
        env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(old),str(old/'tests')]))
        result=subprocess.run([sys.executable,'-c',code,str(path)],cwd=tmp,env=env,capture_output=True,text=True,check=True,timeout=15)
        original=json.loads(result.stdout)
        before=inspect_database(path)
        store=Store(path)
        try:
            after=inspect_database(path)
            rows=store.rows(original['sid'])
            assert [r['id'] for r in rows]==[r['id'] for r in original['rows']]
            assert [r['actual'] for r in rows]==[r['actual'] for r in original['rows']]
            assert rows[0]['status']=='INTERRUPTED'
            assert store.sessions()[0]['mode']=='LEGACY'
            report={'old_store_source':str(old),'before_version':before['schema_version'],
                    'after_version':after['schema_version'],'round_ids_preserved':True,'verdicts_preserved':True,
                    'before_counts':before['counts'],'after_counts':after['counts'],
                    'unfinished_round_after_upgrade':rows[0]['status'], 'old_session_mode':'LEGACY',
                    'pre_upgrade_backup_version':inspect_database(store.migration_backup/'database.sqlite3')['schema_version'],
                    'integrity_ok':after['integrity_ok']}
        finally:store.close()
        print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
