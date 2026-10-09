"""NX-019 explicit opt-in real-model run over the frozen synthetic dataset.

Not collected by default CI (file name is not test_*). Requires
NEXLOOP_NX019_REAL_ENV_FILE naming a private (0600) env file with MODEL_*;
the key is read in-process only, never printed, logged or written. Every input
is synthetic. Guard invariants are asserted; model recall is only measured.
Report goes to NEXLOOP_NX019_REAL_REPORT or the pytest tmp_path.
"""
import json
import os
from pathlib import Path

from nexloop_eios.conversation_extraction import (ExtractionRejected,OpenAICompatibleExtractionProvider,PROMPT_VERSION,EXTRACTOR_VERSION,
    SYSTEM_PROMPT,build_user_payload,normalize)
from nexloop_eios.model_profile import load_model_profile
from test_conversation_extraction import CASES,DATASET,assert_invariants,materialize,matches


class _Result:
    def __init__(self,claims):self.claims=claims


def test_real_deepseek_over_frozen_synthetic_dataset(tmp_path):
    supplied=os.environ.get('NEXLOOP_NX019_REAL_ENV_FILE')
    assert supplied,'explicit private env file required; no implicit .env read'
    profile=load_model_profile(environment={},env_file=supplied)
    assert profile.provider=='deepseek','real model key unavailable'
    provider=OpenAICompatibleExtractionProvider(profile,timeout=90)
    key=profile.credential_for_provider()
    report={'evidence_class':'real','provider':profile.provider,'model_id':profile.model_id,'extractor_version':EXTRACTOR_VERSION,
        'prompt_version':PROMPT_VERSION,'dataset':DATASET['dataset'],'dataset_version':DATASET['version'],'inputs':'synthetic only','cases':[]}
    for case_id in sorted(CASES):
        case=CASES[case_id];messages,context,_=materialize(case)
        entry={'id':case_id,'categories':case['categories']}
        try:
            raw=provider.complete(SYSTEM_PROMPT,build_user_payload(messages,context))
            topics,claims,rejected=normalize(raw,messages,context)
        except ExtractionRejected as error:
            entry.update(outcome='output_rejected',reason=str(error));report['cases'].append(entry);continue
        except Exception as error:
            entry.update(outcome='provider_unavailable',reason=type(error).__name__);report['cases'].append(entry);continue
        assert_invariants(_Result(claims),messages)
        expected=case['expected']
        violations=[pattern for pattern in expected.get('forbidden',[]) if any(matches(c,pattern) for c in claims)]
        wanted=[(w['source_sequence'],w['epistemic_kind']) for w in expected.get('claims',[])]
        got={(c['source_sequence'],c['epistemic_kind']) for c in claims}
        entry.update(outcome='normalized',topics=[[t['first_sequence'],t['last_sequence']] for t in topics],
            expected_topics=expected.get('topics'),claims=[{k:c[k] for k in ('source_sequence','quote','epistemic_kind','predicate','polarity','modality','condition','guard_flags','confidence')}
                |{'valid_time_status':c['valid_time']['status']} for c in claims],
            rejected=[r['reason'] for r in rejected],expected_kind_recall=[w in got for w in wanted],forbidden_violations=violations)
        report['cases'].append(entry)
    outcomes=[c['outcome'] for c in report['cases']]
    report['summary']={'cases':len(outcomes),'normalized':outcomes.count('normalized'),'output_rejected':outcomes.count('output_rejected'),
        'provider_unavailable':outcomes.count('provider_unavailable'),
        'claims':sum(len(c.get('claims',[])) for c in report['cases']),
        'verified_fact_persisted':0,'guard_invariant_failures':0,
        'forbidden_pattern_violations':sum(len(c.get('forbidden_violations',[])) for c in report['cases']),
        'expected_kind_recall':[sum(sum(c.get('expected_kind_recall',[])) for c in report['cases']),sum(len(c.get('expected_kind_recall',[])) for c in report['cases'])]}
    text=json.dumps(report,ensure_ascii=False,indent=1)+'\n'
    assert key not in text
    target=Path(os.environ.get('NEXLOOP_NX019_REAL_REPORT') or tmp_path/'nx019-real-extraction.json')
    target.write_text(text)
    assert report['summary']['provider_unavailable']==0
    assert report['summary']['forbidden_pattern_violations']==0,[c['id'] for c in report['cases'] if c.get('forbidden_violations')]
