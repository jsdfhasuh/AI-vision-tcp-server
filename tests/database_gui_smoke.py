"""Optional Tk database-console smoke. Run under Windows or xvfb-run on Linux."""
from __future__ import annotations
import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from competition.gui import App
from competition import protocol
from competition.dbtools import restore_backup, inspect_database
from test_system import FakePeer


def main():
    temp=tempfile.TemporaryDirectory()
    directory=Path(temp.name)
    root=tk.Tk()
    app=App(root,directory,display_enabled=False)
    failures=[]
    root.report_callback_exception=lambda t,v,tb:failures.append(str(v))
    def wait_for(condition,timeout=10):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            root.update()
            if condition():return
            time.sleep(.02)
        raise TimeoutError('GUI database condition not satisfied')
    def fixture():
        for i,purpose in enumerate(('PRACTICE','OFFICIAL','PRACTICE')):
            session=app.engine.new_session(('练习联调 A 队','正式演示 B 队','练习联调 C 队')[i],'vision-01','packaging','DEMO_ONLY',mode=purpose)
            peer=FakePeer()
            app.engine.receive(peer,protocol.encode(dict(v=1,type='hello',msg_id='h',client_id='vision-01',access_code='DEMO_ONLY')))
            app.engine.receive(peer,protocol.encode(dict(v=1,type='target_ack',msg_id='a',session_id=session['id'],target_revision=1)))
            for j in range(3):
                rid=app.engine.start_round('模拟完整样件' if j%2==0 else '模拟标签异常','OK' if j%2==0 else 'NG')
                app.engine.receive(peer,protocol.encode(dict(v=1,type='result',msg_id=f'r{j}',session_id=session['id'],round_id=rid,target_revision=1,verdict='OK' if j%2==0 else 'NG')))
        return session
    report={}
    try:
        app._submit(fixture,'new_session')
        wait_for(lambda:len(app.state.get('sessions',[]))==3 and not app.busy)
        app._database_manager()
        window=app.database_window
        window.search()
        wait_for(lambda:window.total==3 and not app.busy)
        report['query_all_sessions']=window.total
        window.mode.set('正式比赛');window.search()
        wait_for(lambda:window.total==1 and not app.busy)
        report['official_filter']=window.total
        row=next(iter(window.records))
        window.table.selection_set(row);window.open_session()
        wait_for(lambda:app.history_sid==row and not app.busy)
        assert 'disabled' in app.start_button.state()
        report['history_start_disabled']=True
        window.mode.set('全部用途');window.search()
        wait_for(lambda:window.total==3 and not app.busy)
        window.check()
        wait_for(lambda:'完整性检查：通过' in window.status.get() and not app.busy)
        assert '"schema_version": 2' in window.details.get('1.0','end')
        report['health_from_ui']='passed'
        from PIL import ImageGrab
        window.win.lift();root.update()
        bbox=(window.win.winfo_rootx(),window.win.winfo_rooty(),window.win.winfo_rootx()+window.win.winfo_width(),window.win.winfo_rooty()+window.win.winfo_height())
        ImageGrab.grab(bbox=bbox).save(ROOT/'docs/screenshots/database_console.png')
        window.run('backup',lambda:app.store.backup(directory/'manual_backups'))
        wait_for(lambda:window.status.get().startswith('备份完成') and not app.busy)
        backup=next((directory/'manual_backups').iterdir())
        restored=restore_backup(backup,directory/'restored')
        assert inspect_database(restored)['counts']['rounds']==9
        report['ui_backup_restore_rounds']=9
        wait_for(lambda:not app.pending_backups)
        report['session_switch_backups']=len(list((directory/'backups').iterdir()))
        assert report['session_switch_backups']==2
        assert not failures,failures
        report['tk_callback_errors']=failures
    finally:
        app._close()
        def closed():
            try:return not root.winfo_exists()
            except tk.TclError:return True
        wait_for(closed,timeout=15)
        backups=sorted((directory/'backups').iterdir())
        report['backups_after_shutdown']=len(backups)
        assert len(backups)==3
        saved=inspect_database(backups[-1]/'database.sqlite3')
        report['shutdown_backup_integrity']=saved['integrity_ok']
        report['shutdown_backup_sessions']=saved['counts']['sessions']
        temp.cleanup()
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
