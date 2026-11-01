"""Reload authoritative attempts after the job connection has closed."""
from copy import deepcopy
from src.bank_requests.attempt_projection import disclose


def project(job, service, store):
    if job is None or store is None:
        return job
    result = deepcopy(job)
    try:
        handle = store.for_job(job['id'])
        disclosures, visited = [], set()
        pending=[handle.run_id]
        while pending and len(visited) < 64:
            run_id=pending.pop(0)
            if run_id in visited:
                continue
            visited.add(run_id)
            snapshot = store.snapshot(run_id)
            try:
                store.reconcile_bank(snapshot,service.db_path)
                from .handoff_reconcile import reconcile
                reconcile(store,snapshot,service)
                snapshot=store.snapshot(run_id)
            except Exception:
                pass  # Preserve UNKNOWN on unavailable or conflicting evidence.
            disclosures.append(disclose(snapshot,service))
            if snapshot['run']['parent_run_id']:
                pending.append(snapshot['run']['parent_run_id'])
            pending.extend(store.children(run_id))
        if job.get('result'):
            from .display_check import check
            result['result'] = check(result['result'],service)
        else:
            result['result'] = {'ok':False} if (disclosures[0]['entries'] or
                job.get('status') not in {'QUEUED','RUNNING'}) else None
        if result['result'] is not None:
            result['result']['host_disclosure'] = {'entries':[e for d in disclosures for e in d['entries']],
                'complete':not pending and all(d['complete'] for d in disclosures),
                'runs':[{k:v for k,v in d.items() if k!='entries'} for d in disclosures]}
    except Exception:
        result['result'] = {**(result.get('result') or {}),'ok':False,'error':'原记录暂时无法核对；请勿重复办理。',
                            'host_disclosure':{'complete':False,'entries':[]}}
    return result
