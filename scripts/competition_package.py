"""Copy an explicit source allowlist. Never includes runtime/auth/venv/upstream."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=('src/bank_v1','src/bank_v1_web','src/bank_tasks','src/bank_insights','src/bank_goals','src/bank_evidence','src/bank_requests',
    'src/competition_web','src/moneydata_history','plugins/bank-v1',
    'tests/competition_tasks','tests/competition_insights','tests/competition_policy','tests/competition_security','tests/competition_ai',
    'data/synthetic/competition_v1','third_party/licenses','third_party/r2')
FILES=('docs/FINAL_CANDIDATE_20261029.md','scripts/competition_acceptance_exact.py','scripts/competition_acceptance_gaps.py','scripts/competition_acceptance_continuity.py','scripts/ensure_docker.ps1','scripts/sandbox_layout.cjs','scripts/check_sandbox_runtime.cjs','tests/test_plan_window.py','tests/test_final_reserve.py','docs/有数_20261027体验版使用说明.md','node-runtime/package.json','node-runtime/package-lock.json','requirements-competition.lock.txt','requirements-web.lock.txt','docs/有数_第一次完整体验指南.md','docs/COMPETITION_RUNBOOK.md','docs/COMPETITION_AI_STATUS.md',
    'management/AI工具调用与计算验收标准_2026-10-12.md',
    'config/settings.example.yaml','config/model_route.json','scripts/model_route.cjs',
    'scripts/competition_web.py','scripts/competition_security_enroll.py',
    'scripts/competition_security_story.py','scripts/competition_insights_evaluate.py',
    'scripts/competition_insights_verify.py','scripts/competition_acceptance_http.py',
    'scripts/competition_acceptance_live.py','scripts/acceptance_review_gate.py','scripts/g1_goal_demo.py',
    'scripts/g2b_control.py','scripts/g2b_story.py','scripts/g2b_sync.py','tests/g2_goals/test_service.py','tests/g2_goals/test_current_plan.py','tests/test_goal_impact.py','tests/g2_goals/test_preview.py','tests/g2_goals/test_service_health.py',
    'management/evidence/COMPETITION_V1/acceptance/cases.json',
    'tests/competition_eval/test_failed_turn.py',
    'tests/v1_dev/test_forecast_scope.py',
    'tests/v1_dev/test_forecast_date_semantics.py',
    'tests/v1_dev/test_forecast_scenario_minima.py',
    'scripts/competition_package.py','scripts/start_competition.ps1','scripts/start_product.ps1',
    'scripts/check_dsh_runtime.cjs','scripts/product_budget.py','scripts/batch_budget.cjs','scripts/acceptance_budget.py',
    '启动有数.cmd','停止有数.cmd',
    'scripts/dsh_v1.cjs','scripts/dsh_sandbox.cjs','scripts/dsh_container.cjs','scripts/broker_lifecycle.cjs','scripts/sandbox_provider.cjs',
    'management/contracts/COMPETITION_TASKS.md','management/contracts/COMPETITION_INSIGHTS.md',
    'management/contracts/COMPETITION_SECURITY.md',
    'data/external/moneydata/laramee26openBankTransactionData.xlsx',
    'data/external/moneydata/date_comparison.json','data/external/moneydata/SOURCE.md',
    'data/working/moneydata_v1/transactions.csv','data/working/moneydata_v1/validation.json',
    'data/working/moneydata_v1/README.md')
SECRET=re.compile(rb'(?:ark-[0-9a-f]{8}-[0-9a-f-]{27,}|sk-[A-Za-z0-9_-]{28,})')
LOCAL_ARCHIVE_TESTS = {
    'tests/competition_ai/test_goal_contract_release.py': 'Requires retained local goal-journey audit evidence.',
    'tests/competition_ai/test_usable_goal_release.py': 'Requires retained local goal-journey audit evidence.',
    'tests/competition_ai/test_goal_profile_reference.py': 'Requires retained local R3 model session evidence.',
    'tests/competition_ai/test_billing_observable_period.py': 'Requires retained local R4 evidence and DSH runtime requests.',
    'tests/competition_ai/test_request_contract_r7.py': 'Requires retained local R6 model evidence and runtime requests.',
}


def selected_files(root):
    found={root/name for name in FILES}
    for directory in DIRECTORIES:
        for path in (root/directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.sqlite3','.db'):
                if directory=='third_party/r2' and path.name=='manifest.json':continue
                found.add(path)
    for path in sorted(found):
        if path.relative_to(root).as_posix() in LOCAL_ARCHIVE_TESTS:
            continue
        if not path.is_file():raise FileNotFoundError('Required source missing: '+str(path.relative_to(root)))
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Source symlink/path escaped allowlist')
        data=path.read_bytes()
        if path.suffix!='.xlsx' and SECRET.search(data):raise ValueError('Possible credential detected; refusing package: '+str(path.relative_to(root)))
        yield path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',type=Path,default=ROOT/'deliverables/competition_v1/source')
    args=parser.parse_args()
    target=args.destination.resolve()
    allowed=(ROOT/'deliverables/competition_v1').resolve()
    if target==allowed or not target.is_relative_to(allowed):raise ValueError('Destination must be below project deliverables/competition_v1')
    sources=list(selected_files(ROOT))
    target.mkdir(parents=True,exist_ok=True)
    previous=target/'release_manifest.json'
    old=json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else None
    if old and old.get('packager')!='competition_package_v1':raise ValueError('Unknown previous manifest')
    paths={str(p.relative_to(ROOT)).replace('\\','/') for p in sources}
    allowed_existing=set(old['files']) if old else set()
    allowed_existing.update({'release_manifest.json'})
    unexpected=[p.relative_to(target).as_posix() for p in target.rglob('*')
        if p.is_file() and p.relative_to(target).as_posix() not in allowed_existing]
    if unexpected:
        raise ValueError('Package target has untracked files; use a new clean destination, never include them silently: '+str(unexpected[:5]))
    if old:
        for name in set(old['files'])-paths:
            stale=(target/name).resolve()
            if not stale.is_relative_to(target):raise ValueError('Manifest path escaped package')
            if stale.is_file():stale.unlink()
    manifest={'packager':'competition_package_v1','status':'CANDIDATE_NOT_AI_ACCEPTED',
        'created_at':datetime.now(timezone.utc).isoformat(),'python':'3.13.7','dsh':'0.1.1-rc.2',
        'model_validation':'CANDIDATE_NOT_AI_ACCEPTED. Packaging does not establish AI acceptance. Read docs/COMPETITION_AI_STATUS.md from this same package for the dated source-bound results, remaining work and evidence references. release_manifest.json binds file bytes, not business correctness. Keep initial results, retests, execution corrections and read-only repairs separate; preserve original failures. Offline checks do not establish real-model acceptance.',
        'excluded':['runtime','identity databases','credentials','DSH personal config','venv','upstream source','old evidence databases','large unused datasets'],
        'excluded_tests':LOCAL_ARCHIVE_TESTS,
        'files':{}}
    for source in sources:
        relative=source.relative_to(ROOT)
        destination=target/relative
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,destination)
        manifest['files'][relative.as_posix()]={'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'bytes':destination.stat().st_size}
    readme='# 收支有度参赛候选源代码包\n\n队伍：从容计划。\n\n状态：CANDIDATE_NOT_AI_ACCEPTED。打包成功不等于 AI 或参赛作品验收通过。本说明不固定项目版本、测试数量或历史额度恢复时间；实际状态见同包 [AI 验收状态](docs/COMPETITION_AI_STATUS.md)，并核对其日期、冻结源码和引用证据。\n\n先读 [部署与恢复手册](docs/COMPETITION_RUNBOOK.md)。release_manifest.json 记录本包文件的 SHA256 和大小，只证明文件对应关系，不证明业务正确。若状态文档引用的冻结源码与本包不一致，或最终证据未完成，应继续按未验收候选处理。项目审计档案保留原始失败与历史版本；状态文档中的引用不代表所有原始日志随包分发。\n\n代码测试、无模型业务检查、真实 AI 回答和网页办理结果分开验收。初测与复测不互相替换，不同版本不拼接通过率；首轮回答、一次执行纠正和只读补证分别记录。回答失败后已有的任务、待确认卡和银行回执仍可能有效，须先核对状态，不能重复已成功办理的操作。\n\n完整模型环境需要按手册配置 DSH、Docker 与已授权模型配置。银行和任务离线模式使用 --no-model --without-history；它不能证明聊天能力。安装环境、镜像版本、真实模型调用、备份恢复和项目外运行是否已验证，以同包 AI 状态及对应证据为准，不沿用旧包成绩冒充本包实测。\n\n本包附正式评测器、虚构题集及部分控制流/业务测试。`python scripts/competition_acceptance_live.py --phase both` 默认只输出 32 初测＋12 复测计划，不调用模型；`--self-test` 执行本地虚构银行和 HTTP 自检。离线运行 `python -m unittest discover -s tests/competition_eval -v` 和 `python -m unittest discover -s tests/competition_ai -v`，按实际输出记录数量。包内测试子集不自动等于项目全部回归。真实执行须显式 --execute，会消耗模型额度，并使用独立虚构库和 TEST_FIXTURE；不得擅自复用旧运行库或失败会话。\n\n包不含真实身份库、运行数据库、模型密钥、个人 DSH 配置或虚拟环境。MoneyData 来源见 data/external/moneydata/SOURCE.md，币种未知，不并入模拟资金。第三方许可保留，DSH 独立安装；本包不新增对外发布授权。打包脚本本身不调用或调度模型。\n\n下一步我该怎么验证：由开发与独立审查者核对 manifest 和状态文档的源码绑定，在独立目录完成本包离线检查、真实业务与模型验收、停止后备份恢复，再精确更新最终状态。保留失败原件；用户无需承担技术测试。\n'
    readme += '\n## 测试分发边界\n\n本包仅分发可独立运行的测试子集。以下列出的本地历史证据回归保留在开发审计档案，不随源码分发，因为依赖原始会话或本机运行目录；不会为使测试可运行而打包身份库、运行凭据或整份DSH会话。它们的原测试与失败记录未删除，验收门槛未改变，包内discover结果不能替代它们或正式全量验收。\n\n' + ''.join('- ' + name + '\n' for name in LOCAL_ARCHIVE_TESTS)
    (target/'README.md').write_text(readme,encoding='utf-8')
    manifest['files']['README.md']={'sha256':hashlib.sha256((target/'README.md').read_bytes()).hexdigest(),'bytes':(target/'README.md').stat().st_size}
    previous.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'destination':str(target),'status':'CANDIDATE_NOT_AI_ACCEPTED','files':len(manifest['files']),
        'bytes':sum(v['bytes'] for v in manifest['files'].values())},ensure_ascii=False))


if __name__=='__main__':main()
