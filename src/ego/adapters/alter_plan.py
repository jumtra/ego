"""Bridge between ego task JSON and alter node-graph plans (alter_ego_plan MCP tool)."""
from __future__ import annotations
import json
import shlex
from pathlib import Path
from ..shared.util import EgoError
from ..domain.task_schema import MAX_PROMPT_CHARS as PROMPT_MAX, PlanSchemaLimits, validate_alter_plan_spec
from .memory import MCPStdio, _alter_mcp_spec, ALTER_HOST_HINT

NODE_EXTRA_KEYS=('kind','role','reviews','host','hosts','model','models','agent','mcp','max_retries','strategy','fallback_on','argv','outputs','notes','timeout_seconds')
TASK_EXTRA_KEYS=('defaults','instructions_file')

def task_to_spec(task: dict, slug: str, *, limits: PlanSchemaLimits | None = None) -> dict:
    spec={'slug':slug,'goal':task.get('goal',''),'done':task.get('done',''),'instructions':task.get('instructions',''),
          'extra':{k:task[k] for k in TASK_EXTRA_KEYS if k in task},'nodes':[_node_to_spec(n) for n in task.get('nodes',[])]}
    validate_alter_plan_spec(spec, limits=limits)
    return spec

def _node_to_spec(n: dict) -> dict:
    prompt=n.get('prompt','')
    return {'node_id':n['id'],'prompt':prompt,'depends_on':list(n.get('depends_on',[])),'intent':n.get('intent',''),'requirement':n.get('requirement',{}),
            'steps':n.get('steps',[]),'where':n.get('write_allowlist',[]),'done_when':n.get('done_when',[]),
            'checks':[shlex.join(c) if isinstance(c,list) else str(c) for c in n.get('checks',[])],
            'non_goals':n.get('non_goals',[]),'assumptions':n.get('assumptions',[]),'provides':n.get('provides',[]),
            'extra':{k:n[k] for k in NODE_EXTRA_KEYS if k in n}}

def spec_to_task(spec: dict, plan_id: str, spec_hash: str, *, limits: PlanSchemaLimits | None = None) -> dict:
    validate_alter_plan_spec(spec, limits=limits)
    task={'goal':spec['goal'],'instructions':spec.get('instructions',''),**spec.get('extra',{}),
          'source':{'alter_plan':{'plan_id':plan_id,'spec_hash':spec_hash,'slug':spec['slug']}},'nodes':[_spec_to_node(n) for n in spec['nodes']]}
    if spec.get('done'): task['done']=spec['done']
    return task

def _spec_to_node(s: dict) -> dict:
    extra=s.get('extra',{})
    kind=extra.get('kind','agent')
    node={'id':s['node_id'],'kind':kind,'depends_on':list(s.get('depends_on',[])),'prompt':s.get('prompt',''),
          'write_allowlist':s.get('where',[]),'checks':[shlex.split(c) for c in s.get('checks',[])]}
    if s.get('intent'): node['intent']=s['intent']
    if s.get('requirement'): node['requirement']=s['requirement']
    if s.get('steps'): node['steps']=s['steps']
    if s.get('done_when'): node['done_when']=s['done_when']
    for k in ('non_goals','assumptions','provides'):
        if s.get(k): node[k]=s[k]
    if kind not in ('code','gate') and 'role' not in extra: node['role']='coder'
    return {**node, **extra}


def _payload(result: dict) -> dict:
    if not isinstance(result, dict): return {}
    if isinstance(result.get('structuredContent'), dict): return result['structuredContent']
    texts = [b.get('text', '') for b in result.get('content', []) if isinstance(b, dict) and b.get('type') == 'text']
    if result.get('isError'): raise EgoError('ALTER_ERROR', (texts[0] if texts else '')[:400], 5)
    for text in texts:
        if text.startswith('{'):
            try: return json.loads(text)
            except ValueError: continue
    return {}

class AlterPlanClient:
    def __init__(self, root: Path, timeout: int = 30):
        spec = _alter_mcp_spec(root)
        if spec is None: raise EgoError('ALTER_UNAVAILABLE', ALTER_HOST_HINT, 3)
        cmd, env = spec
        self.mcp = MCPStdio(cmd, timeout=timeout, env=env)
        self.root = root

    def _call(self, name: str, args: dict) -> dict:
        try: result = self.mcp.call_tool(name, args)
        except EgoError as exc:
            if '409' in str(exc): raise EgoError('ALTER_PLAN_CONFLICT', str(exc), 5) from exc
            raise
        return _payload(result)

    def scope(self) -> str:
        return self._call('alter_bootstrap', {'cwd': str(self.root)})['scope']

    def create(self, spec: dict, scope: str) -> dict:
        return self._call('alter_ego_plan', {'action': 'create', 'scope': scope, **spec})

    def get(self, plan_id: str) -> dict:
        return self._call('alter_ego_plan', {'action': 'get', 'plan_id': plan_id})

    def claim(self, plan_id: str, run_id: str, spec_hash: str) -> dict:
        return self._call('alter_ego_plan', {'action': 'claim', 'plan_id': plan_id, 'run_id': run_id, 'spec_hash': spec_hash})

    def report(self, plan_id: str, run_id: str, state: str, nodes: dict) -> dict:
        return self._call('alter_ego_plan', {'action': 'report', 'plan_id': plan_id, 'run_id': run_id, 'state': state, 'nodes': nodes})
