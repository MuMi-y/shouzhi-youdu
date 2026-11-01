"""Bounded recovery of the current bridge run after missing process output.

These observations never authorize a bank write. Only the host attempt store
proves dispatch; ambiguous or truncated files remain explicitly incomplete.
"""
import json
from pathlib import Path
from uuid import UUID
from .handoff_contracts import safe


def inventory(runtime):
    root=Path(runtime)/'dsh'/'runs'
    if not root.exists():
        return set()
    result=set()
    for child in root.iterdir():
        if len(result)>=4096:
            return None
        result.add(child.name)
    return result


def recover(runtime, before, prompt, session_id, scope='bank'):
    failed={'trace':[], 'trace_recovery':{'complete':False,'reason':'UNAVAILABLE'}}
    if before is None:
        return failed
    root=(Path(runtime)/'dsh'/'runs').resolve()
    try:
        after=inventory(runtime)
        if after is None or len(after-before)!=1:
            return {**failed,'trace_recovery':{'complete':False,'reason':'AMBIGUOUS_OR_MISSING_RUN'}}
        name=next(iter(after-before))
        if str(UUID(name))!=name:
            return failed
        run=root/name
        if run.resolve()!=run or not run.is_dir():
            return failed
        request=run/'request.json'; trace=run/'trace.jsonl'
        if request.resolve()!=request or request.stat().st_size>2*1024*1024:
            return failed
        identity=json.loads(request.read_text(encoding='utf-8'))
        if identity.get('prompt')!=prompt or bool(identity.get('resume'))!=bool(session_id):
            return failed
        if identity.get('data_scope')!=scope:
            return failed
        if session_id and identity.get('session_id')!=session_id:
            return failed
        if trace.resolve()!=trace or trace.stat().st_size>8*1024*1024:
            return failed
        records=[];complete=True
        for index,line in enumerate(trace.read_text(encoding='utf-8').splitlines()):
            if index>=512:
                complete=False;break
            if not line.strip():
                continue
            try:
                record=json.loads(line)
                if not isinstance(record,dict):
                    raise ValueError('invalid trace record')
                records.append(safe(record))
            except ValueError:
                complete=False;break
        return {'trace':records,'trace_recovery':{'complete':complete,'reason':'CURRENT_RUN_FILES','run_id':name}}
    except (OSError,ValueError,TypeError):
        return failed
