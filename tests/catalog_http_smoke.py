"""Real HTTP and four TCP sockets; uses isolated synthetic data, never a deployed server."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from competition.station_webapp import create_app
from competition.standard_json import EXAMPLE_CATALOG
from competition.station_protocol import encode, decode
from competition.web_auth import credential_record
from competition.web_config import WebConfig


@contextmanager
def live_server():
    import uvicorn
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);password='Synthetic-catalog-test-123456'
        credentials=root/'admin.json';credentials.write_text(json.dumps(credential_record('admin',password)),encoding='utf-8')
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        base=f'http://localhost:{port}'
        config=WebConfig(data_dir=root/'data',credentials=credentials,public_url=base,port=port,tcp_port=0,auto_backup=False)
        app=create_app(config);server=uvicorn.Server(uvicorn.Config(app,log_level='warning',ws='none',proxy_headers=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            for _ in range(150):
                if server.started:break
                time.sleep(.05)
            if not server.started:raise RuntimeError('HTTP server did not start')
            yield base,password,app
        finally:
            server.should_exit=True;thread.join(10);listener.close()


class TCPClient:
    def __init__(self,address,project,station):
        self.project,self.station=project,station
        self.sock=socket.create_connection(address,timeout=5);self.file=self.sock.makefile('rb')
        hello={'v':2,'type':'hello','msg_id':uuid.uuid4().hex,'project':project,'station':station}
        assert self.send(hello)['type']=='hello_ok'
    def send(self,data):
        self.sock.sendall(encode(data));answer=decode(self.file.readline()[:-1]);return answer
    def result(self,**fields):
        return {'v':2,'type':'result','msg_id':uuid.uuid4().hex,'project':self.project,'station':self.station,'worker_id':'001234',**fields}
    def close(self):
        self.file.close();self.sock.close()


def main():
    import httpx
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    report={'mode':'real HTTP and four TCP sockets','passed':False,'checks':[]}
    try:
        with live_server() as (base,password,app),httpx.Client(base_url=base,timeout=10,trust_env=False) as web:
            r=web.post('/api/auth/login',json={'username':'admin','password':password},headers={'Origin':base});r.raise_for_status()
            headers={'Origin':base,'X-CSRF-Token':r.json()['csrf']}
            def post(path,payload):
                r=web.post(path,json=payload,headers={**headers,'X-Request-ID':uuid.uuid4().hex});r.raise_for_status();return r.json()
            peers=[]
            try:
                peers=[TCPClient(app.state.runtime.tcp.address,proj,n) for proj in ('screw','packaging') for n in (1,2)]
                post('/api/admin/standard-barcode/import',{'content':json.dumps(EXAMPLE_CATALOG),'revision':0})
                post('/api/admin/standard-barcode/select',{'station':1,'box_id':'BOX01','revision':1})
                post('/api/admin/standard-barcode/select',{'station':2,'box_id':'BOX02','revision':2})
                report['checks'].append('HTTP login, batch import, two independent selections')
                for peer in peers:
                    for i in range(10):
                        data=peer.result(**({'screw_count':i%5} if peer.project=='screw' else {'barcode':'001234-AbC','logo':'OK','flame':'NG'}))
                        ack=peer.send(data);assert ack['type']=='result_ack' and ack['recorded']
                        assert set(ack)=={'v','type','reply_to','recorded','duplicate','record_id'}
                    assert peer.send(data)['duplicate']
                report['checks'].append('40 observations + 4 duplicate retries, no target downlink')
                state=web.get('/api/admin/state?project=packaging').json()
                assert state['stations'][0]['latest']['barcode_status']=='MATCH'
                assert state['stations'][1]['latest']['barcode_status']=='MISMATCH'
                assert all(s['connected'] for s in state['stations'])
                assert all(r['logo']=='OK' and r['flame']=='NG' for r in state['records'])
                report['checks'].append('same barcode matches only the selected target; independent LOGO/flame')
                post('/api/admin/standard-barcode/select',{'station':1,'box_id':'BOX03','revision':3})
                rows=[json.loads(line) for line in web.get('/api/admin/export?project=packaging&format=jsonl').text.splitlines()]
                assert len(rows)==20 and all(r['standard_box_id']=='BOX01' for r in rows if r['station']==1)
                csv=web.get('/api/admin/export?project=packaging&format=csv');assert csv.status_code==200 and 'standard_box_name' in csv.text
                backup=post('/api/admin/backup',{});assert web.get(backup['download_url']).content[:16]==b'SQLite format 3\0'
                public=web.get('/api/display?project=packaging').json();assert 'catalog' not in public and 'standard' not in public
                assert all('standard_box_id' not in row for row in public['records'])
                report['checks'].append('historical target snapshots, exports, backup and public-data isolation')
                report['passed']=True
            finally:
                for peer in peers:peer.close()
    finally:
        (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
