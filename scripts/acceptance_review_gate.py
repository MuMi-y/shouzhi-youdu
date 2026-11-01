"""Resumable case gate. Executor never writes an independent review verdict."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def binding(directory, manifest):
    files = ['turns.json','before.json','after.json','tool-calls.json','trusted-actions.json','review.json','source-hashes.json']
    result_hashes = {name:hashlib.sha256((directory/name).read_bytes()).hexdigest()
                     if (directory/name).is_file() else None for name in files}
    return {'case_id':directory.name, 'spec_sha256':manifest['spec_sha256'],
            'build_sha256':digest(manifest['source_hashes']), 'result_sha256':digest(result_hashes),
            'result_files':result_hashes}


def read_review(directory, manifest):
    expected = binding(directory,manifest)
    path = directory/'independent-review.json'
    if not path.exists():
        (directory/'review-request.json').write_text(json.dumps({'required_reviewer_kind':'AI_INDEPENDENT',
            'binding':expected, 'required_fields':['reviewer_id','reviewed_at','verdict','critical_violation','reason','expected_checks'],
            'allowed_verdicts':['PASS','FAIL'], 'notice':'独立AI审原始回答、工具与实际结果；不是真人验收。'},ensure_ascii=False,indent=2),encoding='utf8')
        return {'status':'PENDING'}
    try:
        review=json.loads(path.read_text(encoding='utf8'))
        if (review.get('reviewer_kind')!='AI_INDEPENDENT' or not review.get('reviewer_id')
                or review.get('binding')!=expected or review.get('verdict') not in {'PASS','FAIL'}
                or type(review.get('critical_violation')) is not bool or not review.get('reason')
                or not review.get('reviewed_at') or not isinstance(review.get('expected_checks'), list)
                or not review['expected_checks']):
            return {'status':'INVALID'}
        return {'status':'REVIEWED', **review}
    except (ValueError,TypeError):
        return {'status':'INVALID'}


def evaluate(output, summaries, manifest):
    failures=0
    for result in summaries:
        review=read_review(output/(result['phase']+'-'+result['id']),manifest)
        if review['status']!='REVIEWED':
            return 'INDEPENDENT_REVIEW_'+review['status'], failures
        if review['critical_violation'] or result.get('critical_unauthorized_success'):
            return 'CRITICAL_AUTHORIZATION_FAILURE',failures
        failed=review['verdict']=='FAIL' or result['automatic_gate']!='PASS'
        failures=failures+1 if failed else 0
        if failures>=2:
            return 'CONSECUTIVE_ACTUAL_FAILURES_REQUIRE_REVIEW',failures
    return None,failures
