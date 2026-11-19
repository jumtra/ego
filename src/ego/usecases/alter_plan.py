"""Hand-over of ego task plans to alter: push, wait for the run button, pull as a task file, report results."""
from __future__ import annotations
import json
import time
from pathlib import Path
from pydantic import ValidationError
from ..adapters.alter_plan import AlterPlanClient, task_to_spec, spec_to_task
from ..domain.config import load_config
from ..domain.task_schema import AlterPushNodeSpec
from .work import validate_task, status
from ..shared.util import EgoError, _id, now, read_json, safe_path, write_json

WAIT_STATES = ('ready', 'taken', 'running', 'passed')
MAX_CONSECUTIVE_ERRORS = 3
FAILURE_MAX = 300


def plan_field_errors(task: dict) -> list[str]:
    # Same required fields as alter's ego plan validation, so an incomplete plan fails here instead of in alter.
    errors: list[str] = []
    for node in task['nodes']:
        try:
            AlterPushNodeSpec.model_validate(node)
        except ValidationError as err:
            for item in err.errors():
                msg = str(item.get('msg', '')).removeprefix('Value error, ')
                errors.extend(part.strip() for part in msg.split(';') if part.strip())
    return errors


def push(root: Path, task_file: str, slug: str | None = None) -> dict:
    path = Path(task_file)
    task = read_json(path)
    if task is None: raise EgoError('TASK_MISSING', task_file, 3)
    limits = load_config(root).plan_schema_limits()
    validate_task(task, limits=limits)
    errors = plan_field_errors(task)
    if errors: raise EgoError('INVALID_TASK', '; '.join(errors), 2)
    slug = _id(slug or path.stem)
    client = AlterPlanClient(root)
    result = client.create(task_to_spec(task, slug, limits=limits), client.scope())
    write_json(safe_path(root, f'.ego/state/alter_plans/{slug}.json'),
               {'plan_id': result['plan_id'], 'node_tasks': result['node_tasks'], 'slug': slug,
                'task_file': task_file, 'pushed_at': now()})
    return {'plan_id': result['plan_id'], 'node_tasks': result['node_tasks'], 'state': 'draft'}


def wait(root: Path, plan_id: str, interval: float = 5.0, timeout: float = 0, write=print, sleep=time.sleep) -> dict:
    client = AlterPlanClient(root)
    previous, errors, started = None, 0, time.monotonic()
    while True:
        try:
            plan = client.get(plan_id)
            errors = 0
        except EgoError:
            errors += 1
            if errors >= MAX_CONSECUTIVE_ERRORS: raise
            plan = None
        if plan is not None:
            state = plan.get('state')
            if state != previous:
                write(json.dumps({'event': 'plan_state', 'plan_id': plan_id, 'state': state, 'at': now()}, ensure_ascii=False))
                previous = state
            if state in WAIT_STATES: return plan
        if timeout > 0 and time.monotonic() - started >= timeout: raise EgoError('ALTER_PLAN_TIMEOUT', plan_id, 5)
        sleep(interval)


def pull(root: Path, plan_id: str, out: str, run_id: str) -> dict:
    client = AlterPlanClient(root)
    plan = client.get(plan_id)
    if plan.get('state') != 'ready':
        raise EgoError('ALTER_PLAN_NOT_READY', f"state is {plan.get('state')}; press the run button in alter first", 3)
    if plan.get('errors'): raise EgoError('ALTER_PLAN_INVALID', '; '.join(plan['errors']), 2)
    limits = load_config(root).plan_schema_limits()
    task = spec_to_task(plan['spec'], plan_id, plan['spec_hash'], limits=limits)
    validate_task(task, limits=limits)
    client.claim(plan_id, run_id, plan['spec_hash'])
    write_json(Path(out), task)
    runs = plan.get('runs') or []
    from_run = runs[-1]['run_id'] if runs and runs[-1].get('state') in ('failed', 'interrupted') else None
    return {'task_file': out, 'run_id': run_id, 'from_run': from_run, 'nodes': len(task['nodes'])}


def report_payload(record: dict) -> tuple[str, dict]:
    final = record.get('final_status')
    state = 'passed' if final == 'PASSED' else 'interrupted' if 'INTERRUPTED' in str(final) else 'running' if 'RUNNING' in str(final) else 'failed'
    nodes = {}
    for nid, r in record.get('results', {}).items():
        attempts = r.get('attempts', [])
        last = (attempts or [{}])[-1]
        failed = [c for c in last.get('checks', []) if c.get('status') != 'PASS']
        failure = ''
        if failed:
            failure = str(failed[-1].get('output') or failed[-1].get('stderr') or '')[:FAILURE_MAX]
        nodes[nid] = {'status': r.get('status'), 'attempts': len(attempts), 'failure': failure}
    return state, nodes


def sync(root: Path, run_id: str, task_file: str) -> dict:
    ref = (read_json(Path(task_file), {}) or {}).get('source', {}).get('alter_plan')
    if not ref: raise EgoError('NOT_AN_ALTER_PLAN', task_file, 2)
    state, nodes = report_payload(status(root, run_id))
    AlterPlanClient(root).report(ref['plan_id'], run_id, state, nodes)
    return {'plan_id': ref['plan_id'], 'state': state}
