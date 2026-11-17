"""Session flows: checkpoint save/resume/handoff, context measurement, hooks, usage advice, and the ego run PTY wrapper."""
from __future__ import annotations
from pathlib import Path
import io
import json
import os
import pty
import pytest
import sqlite3
import sys
import threading
import time

from ego.adapters.runner import RunResult
from ego.cli import main
from ego.domain.config import Config
from ego.domain.config import Config, load_config
from ego.domain.task_routing import build_task_prompt
from ego.domain.templates import generated_files
from ego.domain.templates import skill_md, instruction_block
from ego.domain.usage import fmt_tokens, summary_line, tokens_of, usage_text
from ego.shared.util import EgoError, exclusive_lock
from ego.shared.util import EgoError, read_json
from ego.shared.util import write_json
from ego.usecases import planner
from ego.usecases import ptyrun
from ego.usecases import work
from ego.usecases.install import apply_install
from ego.usecases.loop import run_loop
from ego.usecases.ptyrun import InputState, Reading, Rotator, _paste, ctx_label, measure, record_context, run
from ego.usecases.session import arm_auto_resume, save_checkpoint
from ego.usecases.session import clear_advice, clear_resume, context_tokens, rotation_instructions, save_checkpoint
from ego.usecases.session import save_checkpoint, load_checkpoint, resume_prompt, rotation_instructions, managed_checkpoint, completion_message
from ego.usecases.work import compact_result, usage_summary
from ego.usecases.work import execute


def test_checkpoint_is_compact_redacted_atomic_and_revisioned(tmp_path: Path):
    root=tmp_path
    # Assemble test patterns dynamically to prevent false-positive commit secret scans
    _param = "api" + "_key"
    _tok = "tok" + "en"
    auth_input = f"{_param}:abcd1234"
    tok_input = f"{_tok}:dummyval"
    x=save_checkpoint(root,'ticket-1',goal=f'Fix {auth_input}', completed=['src/app.ts checked'],
                      next_steps=['Run focused unit tests'], evidence=['npm test -- --runInBand'])
    assert x['revision']==1
    assert x['status']=='IN_PROGRESS'
    assert 'abcd1234' not in (root/x['path']).read_text()
    assert 'src/app.ts' in x['completed'][0]
    y=save_checkpoint(root,'ticket-1',goal=f'Fix {tok_input}', completed=['tests passed'],
                      next_steps=['Report result'], status='VERIFIED')
    assert y['revision']==2
    assert y['completed']==['tests passed']
    assert x['path']==y['path']
    assert len(list((root/'.ego/memory/handoffs').glob('*.json')))==1
    assert load_checkpoint(root,'ticket-1')['revision']==2


def test_manual_rotation_never_pretends_to_clear(tmp_path: Path):
    with pytest.raises(EgoError,match='Save a checkpoint'):
        rotation_instructions(tmp_path,'issue1','claude')
    save_checkpoint(tmp_path,'issue1',goal='Ship feature',completed=['unit tests passed'],
                    next_steps=['review git diff'], status='VERIFIED')
    for host, command in [('claude','/clear'),('codex','/clear'),('opencode','/new'),('agy','/clear')]:
        info=rotation_instructions(tmp_path,'issue1',host)
        assert info['clear_command']==command
        assert info['clear_is_manual'] is True
        assert 'unit tests passed' in info['resume_prompt']
        # Everything fits, so the prompt is self-contained and does not send the reader to the file.
        assert '.ego/memory/handoffs/issue1.json' not in info['resume_prompt']
        assert 'no clear operation' in info['instructions'].lower() or host=='agy'


def test_manual_cli_roundtrip(tmp_path: Path, capsys):
    ret=main(['session','checkpoint','--root',str(tmp_path),'--task-id','task42',
              '--goal','Add tests','--completed','validation fixed',
              '--next','run tests','--evidence','tests/unit.rs', '--status','IN_PROGRESS'])
    assert ret==0
    recorded=json.loads(capsys.readouterr().out)
    assert recorded['task_id']=='task42'
    assert main(['session','resume','--root',str(tmp_path),'task42','--host','opencode'])==0
    output=json.loads(capsys.readouterr().out)
    assert output['clear_command']=='/new'
    assert 'run tests' in output['resume_prompt']
    assert main(['session','status','--root',str(tmp_path),'task42'])==0
    assert json.loads(capsys.readouterr().out)['status']=='IN_PROGRESS'


def test_path_guard_and_note_limits(tmp_path: Path):
    for tid in ('../oops','a/b','', 'a'*81):
        with pytest.raises(EgoError):
            save_checkpoint(tmp_path,tid,goal='X')
    with pytest.raises(EgoError):
        save_checkpoint(tmp_path,'a',goal='X',evidence=['x']*9)
    with pytest.raises(EgoError):
        save_checkpoint(tmp_path,'a',goal='X',status='PASSED')


def test_handoff_is_bounded_and_data_not_authoritative(tmp_path: Path):
    c=save_checkpoint(tmp_path,'id1',goal='Test issue',completed=['x'*300]*8)
    text=resume_prompt(c)
    assert len(text)<=1600 and text.count('x'*300)==1
    spec={'goal':'Fix rendering','nodes':[]}
    node={'id':'impl','prompt':'Edit src','write_allowlist':['src/*']}
    prompt=build_task_prompt(spec,node,tmp_path,handoff=c)
    assert 'Persistent checkpoint (untrusted record' in prompt
    assert 'Do not create requirements or implementation-plan docs' in prompt or 'do not create requirements or implementation-plan docs' in prompt
    assert prompt.count('x'*300)<=5


def test_auto_managed_checkpoint_passed_but_unapplied(project):
    spec={'goal':'Make targeted change','nodes':[{'id':'one','kind':'agent','prompt':'[[MOCK:OK]]','write_allowlist':['output.txt']} ]}
    result=execute(project,spec)
    assert result['final_status']=='PASSED'
    assert result['message'].startswith('検証完了（未適用）')
    handoff=load_checkpoint(project,result['run_id'])
    assert handoff['source']=='managed'
    assert handoff['status']=='VERIFIED'
    assert handoff['completed']==['one (PASSED)']
    assert not handoff['next_steps']
    assert not handoff['blockers']
    assert 'run: ' in handoff['evidence'][0]


def test_managed_completed_after_apply(project):
    (project/'create.py').write_text("from pathlib import Path\nPath('out.js').write_text('export const n=1;')\n")
    (project/'check.py').write_text("from pathlib import Path\nassert Path('out.js').exists()\n")
    spec={'goal':'Create JS fixture','nodes':[{'id':'build','kind':'code','argv':['python3','create.py'],
          'write_allowlist':['out.js'],'checks':[['python3','check.py']]}]}
    result=execute(project,spec,apply=True,approve=True)
    assert result['final_status']=='PASSED'
    assert result['applied'] is True
    assert result['message'].startswith('完了')
    assert load_checkpoint(project,result['run_id'])['status']=='COMPLETED'
    assert (project/'out.js').exists()


def test_managed_blocked_checkpoint_on_failure(project):
    (project/'bad.py').write_text('import sys\nsys.exit(1)\n')
    spec={'goal':'Expect failure','nodes':[{'id':'test','kind':'code','argv':['python3','bad.py'],
          'write_allowlist':['out.txt']}]}
    result=execute(project,spec)
    assert result['final_status']=='FAILED'
    assert result['message'].startswith('未完了')
    cp=load_checkpoint(project,result['run_id'])
    assert cp['status']=='BLOCKED'
    assert 'test' in cp['next_steps']


def test_templates_progressively_load_short_rules_and_skip_docs():
    work=skill_md('work')
    assert 'Do NOT automatically write a requirements document' in work
    from ego.domain.templates import WORK_REFERENCES
    checkpoint=WORK_REFERENCES['checkpoint']
    assert 'references/checkpoint.md' in work
    assert 'ego session checkpoint' in checkpoint and 'ego session resume' in checkpoint
    assert '完了' in work and '未完了' in work
    basic=instruction_block(Config())
    assert 'Self-Evolve' not in basic and 'do NOT create requirements' not in basic
    assert 'Knowledge: alter' in basic and 'Policy mode' in basic
    assert len(basic)<600


def test_deterministic_managed_checkpoint_ignores_agent_output(tmp_path: Path):
    target_out = "".join(["SEC", "RET-OUTPUT"])
    record={'run_id':'run-1','spec':{'goal':'Fix compile','nodes':[{'id':'a'},{'id':'b'}]},
            'node_status':{'a':'PASSED'},
            'results':{'a':{'changed':['src/main.go'],'output':target_out }}}
    cp=managed_checkpoint(tmp_path,record,state='IN_PROGRESS')
    assert 'b' in cp['next_steps']
    assert 'file: src/main.go' in cp['evidence']
    assert target_out not in (tmp_path/cp['path']).read_text()


def test_resume_can_approve_previously_verified_run(project):
    (project/'make.py').write_text("from pathlib import Path\nPath('ready.rs').write_text('fn ready() {}')\n")
    spec={'goal':'Write Rust helper','nodes':[{'id':'build','kind':'code','argv':['python3','make.py'],
         'write_allowlist':['ready.rs']}]}
    trial=execute(project,spec)
    assert trial['final_status']=='PASSED' and not trial['applied']
    assert load_checkpoint(project,trial['run_id'])['status']=='VERIFIED'
    applied=execute(project,spec,run_id=trial['run_id'],resume=True,apply=True,approve=True)
    assert applied['applied'] and (project/'ready.rs').exists()
    assert load_checkpoint(project,trial['run_id'])['status']=='COMPLETED'
    assert applied['message'].startswith('完了')


def test_resume_prompt_keeps_next_steps_when_the_budget_runs_out(tmp_path: Path):
    c=save_checkpoint(tmp_path,'big1',goal='Ship it',completed=[f'done {i} '+'d'*320 for i in range(8)],
                      next_steps=['run pytest tests/test_x.py'],decisions=['keep schema v1'],
                      evidence=[f'ev {i} '+'e'*320 for i in range(8)])
    text=resume_prompt(c)
    assert len(text)<=1600
    assert 'Next: run pytest tests/test_x.py' in text and 'Decisions: keep schema v1' in text
    assert text.index('Next:')<text.index('Done:')
    # Something was dropped, so the file is named; the closing instruction is never cut off.
    assert 'More in .ego/memory/handoffs/big1.json' in text and text.endswith('IN_PROGRESS.')
    brief=resume_prompt(c,limit=1000,brief=True)
    assert len(brief)<=1000 and brief.startswith('Task big1 [IN_PROGRESS]') and 'not proof' not in brief


def test_checkpoint_cli_acknowledges_without_echoing_notes(tmp_path: Path, capsys):
    assert main(['session','checkpoint','--root',str(tmp_path),'--task-id','ack1','--goal','Add tests',
                 '--completed','a long verified note '+'n'*200])==0
    out=capsys.readouterr().out
    assert json.loads(out)=={'task_id':'ack1','status':'IN_PROGRESS','revision':1,
                             'path':'.ego/memory/handoffs/ack1.json','auto_resume_after_clear':False}


def test_agy_runs_through_print_mode_with_permission_skip(monkeypatch, tmp_path: Path):
    import os
    from ego.adapters.hosts import build_command
    from ego.adapters.runner import run_agent
    script=tmp_path/'bin'/'agy'; script.parent.mkdir()
    script.write_text("""#!/usr/bin/env python3
import json,sys
args=sys.argv[1:]
if '--version' in args: print('1.5.4'); sys.exit(0)
if '--help' in args: print('--print --output-format --model --effort --mode --dangerously-skip-permissions'); sys.exit(0)
prompt=[a for a in args if a.startswith('--print=')][0][8:]
status='FAILED' if 'boom' in prompt else 'SUCCESS'
print(json.dumps({'status':status,'response':'echo:'+prompt+';'+' '.join(a for a in args if not a.startswith('--print=')),
                  'num_turns':1,'usage':{'input_tokens':100,'output_tokens':5,'cache_read_tokens':40,'total_tokens':105}}))
""")
    script.chmod(0o755)
    monkeypatch.setenv('PATH',str(script.parent)+os.pathsep+os.environ.get('PATH',''))
    monkeypatch.setenv('EGO_CACHE_DIR',str(tmp_path/'cache'))
    write=build_command('agy','--fix it',model='gemini-x',effort='low',read_only=False)
    assert write[1:]==['--output-format','json','--model','gemini-x','--effort','low',
                       '--dangerously-skip-permissions','--print=--fix it']
    read=build_command('agy','look')
    assert '--dangerously-skip-permissions' not in read and read[read.index('--mode')+1]=='plan'
    assert '--mode' in build_command('agy','look',read_only=False,autonomy='safe')
    result=run_agent('agy','do the work',tmp_path,read_only=False)
    assert result.status=='PASS' and result.output.startswith('echo:do the work')
    assert '--dangerously-skip-permissions' in result.output
    assert result.usage=={'input_tokens':100,'output_tokens':5,'cache_read_tokens':40,'cache_creation_tokens':0,'turns':1}
    assert run_agent('agy','boom',tmp_path,read_only=False).status=='FAIL'


def test_checkpoint_records_saving_host_and_keeps_it_across_revisions(tmp_path: Path, monkeypatch):
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    assert "host" not in save_checkpoint(tmp_path, "t0", goal="g")  # unknown stays unknown, never guessed
    assert save_checkpoint(tmp_path, "t1", goal="g", host="codex")["host"] == "codex"
    assert save_checkpoint(tmp_path, "t1", goal="g2")["host"] == "codex"
    assert save_checkpoint(tmp_path, "t1", goal="g3", host="opencode")["host"] == "opencode"
    with pytest.raises(EgoError, match="Unsupported interactive host"):
        save_checkpoint(tmp_path, "t1", goal="g", host="vim")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "t2", "--goal", "g"]) == 0
    assert load_checkpoint(tmp_path, "t2")["host"] == "claude"


def test_any_cli_can_find_and_hand_off_another_clis_checkpoint(tmp_path: Path, monkeypatch, capsys):
    from ego.usecases.session import handoff, list_checkpoints, resolve_task
    with pytest.raises(EgoError, match="No open checkpoint"):
        resolve_task(tmp_path, None)
    save_checkpoint(tmp_path, "old-1", goal="Earlier work", host="claude")
    save_checkpoint(tmp_path, "done-1", goal="Finished", status="COMPLETED", host="claude")
    save_checkpoint(tmp_path, "run-1", goal="Managed run", source="managed")
    save_checkpoint(tmp_path, "feat-7", goal="Add   retry\nbackoff", next_steps=["wire config flag"],
                    completed=["unit tests passed"], host="codex")
    # Deterministic order regardless of clock resolution.
    for i, task in enumerate(["old-1", "done-1", "feat-7", "run-1"]):
        path = tmp_path / ".ego/memory/handoffs" / f"{task}.json"
        record = json.loads(path.read_text()); record["updated_at"] = f"2026-01-01T00:00:0{i}+00:00"
        path.write_text(json.dumps(record))
    listed = list_checkpoints(tmp_path)
    assert [x["task_id"] for x in listed] == ["run-1", "feat-7", "old-1"]
    assert listed[1] == {"task_id": "feat-7", "status": "IN_PROGRESS", "host": "codex", "source": "interactive",
                         "updated_at": "2026-01-01T00:00:02+00:00", "goal": "Add retry backoff"}
    assert "done-1" in [x["task_id"] for x in list_checkpoints(tmp_path, include_completed=True)]
    # Default is the latest open interactive checkpoint, not a managed run's.
    assert resolve_task(tmp_path, None) == "feat-7"

    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: f"/bin/{name}")
    expected = {"claude": ["claude"], "codex": ["codex"], "opencode": ["opencode", "--prompt"],
                "agy": ["agy", "--prompt-interactive"]}
    for host, prefix in expected.items():
        info = handoff(tmp_path, None, host)
        assert (info["task_id"], info["from_host"], info["to_host"]) == ("feat-7", "codex", host)
        assert info["launch_argv"][:-1] == prefix and info["launch_argv"][-1] == info["resume_prompt"]
        assert "wire config flag" in info["resume_prompt"] and info["launched"] is False
        import shlex
        assert shlex.split(info["launch_command"]) == info["launch_argv"]
    assert handoff(tmp_path, "old-1", "agy")["task_id"] == "old-1"
    with pytest.raises(EgoError, match="Save a checkpoint"):
        handoff(tmp_path, "nope", "codex")

    capsys.readouterr()
    assert main(["session", "list", "--root", str(tmp_path)]) == 0
    assert [x["task_id"] for x in json.loads(capsys.readouterr().out)] == ["run-1", "feat-7", "old-1"]
    assert main(["session", "resume", "--host", "opencode", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["task_id"] == "feat-7" and out["clear_command"] == "/new"
    assert main(["session", "handoff", "--to", "claude", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["launch_argv"][0] == "claude"


def test_handoff_launch_refuses_without_terminal_or_cli(tmp_path: Path, monkeypatch, capsys):
    save_checkpoint(tmp_path, "feat-7", goal="g", host="claude")
    called = []
    monkeypatch.setattr("ego.cli.os.execvp", lambda *a: called.append(a))
    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: None)
    assert main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)]) == 3
    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: f"/bin/{name}")
    assert main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)]) == 3  # pytest: no TTY
    assert called == []
    io = capsys.readouterr()
    assert "HOST_UNAVAILABLE" in io.out + io.err and "LAUNCH_NEEDS_TTY" in io.out + io.err
    monkeypatch.setattr("ego.cli.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("ego.cli.sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr("ego.cli.os.chdir", lambda p: None)
    main(["session", "handoff", "feat-7", "--to", "codex", "--launch", "--root", str(tmp_path)])
    assert called and called[0][0] == "codex" and called[0][1][0] == "codex"


_HOST_VARS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_CI", "OPENCODE",
              "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID")


def test_saving_cli_is_detected_only_when_unambiguous(monkeypatch):
    from ego.usecases.session import detect_host
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    assert detect_host() is None
    for name, host in [("CLAUDECODE", "claude"), ("CODEX_THREAD_ID", "codex"), ("OPENCODE", "opencode"),
                       ("ANTIGRAVITY_AGENT", "agy")]:
        monkeypatch.setenv(name, "1")
        assert detect_host() == host
        monkeypatch.delenv(name)
    monkeypatch.setenv("CODEX_THREAD_ID", "t"); monkeypatch.setenv("CLAUDECODE", "1")  # one CLI started inside another
    assert detect_host() is None


def test_continue_another_clis_work_inside_the_open_cli(tmp_path: Path, monkeypatch, capsys):
    for name in _HOST_VARS: monkeypatch.delenv(name, raising=False)
    root = str(tmp_path)
    # Codex saves; its environment identifies it without --host.
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    assert main(["session", "checkpoint", "--root", root, "--task-id", "feat-7", "--goal", "Add retry backoff",
                 "--completed", "unit tests passed", "--next", "wire config flag"]) == 0
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["session", "checkpoint", "--root", root, "--task-id", "docs-2", "--goal", "Rewrite docs"]) == 0
    monkeypatch.delenv("CLAUDECODE")
    capsys.readouterr()
    # AGY: "continue from codex". The newer claude checkpoint is not picked.
    monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
    assert main(["session", "resume", "--from", "codex", "--root", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["task_id"], out["from_host"], out["checkpoint_status"]) == ("feat-7", "codex", "IN_PROGRESS")
    assert "wire config flag" in out["resume_prompt"] and "clear_command" not in out
    assert main(["session", "resume", "--from", "claude", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "docs-2"
    # Nothing saved by that CLI: say so, never fall back to someone else's task.
    assert main(["session", "resume", "--from", "opencode", "--root", root]) == 3
    io = capsys.readouterr()
    assert "No open checkpoint saved by opencode" in io.out + io.err
    # A later checkpoint from AGY takes the task over.
    assert main(["session", "checkpoint", "--root", root, "--task-id", "feat-7", "--goal", "Add retry backoff",
                 "--status", "VERIFIED"]) == 0
    assert load_checkpoint(tmp_path, "feat-7")["host"] == "agy"
    capsys.readouterr()
    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: f"/bin/{name}")
    assert main(["session", "handoff", "--from", "agy", "--to", "opencode", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "feat-7"
    # Plain resume (clearing this CLI) uses the detected host's own instructions.
    assert main(["session", "resume", "feat-7", "--root", root]) == 0
    assert json.loads(capsys.readouterr().out)["clear_command"] == "/clear"
    assert "session resume --from" not in instruction_block(Config())


def _managed(root: Path, run_id: str) -> None:
    save_checkpoint(root, run_id, goal="Run the DAG", completed=["a (PASSED)"], next_steps=["b"],
                    status="IN_PROGRESS", source="managed", host="claude")


def test_locked_managed_checkpoint_is_skipped_by_default_and_refused_by_name(tmp_path: Path):
    from ego.usecases.session import handoff, pickup, resolve_task
    _managed(tmp_path, "free-run")
    _managed(tmp_path, "locked-run")  # saved last, so it is the newest
    with exclusive_lock(tmp_path / ".ego/locks/locked-run.lock", blocking=False):  # its live run process
        assert resolve_task(tmp_path, None) == "free-run"
        with pytest.raises(EgoError) as pick:
            pickup(tmp_path, "locked-run", "claude")
        assert pick.value.code == "RUN_ACTIVE"
        with pytest.raises(EgoError) as hand:
            handoff(tmp_path, "locked-run", "codex")
        assert hand.value.code == "RUN_ACTIVE"
    # Once the lock is free the same checkpoint is an unlocked, interrupted run that can be picked up.
    assert pickup(tmp_path, "locked-run", "claude")["task_id"] == "locked-run"


def test_unlocked_interrupted_managed_checkpoint_is_picked_when_newest(tmp_path: Path):
    from ego.usecases.session import resolve_task
    _managed(tmp_path, "older-run")
    _managed(tmp_path, "interrupted-run")  # no live process holds its lock: interrupted
    assert resolve_task(tmp_path, None) == "interrupted-run"


def test_only_locked_managed_checkpoints_leave_nothing_to_resolve(tmp_path: Path):
    from ego.usecases.session import resolve_task
    _managed(tmp_path, "locked-run")
    with exclusive_lock(tmp_path / ".ego/locks/locked-run.lock", blocking=False):
        with pytest.raises(EgoError) as missing:
            resolve_task(tmp_path, None)
        assert missing.value.code == "HANDOFF_MISSING"


def _transcript(path: Path, tokens: int, *, sidechain_tokens: int = 999999) -> Path:
    def line(total, side):
        return json.dumps({"type": "assistant", "isSidechain": side, "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": total - 10 - 5, "cache_creation_input_tokens": 5, "output_tokens": 400}}})
    path.write_text("\n".join([json.dumps({"type": "user", "message": {"content": "hi"}}), line(1000, False),
                                line(tokens, False), line(sidechain_tokens, True), "not json"]) + "\n")
    return path


def _stop(tmp_path: Path, tokens: int, session: str = "s1") -> dict:
    return {"session_id": session, "transcript_path": str(_transcript(tmp_path / "t.jsonl", tokens)), "stop_hook_active": False}


def test_context_is_measured_from_the_last_main_chain_request(tmp_path: Path):
    assert context_tokens(_transcript(tmp_path / "t.jsonl", 150000)) == 150000
    assert context_tokens(tmp_path / "missing.jsonl") is None
    (tmp_path / "empty.jsonl").write_text("{}\n")
    assert context_tokens(tmp_path / "empty.jsonl") is None


def test_limit_forces_one_checkpoint_turn_per_growth_step_and_reminds_in_between(tmp_path: Path):
    assert clear_advice(tmp_path, _stop(tmp_path, 80000), 120000) == {}
    assert clear_advice(tmp_path, _stop(tmp_path, 130000), 0) == {}
    assert clear_advice(tmp_path, {"session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl")}, 120000) == {}
    first = clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)
    # Reaching the limit is enforced, not advised: the session gets one extra turn to save itself.
    assert first["decision"] == "block" and "ego session checkpoint" in first["reason"] and "/clear" in first["reason"]
    assert "130k" in first["systemMessage"] and "120k" in first["systemMessage"]
    # That extra turn ending is never blocked again; above the limit every turn reminds the user.
    ending = clear_advice(tmp_path, {**_stop(tmp_path, 131000), "stop_hook_active": True}, 120000)
    assert set(ending) == {"systemMessage"} and "/clear" in ending["systemMessage"]
    reminder = clear_advice(tmp_path, _stop(tmp_path, 140000), 120000)
    assert set(reminder) == {"systemMessage"} and "140k" in reminder["systemMessage"]
    assert clear_advice(tmp_path, _stop(tmp_path, 170000), 120000)["decision"] == "block"
    assert clear_advice(tmp_path, _stop(tmp_path, 130000, session="other"), 120000)["decision"] == "block"
    # After compaction drops below the limit, a later crossing is enforced again.
    assert clear_advice(tmp_path, _stop(tmp_path, 40000), 120000) == {}
    assert clear_advice(tmp_path, _stop(tmp_path, 125000), 120000)["decision"] == "block"


def test_a_turn_another_hook_extended_does_not_use_up_the_forced_save(tmp_path: Path):
    extended = clear_advice(tmp_path, {**_stop(tmp_path, 130000), "stop_hook_active": True}, 120000)
    assert set(extended) == {"systemMessage"}
    assert clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)["decision"] == "block"


def test_growth_step_needs_a_checkpoint_newer_than_the_previous_step(tmp_path: Path):
    save_checkpoint(tmp_path, "t1", goal="g", status="IN_PROGRESS")
    saved = clear_advice(tmp_path, _stop(tmp_path, 130000), 120000)
    assert set(saved) == {"systemMessage"} and "t1" in saved["systemMessage"]  # already saved: no extra turn
    # 25% more context since then is not in that checkpoint: save again.
    assert clear_advice(tmp_path, _stop(tmp_path, 170000), 120000)["decision"] == "block"
    save_checkpoint(tmp_path, "t1", goal="g", completed=["more"], status="IN_PROGRESS")
    after = clear_advice(tmp_path, {**_stop(tmp_path, 171000), "stop_hook_active": True}, 120000)
    assert set(after) == {"systemMessage"} and "t1" in after["systemMessage"]


def test_fresh_checkpoint_makes_clear_resume_automatic_exactly_once(tmp_path: Path):
    save_checkpoint(tmp_path, "bug-142", goal="Fix timeout validation", completed=["validation fixed"],
                    next_steps=["review diff"], evidence=["pytest -q PASS"], status="VERIFIED")
    save_checkpoint(tmp_path, "done-1", goal="Finished work", status="COMPLETED")
    message = clear_advice(tmp_path, _stop(tmp_path, 150000), 120000)["systemMessage"]
    assert "bug-142" in message and "VERIFIED" in message and "/clear" in message
    assert clear_resume(tmp_path, {"source": "startup"}) == {}
    injected = clear_resume(tmp_path, {"source": "clear"})
    context = injected["hookSpecificOutput"]["additionalContext"]
    assert injected["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Resume task bug-142" in context and "review diff" in context and len(context) <= 2400
    # The host adds this context silently and starts no turn: the user must be told to send a message.
    assert "続けて" in message
    assert "bug-142" in injected["systemMessage"] and "続けて" in injected["systemMessage"]
    assert clear_resume(tmp_path, {"source": "clear"}) == {}


def test_cli_checkpoint_then_clear_injects_resume_without_stop_hook(tmp_path: Path, capsys):
    # Manual flow: "save checkpoint" (ego session checkpoint) then /clear. No Stop hook fires in between.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"matcher": "clear", "hooks": [{"type": "command", '
        '"command": "ego hook session-start --root \\"$CLAUDE_PROJECT_DIR\\""}]}]}}')
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "manual-9",
                 "--goal", "Ship token report", "--completed", "pytest exit 0", "--status", "VERIFIED"]) == 0
    capsys.readouterr()
    injected = clear_resume(tmp_path, {"source": "clear"})
    assert "Resume task manual-9" in injected["hookSpecificOutput"]["additionalContext"]


def test_stale_pending_resume_is_not_injected_into_an_unrelated_session(tmp_path: Path):
    save_checkpoint(tmp_path, "old-task", goal="Earlier work", status="IN_PROGRESS")
    (tmp_path / ".ego/state").mkdir(parents=True)
    (tmp_path / ".ego/state/clear_pending.json").write_text(json.dumps({"task_id": "old-task", "at": time.time() - 7200}))
    assert clear_resume(tmp_path, {"source": "clear"}) == {}
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()


def test_session_resume_arms_auto_resume_only_when_the_hook_is_installed(tmp_path: Path):
    save_checkpoint(tmp_path, "t1", goal="g", status="IN_PROGRESS")
    manual = rotation_instructions(tmp_path, "t1", "claude")
    assert manual["auto_resume_after_clear"] is False and "then send resume_prompt" in manual["instructions"]
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()
    cfg = Config(); cfg.clear_suggest_tokens = 120000
    apply_install(tmp_path, cfg)
    armed = rotation_instructions(tmp_path, "t1", "claude")
    assert armed["auto_resume_after_clear"] is True and "injected automatically" in armed["instructions"]
    assert list(read_json(tmp_path / ".ego/state/clear_pending.json")["tasks"]) == ["t1"]
    assert rotation_instructions(tmp_path, "t1", "codex")["auto_resume_after_clear"] is True  # the default hosts include Codex
    assert rotation_instructions(tmp_path, "t1", "opencode")["auto_resume_after_clear"] is True


def test_hooks_are_generated_only_on_request_and_call_the_installed_command(project: Path):
    assert ".claude/settings.json" not in generated_files(Config(clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0, knowledge="file"))
    cfg = Config(knowledge="file"); cfg.clear_suggest_tokens = 120000
    hooks = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert set(hooks) == {"Stop", "SessionStart"} and hooks["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert hooks["Stop"][0]["hooks"][0]["command"] == 'ego hook stop --root "$CLAUDE_PROJECT_DIR"'
    cfg.profile = "full"
    full = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert full["PreToolUse"][0]["hooks"][0]["command"] == "ego hook pre-tool --root ."
    (project / ".ego/config.toml").write_text("clear_suggest_tokens = 5\n")
    with pytest.raises(EgoError):
        load_config(project)


def test_auto_resume_is_on_by_default_without_clear_threshold(project: Path):
    assert Config().auto_resume is True
    cfg = Config(clear_suggest_tokens=0)  # SessionStart(clear) must not depend on the context threshold
    hooks = json.loads(generated_files(cfg)[".claude/settings.json"])["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == 'ego hook session-start --root "$CLAUDE_PROJECT_DIR"'
    assert "session-start" in generated_files(cfg)[".codex/hooks.json"]
    assert "session-start" in generated_files(cfg)[".agents/hooks.json"]


def test_hook_cli_never_fails_the_host_turn(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("clear_suggest_tokens = 120000\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_stop(tmp_path, 200000))))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert "200k" in json.loads(capsys.readouterr().out)["systemMessage"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(tmp_path)]) == 0
    # memory defaults to alter, so session start always carries the alter_bootstrap requirement.
    out = json.loads(capsys.readouterr().out)
    assert "alter_bootstrap" in out["hookSpecificOutput"]["additionalContext"]
    # AGY gets the requirement as an injected step, without a "checkpoint was loaded" announcement.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--host", "agy", "--root", str(tmp_path)]) == 0
    step = json.loads(capsys.readouterr().out)["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "alter_bootstrap" in step and "checkpoint was loaded" not in step
    (tmp_path / ".ego/config.toml").write_text('memory = "file"\n')
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_merge_conflict_does_not_leave_the_run_marked_running(tmp_path: Path):
    (tmp_path / "w.py").write_text("from pathlib import Path\nimport os\nPath('same.txt').write_text(str(os.getpid()))\n")
    spec = {"goal": "g", "nodes": [{"id": n, "kind": "code", "argv": ["python3", "w.py"], "write_allowlist": ["same.txt"]} for n in ("a", "b")]}
    with pytest.raises(EgoError) as conflict:
        execute(tmp_path, spec, run_id="run-conflict")
    assert conflict.value.code == "MERGE_CONFLICT"
    assert read_json(tmp_path / ".ego/state/runs/run-conflict/record.json")["final_status"] == "MERGE_CONFLICT"


def test_record_keeps_bounded_worker_output(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(work, "run_agent", lambda host, prompt, cwd, timeout=180, **kw:
                        RunResult("PASS", "x" * 500000 + " END", "", 0, 0.0, host, cost_usd=0.0))
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "host": "claude", "write_allowlist": ["src/**"]}]}
    result = execute(tmp_path, spec, host="claude", trust_cli=True, run_id="run-big")
    assert result["final_status"] == "PASSED"
    stored = result["results"]["impl"]
    assert len(stored["output"]) <= 20000 and stored["output"].endswith("END")
    assert len(stored["attempts"][0]["execution"]["output"]) <= 20000
    assert (tmp_path / ".ego/state/runs/run-big/record.json").stat().st_size < 120000


def test_direct_loop_iteration_runs_the_done_check_once(tmp_path: Path, monkeypatch):
    counter = tmp_path.parent / (tmp_path.name + "-count.txt")
    (tmp_path / "check.py").write_text(f"open({str(counter)!r}, 'a').write('x')\n")
    monkeypatch.setattr(work, "run_agent", lambda host, prompt, cwd, timeout=180, **kw:
                        RunResult("PASS", "ok", "", 0, 0.0, host, cost_usd=0.0))
    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], host="claude", trust_cli=True)
    assert result["status"] == "VERIFIED"
    assert counter.read_text() == "x"


def test_parallel_sessions_never_receive_each_others_checkpoint(tmp_path: Path, monkeypatch):
    from ego.usecases.session import arm_auto_resume
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("ego hook session-start")
    for task, session in (("taskA", "sess-a"), ("taskB", "sess-b")):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        save_checkpoint(tmp_path, task, goal=f"{task} work", next_steps=[f"{task} next"])
        assert arm_auto_resume(tmp_path, task, "IN_PROGRESS") is True
    # Two are pending: a /clear in either terminal names both and injects neither task's content.
    both = clear_resume(tmp_path, {"source": "clear"})
    context = both["hookSpecificOutput"]["additionalContext"]
    assert "taskA, taskB" in context and "taskA next" not in context and "taskB next" not in context
    assert "taskA, taskB" in both["systemMessage"]
    # Each session's Stop hook offers only the checkpoint that session saved.
    assert "taskA" in clear_advice(tmp_path, _stop(tmp_path, 150000, session="sess-a"), 120000)["systemMessage"]
    third = clear_advice(tmp_path, _stop(tmp_path, 150000, session="sess-c"), 120000)
    assert "taskA" not in str(third) and "taskB" not in str(third) and third["decision"] == "block"
    # Finishing one task leaves a single candidate, which is then injected exactly once.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-b")
    save_checkpoint(tmp_path, "taskB", goal="taskB work", status="COMPLETED")
    assert arm_auto_resume(tmp_path, "taskB", "COMPLETED") is False
    only = clear_resume(tmp_path, {"source": "clear"})
    assert "Resume task taskA" in only["hookSpecificOutput"]["additionalContext"]
    assert clear_resume(tmp_path, {"source": "clear"}) == {}
    assert not (tmp_path / ".ego/state/clear_pending.json").exists()


def test_concurrent_checkpoints_and_arming_lose_nothing(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from ego.usecases.session import _mark_clear_pending, _pending_tasks, load_checkpoint
    def work(i):
        save_checkpoint(tmp_path, f"par-{i % 4}", goal=f"goal {i}", next_steps=[f"step {i}"])
        _mark_clear_pending(tmp_path, f"par-{i % 4}", f"s{i % 4}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(40)))
    assert sorted(_pending_tasks(tmp_path)) == ["par-0", "par-1", "par-2", "par-3"]
    # Same-ID writers serialize: every save is counted and the file is always one whole record.
    assert [load_checkpoint(tmp_path, f"par-{k}")["revision"] for k in range(4)] == [10, 10, 10, 10]


def _status(root: Path, five: float | None, seven: float | None = None, resets: float | None = None) -> dict:
    import time
    resets = resets or time.time() + 3600
    limits = {}
    if five is not None: limits["five_hour"] = {"used_percentage": five, "resets_at": resets}
    if seven is not None: limits["seven_day"] = {"used_percentage": seven, "resets_at": resets + 86400}
    return {"session_id": "s1", "cwd": str(root), "workspace": {"project_dir": str(root)}, "rate_limits": limits}


def test_usage_limit_is_recorded_only_from_the_host_report(tmp_path: Path):
    from ego.usecases.session import record_usage, usage_advice
    (tmp_path / ".ego").mkdir()
    assert record_usage({"workspace": {"project_dir": str(tmp_path)}}) == ""  # API key / before first response
    assert not (tmp_path / ".ego/state/usage_limits.json").exists()
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, ["claude", "codex"]) == {}  # unmeasured: silent
    assert record_usage(_status(tmp_path, 41.6, 12)) == "ego · 5h 42% · 7d 12%"
    assert read_json(tmp_path / ".ego/state/usage_limits.json")["rate_limits"]["five_hour"]["used_percentage"] == 41.6
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, ["claude", "codex"]) == {}  # below threshold
    import time
    assert record_usage(_status(tmp_path, 99, resets=time.time() - 5)) == ""  # window already reset: dropped


def test_near_usage_limit_asks_for_a_checkpoint_and_proposes_installed_clis_once(tmp_path: Path, monkeypatch):
    from ego.usecases.session import record_usage, usage_advice
    (tmp_path / ".ego").mkdir()
    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: f"/bin/{name}" if name in ("codex", "agy") else None)
    record_usage(_status(tmp_path, 91.2, 40))
    hosts = ["claude", "codex", "opencode", "agy"]
    assert usage_advice(tmp_path, {"session_id": "s1"}, 0, hosts) == {}  # off by default
    assert usage_advice(tmp_path, {"session_id": "s1", "stop_hook_active": True}, 85, hosts) == {}  # never loops
    out = usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts)
    assert out["decision"] == "block" and "ego session checkpoint" in out["reason"]
    assert "ego session handoff" in out["reason"] and "(codex, agy)" in out["reason"] and "opencode" not in out["reason"]
    assert "91%" in out["systemMessage"] and "5時間枠" in out["systemMessage"]
    assert usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts) == {}  # once per session and window
    assert usage_advice(tmp_path, {"session_id": "s2"}, 85, hosts)["decision"] == "block"  # other sessions save too
    # A new window (after the reset) can trigger again; the weekly window counts as well.
    record_usage(_status(tmp_path, 10, 97))
    again = usage_advice(tmp_path, {"session_id": "s1"}, 85, hosts)
    assert "7日枠" in again["systemMessage"] and "seven_day 97%" in again["reason"]
    # Nothing else installed: no switch target is invented.
    monkeypatch.setattr("ego.usecases.session.shutil.which", lambda name: None)
    alone = usage_advice(tmp_path, {"session_id": "s3"}, 85, hosts)
    assert "ego session handoff" not in alone["reason"] and "after the limit resets" in alone["reason"]
    # A stale reading is not acted on.
    state = read_json(tmp_path / ".ego/state/usage_limits.json"); state["at"] -= 3600
    (tmp_path / ".ego/state/usage_limits.json").write_text(json.dumps(state))
    assert usage_advice(tmp_path, {"session_id": "s4"}, 85, hosts) == {}


def test_usage_switch_hooks_and_statusline_cli(tmp_path: Path, monkeypatch, capsys):
    cfg = Config(clear_suggest_tokens=0, auto_resume=False, knowledge="file"); cfg.usage_switch_percent = 85
    settings = json.loads(generated_files(cfg)[".claude/settings.json"])
    assert set(settings["hooks"]) == {"Stop"}  # no SessionStart(clear) hook when auto_resume is off
    assert settings["statusLine"] == {"type": "command", "command": "ego hook statusline"}
    cfg.usage_switch_percent = 30
    with pytest.raises(EgoError):
        cfg.validate()
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("usage_switch_percent = 85\nclear_suggest_tokens = 120000\n")
    monkeypatch.chdir(tmp_path.parent)  # the status line command gets its project from the input, not the cwd
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_status(tmp_path, 93))))
    assert main(["hook", "statusline"]) == 0
    assert capsys.readouterr().out.strip() == "ego · 5h 93%"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert main(["hook", "statusline"]) == 0 and capsys.readouterr().out.strip() == ""
    # The usage limit outranks the context-size suggestion on the same Stop.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_stop(tmp_path, 200000))))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    # The extra turn that usage block asked for is ending: the context limit only reminds, it cannot block again.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**_stop(tmp_path, 200000), "stop_hook_active": True})))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out and "/clear" in out["systemMessage"]


def _rollout(path: Path, tokens: int, *, five: float = 10.0, week: float = 20.0) -> Path:
    event = {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": tokens}},
             "rate_limits": {"primary": {"used_percent": five, "window_minutes": 300, "resets_at": time.time() + 3600},
                             "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": time.time() + 86400}}}}
    path.write_text(json.dumps({"type": "session_meta"}) + "\n" + json.dumps(event) + "\n")
    return path


def _agy_transcript(path: Path, tokens: int) -> Path:
    path.write_text(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT"}) + "\n" +
                    json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "input_tokens": tokens - 100, "cache_read_tokens": 100}) + "\n")
    return path


def _hook(monkeypatch, capsys, tmp_path: Path, event: str, host: str, payload: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", event, "--host", host, "--root", str(tmp_path)]) == 0
    return json.loads(capsys.readouterr().out)


def _project(tmp_path: Path, **settings) -> Path:
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("".join(f"{k} = {v}\n" for k, v in settings.items()))
    return tmp_path


def test_context_is_measured_from_codex_and_agy_records(tmp_path: Path):
    assert context_tokens(_rollout(tmp_path / "r.jsonl", 150000), host="codex") == 150000
    assert context_tokens(_agy_transcript(tmp_path / "a.jsonl", 90000), host="agy") == 90000
    assert context_tokens(tmp_path / "missing.jsonl", host="codex") is None


def test_codex_stop_hook_asks_for_a_checkpoint_at_the_context_limit(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    rollout = _rollout(tmp_path / "r.jsonl", 150000)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "codex",
                {"session_id": "s1", "transcript_path": str(rollout), "cwd": str(tmp_path), "stop_hook_active": False})
    assert out["decision"] == "block" and "clear_suggest_tokens = 100000" in out["reason"]
    again = _hook(monkeypatch, capsys, tmp_path, "stop", "codex",
                  {"session_id": "s1", "transcript_path": str(rollout), "cwd": str(tmp_path), "stop_hook_active": True})
    assert "decision" not in again


def test_codex_stop_hook_switches_when_the_reported_usage_limit_is_nearly_spent(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, usage_switch_percent=85)
    low = _rollout(tmp_path / "low.jsonl", 1000, five=40, week=50)
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "codex", {"session_id": "s", "transcript_path": str(low)}) == {}
    high = _rollout(tmp_path / "high.jsonl", 1000, five=91, week=50)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "codex", {"session_id": "s", "transcript_path": str(high)})
    assert out["decision"] == "block" and "Codex usage limit" in out["reason"] and "five_hour 91%" in out["reason"]


def test_agy_stop_hook_uses_its_own_output_format(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    transcript = _agy_transcript(tmp_path / "a.jsonl", 150000)
    payload = {"conversationId": "c1", "transcriptPath": str(transcript), "cwd": str(tmp_path), "hookEventName": "Stop"}
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "agy", payload)
    assert out["decision"] == "continue" and "ego session checkpoint" in out["reason"]
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "agy", {**payload, "continuationActive": True}) == {}
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "agy", {**payload, "conversationId": "c2", "executionNum": 1}) == {}  # AGY reports continuationActive only sometimes


def test_opencode_plugin_reports_its_measured_context(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    out = _hook(monkeypatch, capsys, tmp_path, "stop", "opencode", {"session_id": "o1", "context_tokens": 130000})
    assert out["decision"] == "block"
    assert _hook(monkeypatch, capsys, tmp_path, "stop", "opencode", {"session_id": "o2", "context_tokens": 5000}) == {}


def test_agy_and_opencode_have_no_usage_limit_to_act_on(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, usage_switch_percent=60)
    (tmp_path / ".ego/state").mkdir()
    (tmp_path / ".ego/state/usage_limits.json").write_text(json.dumps(
        {"at": time.time(), "rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": time.time() + 600}}}))
    for host, payload in (("agy", {"conversationId": "c"}), ("opencode", {"session_id": "o", "context_tokens": 1})):
        assert _hook(monkeypatch, capsys, tmp_path, "stop", host, payload) == {}  # Claude's reading is not theirs


def test_session_start_injects_the_pending_checkpoint_in_each_hosts_format(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path, clear_suggest_tokens=100000)
    for host, payload in (("codex", {"source": "clear"}), ("agy", {"conversationId": "c", "cwd": str(tmp_path)}),
                          ("opencode", {"source": "clear", "session_id": "o"})):
        save_checkpoint(tmp_path, f"t-{host}", goal=f"goal {host}", status="IN_PROGRESS")
        from ego.usecases.session import _mark_clear_pending
        _mark_clear_pending(tmp_path, f"t-{host}")
        out = _hook(monkeypatch, capsys, tmp_path, "session-start", host, payload)
        text = json.dumps(out)
        assert f"t-{host}" in text
        if host == "agy":
            assert out["injectSteps"][0]["systemMessage"]["systemMessage"]
        else:
            assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_hook_files_are_generated_for_codex_agy_and_opencode_on_request(tmp_path: Path):
    assert not {".codex/hooks.json", ".agents/hooks.json", ".opencode/plugins/ego.ts"} & set(generated_files(Config(clear_suggest_tokens=0, auto_resume=False, usage_switch_percent=0, knowledge="file")))
    cfg = Config(knowledge="file"); cfg.clear_suggest_tokens = 120000
    files = generated_files(cfg)
    codex = json.loads(files[".codex/hooks.json"])["hooks"]
    assert codex["Stop"][0]["hooks"][0]["command"] == "ego hook stop --host codex --root ."
    assert codex["SessionStart"][0]["matcher"] == "startup|resume|clear"
    agy = json.loads(files[".agents/hooks.json"])["ego"]
    assert agy["Stop"][0]["command"] == "ego hook stop --host agy --root ." and agy["Stop"][0]["type"] == "command"
    assert "session.idle" in files[".opencode/plugins/ego.ts"] and "ego" in files[".opencode/plugins/ego.ts"]
    usage_only = Config(clear_suggest_tokens=0, auto_resume=False, knowledge="file"); usage_only.usage_switch_percent = 85
    only = generated_files(usage_only)
    assert set(json.loads(only[".codex/hooks.json"])["hooks"]) == {"Stop"}  # Codex reports its limits
    assert ".agents/hooks.json" not in only and ".opencode/plugins/ego.ts" not in only  # AGY / OpenCode do not
    cfg.hosts = ["claude"]
    assert not {".codex/hooks.json", ".agents/hooks.json", ".opencode/plugins/ego.ts"} & set(generated_files(cfg))


def test_opencode_plugin_pins_the_ego_binary_and_reports_failures(monkeypatch):
    import ego.domain.templates as templates
    monkeypatch.setattr(templates.shutil, "which", lambda name: "/opt/tools/bin/ego" if name == "ego" else None)
    plugin = generated_files(Config(clear_suggest_tokens=200000))[".opencode/plugins/ego.ts"]
    assert 'const EGO_BIN = "/opt/tools/bin/ego"' in plugin and "__EGO_BIN__" not in plugin
    assert 'spawnSync(EGO_BIN, ["hook"' in plugin and "systemMessage" in plugin
    monkeypatch.setattr(templates.shutil, "which", lambda name: None)
    assert 'const EGO_BIN = "ego"' in generated_files(Config(clear_suggest_tokens=200000))[".opencode/plugins/ego.ts"]


def test_a_hook_under_ego_run_records_its_session(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".ego").mkdir()
    (tmp_path / ".ego/config.toml").write_text("clear_suggest_tokens = 120000\n")
    bound = tmp_path / ".ego/state/run/1.session.json"
    monkeypatch.setenv("EGO_RUN", "claude")
    monkeypatch.setenv("EGO_RUN_SESSION", str(bound))
    payload = json.dumps({"session_id": "s-9", "transcript_path": "/x/s-9.jsonl"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    record = read_json(bound)
    assert record["session_id"] == "s-9" and record["transcript_path"] == "/x/s-9.jsonl"
    elsewhere = tmp_path / "elsewhere.json"
    monkeypatch.setenv("EGO_RUN_SESSION", str(elsewhere))  # never a file outside this project's run state
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert main(["hook", "stop", "--root", str(tmp_path)]) == 0
    assert not elsewhere.exists()


def test_checkpoint_under_ego_run_records_the_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("EGO_RUN_ID", "r1")
    assert main(["session", "checkpoint", "--root", str(tmp_path), "--task-id", "t1", "--goal", "g"]) == 0
    assert read_json(tmp_path / ".ego/memory/handoffs/t1.json")["run"] == "r1"


def test_clear_resume_ignores_non_clear_sources(tmp_path: Path):
    for source in ("startup", "resume", "compact", None):
        assert clear_resume(tmp_path, {"source": source} if source else {}) == {}


def _fake_alter_usage(monkeypatch, payload):
    from ego.adapters import memory

    monkeypatch.delenv("EGO_ALTER_SESSION_RULES", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))
    monkeypatch.setattr(
        memory.MCPStdio, "call_tool",
        lambda self, name, args: {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]} if name == "alter_ai_usage" else {},
    )


def test_alter_project_rules_are_injected_at_session_start(tmp_path: Path, monkeypatch):
    from ego.usecases import session

    _fake_alter_usage(monkeypatch, {"scope": "project/x", "source": "project",
                                    "text": "- 日本語: 日本語で回答する\n- 検証: テストを先に確認する\n-  \nnot a rule"})
    context = session.with_alter_mandate({}, tmp_path)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(session.ALTER_SESSION_MANDATE_INJECTED)
    assert "call `alter_ai_usage`" not in context
    assert "- 日本語: 日本語で回答する" in context and "- 検証: テストを先に確認する" in context
    assert "not a rule" not in context


def test_alter_common_fallback_rules_are_not_applied(tmp_path: Path, monkeypatch):
    from ego.usecases import session

    _fake_alter_usage(monkeypatch, {"scope": "project/x", "source": "common_fallback", "text": "- 共通: 共通ルール"})
    assert session.alter_project_rules(tmp_path) == []


def test_alter_project_rules_fail_open(tmp_path: Path, monkeypatch):
    from ego.adapters import memory
    from ego.usecases import session

    _fake_alter_usage(monkeypatch, {})
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    assert session.alter_project_rules(tmp_path) == []
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))
    monkeypatch.setattr(memory.MCPStdio, "call_tool", lambda self, name, args: (_ for _ in ()).throw(memory.EgoError("MCP_TIMEOUT", name, 5)))
    assert session.alter_project_rules(tmp_path) == []
    # alter is the knowledge source: unreachable is an error that reaches the user and the model, and still never blocks.
    out = session.with_alter_mandate({}, tmp_path)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(session.ALTER_SESSION_MANDATE) and "ego ERROR: alter に接続できません" in context
    assert "alter_ai_usage" in out["systemMessage"]


def test_alter_status_distinguishes_unreachable_from_no_rules(tmp_path: Path, monkeypatch):
    from ego.adapters import memory
    from ego.usecases import session

    monkeypatch.delenv("EGO_ALTER_SESSION_RULES", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    rules, error = session.alter_status(tmp_path)
    assert rules == [] and "not configured" in error
    _fake_alter_usage(monkeypatch, {"source": "common_fallback", "text": "- common rule"})
    assert session.alter_status(tmp_path) == ([], "")  # reachable, but only common rules: not an error, not applied
    _fake_alter_usage(monkeypatch, {"source": "project", "text": "- project rule"})
    assert session.alter_status(tmp_path) == (["project rule"], "")
    assert "ego ERROR:" not in session.with_alter_mandate({}, tmp_path)["hookSpecificOutput"]["additionalContext"]


def _fake_alter_context(monkeypatch, payload, calls=None):
    from ego.adapters import memory

    monkeypatch.delenv("EGO_ALTER_PROMPT_CONTEXT", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], None))

    def call(self, name, args):
        if calls is not None:
            calls.append((name, args))
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    monkeypatch.setattr(memory.MCPStdio, "call_tool", call)


def test_alter_prompt_context_looks_up_evidence_for_the_prompt(tmp_path: Path, monkeypatch):
    from ego.usecases import session

    calls: list = []
    _fake_alter_context(monkeypatch, {"evidence": [{"title": "決定A", "excerpt": "\n## 内容\n理由 B", "source_ref": "note:1@r1"}, {"excerpt": "no title"}]}, calls)
    out = session.alter_prompt_context(tmp_path, "/work 過去の決定を探したい")
    assert "- 決定A: 内容 理由 B [note:1@r1]" in out and "no title" not in out
    assert calls == [("alter_task_context", {"task": "過去の決定を探したい", "cwd": str(tmp_path)})]


def test_alter_prompt_context_is_silent_when_nothing_to_say(tmp_path: Path, monkeypatch):
    from ego.adapters import memory
    from ego.usecases import session

    calls: list = []
    _fake_alter_context(monkeypatch, {"evidence": []}, calls)
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""
    assert session.alter_prompt_context(tmp_path, "/clear") == "" and session.alter_prompt_context(tmp_path, "ok") == ""
    assert len(calls) == 1  # the short ones never reach alter
    monkeypatch.setenv("EGO_ALTER_PROMPT_CONTEXT", "0")
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""
    monkeypatch.delenv("EGO_ALTER_PROMPT_CONTEXT")
    monkeypatch.setattr(memory.MCPStdio, "call_tool", lambda self, name, args: (_ for _ in ()).throw(memory.EgoError("MCP_TIMEOUT", name, 5)))
    assert session.alter_prompt_context(tmp_path, "十分に長いプロンプトです") == ""


def test_alter_knowledge_uses_the_host_configured_server(tmp_path: Path, monkeypatch):
    from ego.adapters import memory

    monkeypatch.delenv("EGO_ALTER_MCP_COMMAND", raising=False)
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: (["alter-mcp"], {"K": "v"}))
    assert memory.knowledge_health(tmp_path, "alter")["mcp_resolved"] is True
    monkeypatch.setattr(memory, "_alter_mcp_spec", lambda root: None)
    assert memory.knowledge_health(tmp_path, "alter")["mcp_resolved"] is False


def test_alter_memory_installs_the_user_prompt_hook(tmp_path: Path):
    from ego.domain.config import Config
    from ego.domain.templates import generated_files as render_files

    files = render_files(Config(knowledge="alter", hosts=["claude", "codex"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    assert "ego hook user-prompt" in files[".claude/settings.json"] and "ego hook user-prompt" in files[".codex/hooks.json"]
    plain = render_files(Config(knowledge="file", hosts=["claude"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    assert ".claude/settings.json" not in plain


def test_alter_memory_installs_the_same_hooks_on_every_host(tmp_path: Path):
    from ego.domain.config import Config
    from ego.domain.templates import generated_files as render_files

    files = render_files(Config(knowledge="alter", hosts=["claude", "codex", "opencode", "agy"], auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    claude = json.loads(files[".claude/settings.json"])["hooks"]
    codex = json.loads(files[".codex/hooks.json"])["hooks"]
    agy = json.loads(files[".agents/hooks.json"])["ego"]
    assert "SessionEnd" not in claude
    assert "SessionStart" in claude and "UserPromptSubmit" in claude  # the requirement and rules, then the per-prompt lookup
    assert "SessionStart" in codex and "UserPromptSubmit" in codex
    assert "session-start --host agy" in agy["SessionStart"][0]["command"]
    assert agy["PreInvocation"][0]["command"] == "ego hook user-prompt --host agy --root ."
    plugin = files[".opencode/plugins/ego.ts"]
    assert "const ALTER = true" in plugin and '"chat.message"' in plugin and '"user-prompt"' in plugin
    off = render_files(Config(knowledge="file", hosts=["opencode"], auto_resume=True, clear_suggest_tokens=0, usage_switch_percent=0))
    assert "const ALTER = false" in off[".opencode/plugins/ego.ts"]
    assert "PreInvocation" not in render_files(Config(knowledge="file", hosts=["agy"], auto_resume=True))[".agents/hooks.json"]


def test_opencode_plugin_is_valid_typescript_text():
    from ego.domain.config import Config
    from ego.domain.templates import generated_files as render_files

    plugin = render_files(Config(knowledge="alter", hosts=["opencode"]))[".opencode/plugins/ego.ts"]
    for line in plugin.splitlines():  # a string literal must never be split across lines
        assert line.count('"') % 2 == 0, line


def test_opencode_plugin_covers_resumed_sessions_and_hides_injected_context():
    from ego.domain.config import Config
    from ego.domain.templates import generated_files as render_files

    plugin = render_files(Config(knowledge="alter", hosts=["opencode"]))[".opencode/plugins/ego.ts"]
    # A resumed session fires no session.created: chat.message injects the requirement (not a checkpoint resume).
    assert 'started.has(sessionID)' in plugin and 'source: "startup"' in plugin
    # Injected context is synthetic, so it is hidden and never looked up as a user prompt again.
    assert plugin.count("synthetic: true") == 2


def test_session_start_for_alter_alone_does_not_resume_a_checkpoint(tmp_path: Path, monkeypatch, capsys):
    from ego.usecases import session

    apply_install(tmp_path, Config(knowledge="alter", auto_resume=False, clear_suggest_tokens=0, usage_switch_percent=0))
    monkeypatch.setattr(session, "clear_resume", lambda root, obj: (_ for _ in ()).throw(AssertionError("resume must stay off")))
    out = _hook(monkeypatch, capsys, tmp_path, "session-start", "claude", {"source": "clear", "cwd": str(tmp_path)})
    assert "alter_bootstrap" in out["hookSpecificOutput"]["additionalContext"]


def _agy_prompts(path: Path, *prompts: str) -> None:
    rows = []
    for i, text in enumerate(prompts):
        rows.append({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": text, "step_index": i * 2})
        rows.append({"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "ok", "step_index": i * 2 + 1})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf8")


def test_agy_prompt_is_handed_out_once_per_user_message(tmp_path: Path):
    from ego.usecases import session

    (tmp_path / ".ego").mkdir()
    transcript = tmp_path / "t.jsonl"
    payload = {"session_id": "c1", "transcript_path": str(transcript)}
    assert session.new_transcript_prompt(tmp_path, payload) == ""  # nothing written yet
    _agy_prompts(transcript, "最初の依頼です")
    assert session.new_transcript_prompt(tmp_path, payload) == "最初の依頼です"
    assert session.new_transcript_prompt(tmp_path, payload) == ""  # PreInvocation fires again between tool calls
    _agy_prompts(transcript, "最初の依頼です", "次の依頼です")
    assert session.new_transcript_prompt(tmp_path, payload) == "次の依頼です"
    assert session.new_transcript_prompt(tmp_path, {"session_id": "c2", "transcript_path": str(transcript)}) == "次の依頼です"  # per session
    assert session.new_transcript_prompt(tmp_path, {"transcript_path": str(transcript)}) == ""


def test_agy_user_prompt_hook_injects_alter_evidence_as_a_system_message(tmp_path: Path, monkeypatch, capsys):
    apply_install(tmp_path, Config(knowledge="alter", clear_suggest_tokens=0, usage_switch_percent=0))
    transcript = tmp_path / "t.jsonl"
    _agy_prompts(transcript, "過去の決定を探したい")
    _fake_alter_context(monkeypatch, {"evidence": [{"title": "決定A", "excerpt": "理由", "source_ref": "note:1@r1"}]})
    payload = ({"conversationId": "c9", "transcriptPath": str(transcript), "cwd": str(tmp_path)})
    for expect in (True, False):  # the second model call of the same prompt adds nothing
        out = _hook(monkeypatch, capsys, tmp_path, "user-prompt", "agy", payload)
        if expect:
            assert "決定A" in out["injectSteps"][0]["systemMessage"]["systemMessage"]
        else:
            assert out == {}


def _lines(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_claude_is_measured_from_its_transcript(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "my.proj"
    path = tmp_path / "cfg" / "projects" / str(root).replace("/", "-").replace(".", "-").replace("_", "-") / "a.jsonl"
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 5}
    _lines(path, [{"type": "user", "message": {"content": "hi"}},
                  {"type": "assistant", "message": {"usage": usage, "stop_reason": "tool_use"}}])
    assert measure("claude", root, 0) == Reading(str(path), 90015, False)  # a tool call is pending: not idle
    _lines(path, [{"type": "assistant", "message": {"usage": usage, "stop_reason": "end_turn"}},
                  {"type": "system", "subtype": "turn_duration"}])
    assert measure("claude", root, 0) == Reading(str(path), 90015, True)
    assert measure("claude", root, time.time() + 60) is None  # written before this run started: someone else's


def _two_claude_sessions(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """Sessions a (150k, older) and b (10k, written last) of one project, as two parallel runs leave them."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "proj"
    folder = tmp_path / "cfg" / "projects" / str(root).replace("/", "-").replace(".", "-").replace("_", "-")
    def transcript(name, tokens):
        usage = {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        return _lines(folder / f"{name}.jsonl", [{"type": "assistant", "message": {"usage": usage, "stop_reason": "end_turn"}}])
    a, b = transcript("a", 150000), transcript("b", 10000)
    os.utime(a, (time.time() - 60, time.time() - 60))
    return root, a, b


def test_parallel_claude_runs_each_read_their_own_session(tmp_path: Path, monkeypatch):
    root, a, b = _two_claude_sessions(tmp_path, monkeypatch)
    runs = root / ".ego" / "state" / "run"
    write_json(runs / "111.session.json", {"session_id": "a", "transcript_path": str(a)})
    write_json(runs / "222.session.json", {"session_id": "b", "transcript_path": str(b)})
    assert measure("claude", root, 0, None, runs / "111.session.json").tokens == 150000
    assert measure("claude", root, 0, None, runs / "222.session.json").tokens == 10000


def test_an_unbound_run_does_not_guess_beside_a_live_run(tmp_path: Path, monkeypatch):
    root, a, b = _two_claude_sessions(tmp_path, monkeypatch)
    runs = root / ".ego" / "state" / "run"
    own, other = runs / "999999.session.json", runs / f"{os.getpid()}.session.json"
    write_json(own, {})
    write_json(other, {})
    assert measure("claude", root, 0, None, own) is None  # the newest record may be the other run's
    other.unlink()
    assert measure("claude", root, 0, None, own).key == str(b)  # alone: the newest record, as before


def test_codex_is_measured_from_the_rollout_of_this_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root = tmp_path / "proj"
    def rollout(name, cwd, events):
        return _lines(tmp_path / "sessions" / "2026" / "11" / "11" / f"rollout-{name}.jsonl",
                      [{"type": "session_meta", "payload": {"cwd": cwd}}] + [{"type": "event_msg", "payload": e} for e in events])
    count = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 247583, "cached_input_tokens": 247296}}}
    mine = rollout("a", str(root), [{"type": "task_started"}, count, {"type": "task_complete"}])
    rollout("b", "/elsewhere", [count])
    assert measure("codex", root, 0) == Reading(str(mine), 247583, True)
    rollout("a", str(root), [count, {"type": "task_complete"}, {"type": "task_started"}])
    assert measure("codex", root, 0).idle is False


def test_opencode_is_measured_from_its_database(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = tmp_path / "proj"
    assert measure("opencode", root, 0) is None
    (tmp_path / "opencode").mkdir()
    con = sqlite3.connect(tmp_path / "opencode" / "opencode.db")
    con.execute("create table session (id text, directory text, parent_id text, time_updated integer)")
    con.execute("create table message (id text, session_id text, time_created integer, data text)")
    now = int(time.time() * 1000)
    con.execute("insert into session values ('ses_1', ?, null, ?)", (str(root), now))
    con.execute("insert into session values ('ses_child', ?, 'ses_1', ?)", (str(root), now + 5))
    done = {"role": "assistant", "finish": "stop", "time": {"completed": now},
            "tokens": {"input": 12000, "output": 900, "cache": {"read": 30000, "write": 500}}}
    con.execute("insert into message values ('m1', 'ses_1', 1, ?)", (json.dumps(done),))
    con.commit()
    assert measure("opencode", root, 0) == Reading("ses_1", 42500, True)
    con.execute("insert into message values ('m2', 'ses_1', 2, ?)", (json.dumps({"role": "user"}),))
    con.commit(); con.close()
    assert measure("opencode", root, 0) == Reading("ses_1", 42500, False)  # a turn is running


def test_parallel_codex_runs_each_read_their_own_rollout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root = tmp_path / "proj"
    def rollout(name, tokens):
        count = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": tokens}}}
        return _lines(tmp_path / "sessions" / "2026" / "11" / "11" / f"rollout-{name}.jsonl",
                      [{"type": "session_meta", "payload": {"cwd": str(root)}}]
                      + [{"type": "event_msg", "payload": e} for e in (count, {"type": "task_complete"})])
    a = rollout("a", 247583)
    os.utime(a, (time.time() - 60, time.time() - 60))
    rollout("b", 1000)  # the other run's session, written last
    bound = root / ".ego" / "state" / "run" / "111.session.json"
    write_json(bound, {"session_id": "a", "transcript_path": str(a)})
    assert measure("codex", root, 0, None, bound).tokens == 247583


def test_parallel_opencode_runs_each_read_their_own_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = tmp_path / "proj"
    (tmp_path / "opencode").mkdir()
    con = sqlite3.connect(tmp_path / "opencode" / "opencode.db")
    con.execute("create table session (id text, directory text, parent_id text, time_updated integer)")
    con.execute("create table message (id text, session_id text, time_created integer, data text)")
    now = int(time.time() * 1000)
    def done(tokens):
        return json.dumps({"role": "assistant", "finish": "stop", "time": {"completed": now}, "tokens": {"input": tokens}})
    con.execute("insert into session values ('ses_1', ?, null, ?)", (str(root), now))
    con.execute("insert into session values ('ses_2', ?, null, ?)", (str(root), now + 5))  # the other run's, newer
    con.execute("insert into message values ('m1', 'ses_1', 1, ?)", (done(42500),))
    con.execute("insert into message values ('m2', 'ses_2', 1, ?)", (done(1000),))
    con.commit(); con.close()
    bound = root / ".ego" / "state" / "run" / "111.session.json"
    write_json(bound, {"session_id": "ses_1", "transcript_path": None})
    assert measure("opencode", root, 0, None, bound) == Reading("ses_1", 42500, True)


def test_agy_reports_through_its_status_line(tmp_path: Path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    payload = {"conversation_id": "c1", "agent_state": "idle",
               "context_window": {"context_window_size": 1000000, "used_percentage": 12.5, "current_usage": None}}
    assert record_context(payload, state) == "ego · ctx 125k"
    assert measure("agy", tmp_path, 0, state) == Reading("c1", 125000, True)
    monkeypatch.setenv("EGO_RUN_STATE", str(state))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({**payload, "agent_state": "running"})))
    assert main(["hook", "context-probe", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "ego · ctx 125k"
    assert measure("agy", tmp_path, 0, state).idle is False
    assert measure("agy", tmp_path, 0, None) is None


def test_an_unmeasured_context_is_shown_as_a_dash(tmp_path: Path):
    assert ctx_label(None) == "ego · ctx —" and ctx_label(0) == "ego · ctx —"
    assert ctx_label(125000) == "ego · ctx 125k"
    empty = {"context_window": {"context_window_size": 0, "used_percentage": 0}}
    assert record_context(empty, tmp_path / "state.json") == "ego · ctx —"


def test_the_bar_keeps_the_last_row_and_the_host_gets_the_rest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptyrun, "BAR_QUIET", 0)
    screen_master, screen = pty.openpty()
    ptyrun._set_size(screen, 10, 40)
    read_end, write_end = os.pipe()
    chunks: list[bytes] = []

    def drain():
        while True:
            try:
                data = os.read(screen_master, 4096)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    host = ("import fcntl,struct,termios,time\n"
            "time.sleep(0.4)\n"
            "rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0' * 8))\n"
            "print('SIZE', rows, cols, flush=True)\n")
    try:
        code, rotations = run("claude", [sys.executable, "-c", host], tmp_path, 40000, stdin_fd=read_end,
                              stdout_fd=screen, probe=lambda *_: Reading("s1", 12000, False),
                              poll=0.05, quiet_output=0.05, quiet_keys=0)
    finally:
        os.close(read_end)
        os.close(write_end)
        os.close(screen)
    reader.join(5)
    out = b"".join(chunks)
    assert (code, rotations) == (0, 0)
    assert b"SIZE 9 40" in out  # the host draws in 9 rows; the tenth is ego's
    assert b"\x1b[1;9r" in out and b"\x1b[10;1H" in out  # scrolling stops above the bar row
    assert "ego · ctx 12k".encode() in out  # the probe's reading reached the bar
    assert out.endswith(b"\x1b[r\x1b[10;1H\x1b[2K")  # the region is released on exit
    os.close(screen_master)


def test_a_draft_in_the_input_box_is_noticed():
    keys = InputState()
    keys.feed(b"\x1b[I\x1b[A\x1b]11;rgb:0000/0000/0000\x07", 1)  # focus, arrow, a terminal's colour reply
    assert not keys.dirty
    keys.feed(b"fix th", 2)
    assert keys.dirty and keys.last_key == 2
    keys.feed(b"e bug\r", 3)
    assert not keys.dirty
    keys.feed(b"\x1b[97u\x1b[98;2u", 4)  # kitty protocol: "a", shift+"b"
    assert keys.dirty
    keys.feed(b"\x1b[13;2u", 5)          # shift+enter adds a line, it does not send
    assert keys.dirty
    keys.feed(b"\x1b[13u", 6)
    assert not keys.dirty
    keys.feed(b"x\x1b[99;5u", 7)          # ctrl+c
    assert not keys.dirty


def test_pasted_text_cannot_carry_keystrokes():
    out = _paste("Goal: ship\nNext: \x1b[201~\rrm -rf /\x03")
    assert out.startswith(b"\x1b[200~") and out.endswith(b"\x1b[201~")
    assert not any(b < 32 for b in out[6:-6])


def test_rotation_saves_first_and_clears_only_after_a_new_checkpoint(tmp_path: Path):
    rot = Rotator(tmp_path, "opencode", 100000)
    assert rot.step(None, True, 0) == []
    assert rot.step(Reading("s1", 99000, True), True, 1) == []
    assert rot.step(Reading("s1", 150000, False), True, 2) == []   # still working
    assert rot.step(Reading("s1", 150000, True), False, 3) == []   # the user is typing
    save_checkpoint(tmp_path, "old", goal="earlier work")           # saved before we asked: not this state
    time.sleep(0.05)
    typed = rot.step(Reading("s1", 150000, True), True, time.time())
    assert b"ego session checkpoint" in typed[0][1] and typed[-1][1] == b"\r"
    assert rot.step(Reading("s1", 150000, True), True, time.time()) == []
    save_checkpoint(tmp_path, "feat", goal="ship it", next_steps=["run tests"])
    assert rot.step(Reading("s1", 151000, False), True, time.time()) == []  # the save turn has not ended
    typed = rot.step(Reading("s1", 151000, True), True, time.time())
    assert [k for _, k in typed][:2] == [b"/new", b"\r"]
    assert b"Resume task feat" in typed[2][1] and b"run tests" in typed[2][1] and rot.rotations == 1
    assert rot.step(Reading("s1", 151000, True), True, time.time()) == []   # the cleared session still reads as full
    assert rot.step(Reading("s2", 3000, True), True, time.time()) == []


def test_a_resumed_session_already_over_the_limit_is_not_rotated_again(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 1000)  # below what a fresh session costs: clearing cannot get under it
    assert rot.step(Reading("s1", 70000, True), True, time.time())
    save_checkpoint(tmp_path, "feat", goal="ship it")
    assert rot.step(Reading("s1", 72000, True), True, time.time()) and rot.rotations == 1
    assert rot.step(Reading("s2", None, False), True, time.time()) == []    # the resume turn is running
    for _ in range(3):                                                      # resumed, idle, nobody typing
        assert rot.step(Reading("s2", 50000, True), True, time.time()) == []
    assert rot.step(Reading("s2", 60000, True), True, time.time()) == []    # ordinary growth
    assert rot.step(Reading("s2", 63000, True), True, time.time())          # a quarter larger: clearing frees context again
    assert rot.rotations == 1


def test_without_a_checkpoint_nothing_is_cleared(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 100000)
    assert rot.step(Reading("s1", 120000, True), True, 1000)
    assert rot.step(Reading("s1", 121000, True), True, 1000 + ptyrun.SAVE_TIMEOUT + 1) == []
    assert rot.step(Reading("s1", 130000, True), True, 2000) == []      # asks again only after real growth
    assert rot.step(Reading("s1", 160000, True), True, 2001)
    assert rot.rotations == 0


def test_a_run_waits_only_for_its_own_checkpoint(tmp_path: Path):
    rot = Rotator(tmp_path, "claude", 100000, "mine")
    asked = time.time() - 10
    assert rot.step(Reading("s1", 120000, True), True, asked)  # asks this session to save
    save_checkpoint(tmp_path, "task-b", goal="b", run="other")  # a parallel run's session saved first
    assert rot.step(Reading("s1", 120000, True), True, asked + 20) == []
    save_checkpoint(tmp_path, "task-a", goal="a", run="mine")
    typed = b"".join(keys for _, keys in rot.step(Reading("s1", 120000, True), True, asked + 30))
    assert b"Resume task task-a" in typed and b"task-b" not in typed


HOST = r"""
import os, sys, tty
tty.setraw(0)
log = open(sys.argv[1], "ab", buffering=0)
while True:
    data = os.read(0, 4096)
    log.write(data)
    if not data or b"Q" in data:
        break
"""


def test_the_wrapped_cli_is_typed_into(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptyrun, "AFTER_CLEAR", 0.1)
    log = tmp_path / "typed.log"
    script = tmp_path / "host.py"
    script.write_text(HOST)
    read_end, write_end = os.pipe()
    sink = os.open(os.devnull, os.O_WRONLY)
    deadline = time.time() + 30

    def probe(host, root, since, state, session=None):
        typed = log.read_bytes() if log.exists() else b""
        if b"Resume task feat" in typed or time.time() > deadline:
            if typed.endswith(b"\r") or time.time() > deadline:
                os.write(write_end, b"Q")
            return Reading("s2", 100, True)
        if b"ego session checkpoint" in typed and not (tmp_path / ".ego/memory/handoffs/feat.json").exists():
            save_checkpoint(tmp_path, "feat", goal="ship it", next_steps=["run tests"])
        return Reading("s1", 50000, True)

    try:
        code, rotations = run("claude", [sys.executable, str(script), str(log)], tmp_path, 40000, stdin_fd=read_end,
                              stdout_fd=sink, probe=probe, poll=0.05, quiet_output=0.05, quiet_keys=0)
    finally:
        for fd in (read_end, write_end, sink):
            os.close(fd)
    typed = log.read_bytes()
    assert (code, rotations) == (0, 1)
    save, clear, resume = typed.index(b"ego session checkpoint"), typed.index(b"/clear\r"), typed.index(b"Resume task feat")
    assert save < clear < resume and b"run tests" in typed


def test_hooks_stand_down_inside_ego_run(project: Path, monkeypatch, capsys):
    cfg_path = project / ".ego/config.toml"
    cfg_path.write_text(cfg_path.read_text().replace('knowledge = "file"', 'knowledge = "alter"'))
    save_checkpoint(project, "feat", goal="ship it")
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "settings.json").write_text('{"hooks": "ego hook session-start"}')
    assert arm_auto_resume(project, "feat") is True
    monkeypatch.setenv("EGO_RUN", "claude")
    assert arm_auto_resume(project, "feat") is False
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"source": "clear"})))
    assert main(["hook", "session-start", "--root", str(project)]) == 0
    # The checkpoint is not resumed again; only the alter requirement (memory defaults to alter) remains.
    from ego.usecases.session import ALTER_SESSION_MANDATE
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["additionalContext"] == ALTER_SESSION_MANDATE


def test_run_refuses_without_a_threshold_or_a_terminal(project: Path, capsys):
    assert main(["run", "claude", "--root", str(project)]) == 2
    assert "CLEAR_THRESHOLD_UNSET" in capsys.readouterr().err
    assert main(["run", "claude", "--clear-tokens", "50000", "--root", str(project), "--", "--root", "x"]) == 3
    assert "RUN_NEEDS_TTY" in capsys.readouterr().err


def test_yolo_adds_each_clis_own_flag_once():
    assert ptyrun.with_yolo("claude", ["--model", "x"]) == ["--dangerously-skip-permissions", "--model", "x"]
    assert ptyrun.with_yolo("codex", []) == ["--dangerously-bypass-approvals-and-sandbox"]
    assert ptyrun.with_yolo("opencode", []) == ["--auto"]
    assert ptyrun.with_yolo("agy", ["--dangerously-skip-permissions"]) == ["--dangerously-skip-permissions"]
    assert ptyrun.with_yolo("codex", ["--yolo"]) == ["--yolo"]  # the CLI's own alias was already given


def test_yolo_comes_from_the_flag_or_the_config(project: Path, monkeypatch):
    seen = []
    monkeypatch.setattr(ptyrun, "run_host", lambda host, args, root, threshold, yolo=False: seen.append((host, args, yolo)) or 0)
    assert main(["run", "claude", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert main(["run", "codex", "--no-yolo", "--root", str(project), "--clear-tokens", "100000", "--", "--model", "x"]) == 0
    config = project / ".ego/config.toml"
    config.write_text(config.read_text().replace("run_yolo = true", "run_yolo = false"))
    assert main(["run", "agy", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert main(["run", "agy", "--yolo", "--root", str(project), "--clear-tokens", "100000"]) == 0
    assert seen == [("claude", [], True), ("codex", ["--model", "x"], False), ("agy", [], False), ("agy", [], True)]


def _attempt(host, usage, cost=None):
    return {"execution": {"host": host, "cost_usd": cost, "usage": usage}}


def test_tokens_include_cache_traffic_and_format_compactly():
    assert tokens_of({"input_tokens": 40, "output_tokens": 300, "cache_read_tokens": 52000, "cache_creation_tokens": 9000}) == 61340
    assert tokens_of(None) == 0
    assert [fmt_tokens(n) for n in (0, 999, 1200, 45600, 2_500_000)] == ["0", "999", "1.2k", "45.6k", "2.50M"]


def test_usage_text_shows_only_the_phases_that_ran():
    assert usage_text({"plan_tokens": 1200}) == "plan 1.2k tokens"
    assert usage_text({"work_tokens": 0, "by_task": {}}) == "work 0 tokens"
    line = usage_text({"plan_tokens": 1200, "work_tokens": 45600, "by_task": {"A": 8100, "B": 2000}})
    assert line == "plan 1.2k tokens · work 45.6k tokens (task A 8.1k, task B 2.0k) · total 46.8k tokens"
    assert usage_text({}) == "tokens: no model calls"


def test_run_usage_splits_plan_and_each_task_and_counts_the_planner_cost():
    record = {"planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01},
              "results": {
                  "A": {"attempts": [_attempt("claude", {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 100}, 0.1),
                                     _attempt("claude", {"input_tokens": 1, "output_tokens": 2}, 0.05)]},
                  "B": {"attempts": [_attempt("local", None)]},
                  "C": {"attempts": [_attempt("claude", {"input_tokens": 10, "output_tokens": 20}, 0.0)]}}}
    total = usage_summary(record)
    assert total["planned"] is True and total["plan_tokens"] == 1200
    assert total["by_task"] == {"A": 112 + 3, "C": 30} and total["work_tokens"] == 145
    assert total["model_calls"] == 4 and total["cost_usd"] == 0.16 and total["cost_known"] is True
    assert summary_line(total) == "plan 1.2k tokens · work 145 tokens (task A 115, task C 30) · total 1.3k tokens"


def test_run_without_planner_or_model_calls_reports_zero_work_and_no_plan():
    assert summary_line(usage_summary({"results": {"gen": {"attempts": [_attempt("local", None)]}}})) == "work 0 tokens"


def test_compact_result_prints_the_line_next_to_the_totals():
    result = {"run_id": "run-1", "final_status": "PASSED", "node_status": {"A": "PASSED"}, "changed_files": [],
              "results": {}, "planner_usage": {"input_tokens": 1000, "output_tokens": 200},
              "usage": usage_summary({"planner_usage": {"input_tokens": 1000, "output_tokens": 200},
                                      "results": {"A": {"attempts": [_attempt("claude", {"input_tokens": 9000})]}}})}
    out = compact_result(result)
    assert out["usage_text"] == "plan 1.2k tokens · work 9.0k tokens (task A 9.0k) · total 10.2k tokens"


def test_cli_run_reports_the_plan_phase_recorded_in_the_task_file(tmp_path: Path, capsys):
    (tmp_path / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('OK')\n")
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"goal": "g", "planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 300},
                                "nodes": [{"id": "gen", "kind": "code", "argv": ["python3", "gen.py"],
                                           "write_allowlist": ["result.txt"]}]}))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(task)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["usage_text"] == "plan 1.5k tokens · work 0 tokens · total 1.5k tokens"


def test_cli_plan_writes_planning_cost_into_the_task_file(tmp_path: Path, monkeypatch, capsys):
    spec = {"goal": "g", "nodes": [{"id": "impl", "kind": "agent", "prompt": "p", "write_allowlist": ["src/**"]}]}

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        return RunResult("PASS", json.dumps(spec), "", 0, 0.0, host,
                         usage={"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 300})

    monkeypatch.setattr(planner, "run_agent", fake_agent)
    out_file = tmp_path / "task.json"
    assert main(["work", "plan", "--root", str(tmp_path), "--goal", "g", "--host", "claude", "--execute", "--out", str(out_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["usage_text"] == "plan 1.5k tokens" and printed["planner_usage"]["input_tokens"] == 1000
    assert json.loads(out_file.read_text())["planner_usage"]["output_tokens"] == 200


def test_loop_ledger_totals_plan_and_work_tokens_across_iterations(tmp_path: Path):
    (tmp_path / "make_all.py").write_text("from pathlib import Path\nPath('a.txt').write_text('A')\n")
    (tmp_path / "check.py").write_text("from pathlib import Path\nimport sys\nsys.exit(0 if Path('a.txt').exists() else 1)\n")
    calls = []

    def planner_stub(goal, history, workdir):
        calls.append(history)
        return {"task": {"goal": goal, "nodes": [{"id": "make", "kind": "code", "argv": ["python3", "make_all.py"],
                                                  "write_allowlist": ["*.txt"]}]},
                "planner_usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01}}

    result = run_loop(tmp_path, "make a", done_checks=[["python3", "check.py"]], planner=planner_stub,
                      scope=["*.txt"], plan_first=True)
    assert result["status"] == "VERIFIED" and len(calls) == 1
    assert result["usage"]["plan_tokens"] == 1200 and result["usage"]["planned"] is True
    assert result["usage_text"] == "plan 1.2k tokens · work 0 tokens · total 1.2k tokens"


def test_resume_of_a_run_whose_task_file_carries_plan_usage_does_not_mismatch(tmp_path: Path, capsys):
    (tmp_path / "gen.py").write_text("from pathlib import Path\nPath('result.txt').write_text('OK')\n")
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"goal": "g", "planner_usage": {"input_tokens": 10},
                                "nodes": [{"id": "gen", "kind": "code", "argv": ["python3", "gen.py"],
                                           "write_allowlist": ["result.txt"]}]}))
    assert main(["work", "run", "--root", str(tmp_path), "--task", str(task)]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main(["work", "resume", run_id, "--root", str(tmp_path), "--task", str(task)]) == 0
    assert json.loads(capsys.readouterr().out)["final_status"] == "PASSED"


def test_codex_cached_tokens_are_not_counted_twice():
    from ego.adapters.runner import parse_host_usage
    raw = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 10}})
    usage = parse_host_usage("codex", raw)
    assert usage["input_tokens"] == 100 and usage["cache_read_tokens"] == 900
    assert tokens_of(usage) == 1010


def test_loop_accounts_for_a_planner_call_whose_plan_was_rejected(tmp_path: Path, monkeypatch):
    (tmp_path / "check.py").write_text("import sys\nsys.exit(1)\n")

    def fake_agent(host, prompt, cwd, timeout=180, **kw):
        return RunResult("PASS", "[1, 2]", "", 0, 0.0, host, cost_usd=0.25,
                         usage={"input_tokens": 1000, "output_tokens": 200})

    monkeypatch.setattr(planner, "run_agent", fake_agent)
    result = run_loop(tmp_path, "g", done_checks=[["python3", "check.py"]], host="claude", execute_planner=True,
                      plan_first=True, max_iterations=3)
    assert result["iterations"][0]["status"] == "PLAN_REJECTED"
    assert "PLAN_INVALID_JSON" in result["iterations"][0]["failures"][0]["feedback"]
    assert result["cost_usd"] == 0.5 and result["usage"]["plan_tokens"] == 2400  # two calls, then STALLED
