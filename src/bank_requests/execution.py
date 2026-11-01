"""Durable chat entry for standalone evaluation, using the product request contract."""


def run_chat(runner, jobs, prompt, session_id, *, readonly=False):
    store = jobs.enable_handoff()
    gateway = getattr(runner,'request_gateway',None)
    if gateway is not None:
        if gateway.store is not None and gateway.store is not store:
            raise ValueError('REQUEST_STORE_MISMATCH')
        gateway.store = store
    job = jobs.add(prompt, session_id=session_id or '', read_only=readonly)
    if jobs.claim(job_id=job['id']) is None:
        raise ValueError('REQUEST_ALREADY_CLAIMED')
    if session_id:
        jobs.clear_setting_if_matches('chat_session', session_id)
    try:
        result = runner.run_request(prompt, session_id, request_context=jobs.request_context(job['id']),
                                    readonly=readonly)
    except Exception:
        jobs.finish(job['id'], {'ok': False, 'error': '请求执行失败；请核对原操作和回执，不自动重放。'})
        raise
    jobs.finish(job['id'], result)
    store.producers_finished(store.for_job(job['id']),trace_finalized=not result.get('process_cleanup_pending'))
    if result.get('ok') and result.get('session_id'):
        jobs.save_session(job['id'],result)
    return result
