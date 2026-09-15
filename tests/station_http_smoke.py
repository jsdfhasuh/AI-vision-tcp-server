"""Native HTTP client + production entry point + four independent TCP connections."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from competition.station_protocol import encode, decode
from competition.web_auth import credential_record


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def main():
    import httpx
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    report={'passed':False,'mode':'native HTTP + real TCP (not browser)','records':0}
    peers=[]
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);password='HTTP-test-only-123456';credentials=root/'admin.json'
        credentials.write_text(json.dumps(credential_record('admin',password)))
        port,tcp_port=free_port(),free_port()
        while port==tcp_port:tcp_port=free_port()
        origin=f'http://localhost:{port}'
        cmd=[sys.executable,'web_server.py','--data-dir',str(root/'data'),'--credentials',str(credentials),
             '--port',str(port),'--tcp-port',str(tcp_port),'--public-url',origin]
        with (args.output_dir/'server.log').open('w') as log:
            proc=subprocess.Popen(cmd,cwd=Path(__file__).resolve().parents[1],env={**os.environ,'AUTO_BACKUP':'0'},stdout=log,stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=origin,timeout=5,trust_env=False) as http:
                    for _ in range(100):
                        if proc.poll() is not None:raise RuntimeError('server exited before startup')
                        try:
                            if http.get('/healthz').status_code==200:break
                        except httpx.HTTPError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('server startup timed out')
                    login=http.post('/api/auth/login',json={'username':'admin','password':password},headers={'Origin':origin});login.raise_for_status()
                    headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf'],'X-Request-ID':uuid.uuid4().hex}
                    http.post('/api/admin/standard-barcode',json={'barcode':'00001234-AbC','revision':0},headers=headers).raise_for_status()
                    messages=[]
                    for project in ('screw','packaging'):
                        for station in (1,2):
                            sock=socket.create_connection(('127.0.0.1',tcp_port),timeout=5);stream=sock.makefile('rb');peers.append((sock,stream))
                            for i in range(10):
                                data={'v':2,'type':'result','msg_id':uuid.uuid4().hex,'project':project,'station':station,'worker_id':f'D7051{station}'}
                                if project=='screw':data['screw_count']=4 if i%2==0 else 3
                                else:data.update(barcode='00001234-AbC' if i%2==0 else '1234-AbC',logo='OK' if i%3 else 'NG',flame='NG' if i%2 else 'OK')
                                sock.sendall(encode(data));ack=decode(stream.readline()[:-1]);assert ack['recorded'] and not ack['duplicate']
                                report['records']+=1
                            messages.append(data)
                    for (sock,stream),data in zip(peers,messages):
                        sock.sendall(encode(data));assert decode(stream.readline()[:-1])['duplicate']
                    for project in ('screw','packaging'):
                        snapshot=http.get('/api/admin/state',params={'project':project}).json()
                        assert len(snapshot['records'])==20 and all(s['connected'] for s in snapshot['stations'])
                        if project=='packaging':
                            for r in snapshot['records']:
                                assert r['barcode_status']==('MATCH' if r['barcode']=='00001234-AbC' else 'MISMATCH')
                        public=http.get('/api/display',params={'project':project}).json();assert 'standard' not in public and 'logs' not in public
                    export=http.get('/api/admin/export',params={'project':'packaging','format':'jsonl'});export.raise_for_status()
                    assert len(export.text.strip().splitlines())==20
                    backup=http.post('/api/admin/backup',json={},headers=headers);backup.raise_for_status()
                    assert http.get(backup.json()['download_url']).content.startswith(b'SQLite format 3\x00')
                    assert not (root/'data'/'competition.sqlite3').exists()
                    report.update(passed=True,duplicate_replays=4,projects=2,simultaneous_stations=4)
            finally:
                for sock,stream in peers:stream.close();sock.close()
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
