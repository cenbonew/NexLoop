"""NX-045 explicit opt-in: calibrate glue weights with the real embedding profile.

Not collected by default CI. NEXLOOP_REAL_EMBEDDING_ENV_FILE names the private
env file (EMBEDDING_*); the key stays in process memory. Synthetic pairs only.
The committed CI configuration uses the deterministic test embedding; a
production tenant configuration must be calibrated on its own embedding profile
(this run) and published through trusted configuration.
"""
import json
import os
from pathlib import Path

from nexloop_eios.candidate_merge import FEATURE_VERSION,calibrate
from nexloop_eios.embedding_profile import load_embedding_profile
from nexloop_eios.embedding_provider import ArkMultimodalEmbeddingProvider

ROOT=Path(__file__).resolve().parents[1]


def test_real_embedding_calibration(tmp_path):
    supplied=os.environ.get('NEXLOOP_REAL_EMBEDDING_ENV_FILE')
    assert supplied,'explicit private embedding configuration file required; no implicit .env read'
    profile=load_embedding_profile(env_file=supplied)
    provider=ArkMultimodalEmbeddingProvider(profile)
    dataset=json.loads((ROOT/'tests/data/nx045_merge_calibration.json').read_text())
    result=calibrate(dataset['pairs'],provider)
    assert result['precision']==1.0 and result['false_positive']==0
    report={'evidence_class':'real','embedding_profile':provider.profile_id,'feature_version':FEATURE_VERSION,'dataset':dataset['dataset'],
        'dataset_version':dataset['version'],'inputs':'synthetic only','result':result,'transport_failures':provider.transport_failures}
    text=json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)
    assert profile.credential_for_provider() not in text
    Path(os.environ.get('NEXLOOP_NX045_REAL_REPORT') or tmp_path/'nx045-real-calibration.json').write_text(text+'\n')
    print(json.dumps(result,ensure_ascii=False))
