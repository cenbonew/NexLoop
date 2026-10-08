"""Read-only product audit; actual governed CREATE/EDIT over disposable PG."""
import json
from pathlib import Path
from effect_execution_fixture import execution_plan

def test_generic_partial_edit_preserves_other_published_properties(execution_plan,admin,tmp_path):
    plan=execution_plan
    cases=[('Goal','goal','goal_editor',{'state':'closed'}),('EffectControl','control','control_editor',{'allow_effect':False})]
    observations=[]
    for name,identifier,editor,patch in cases:
        object_id=plan[identifier]
        before,revision=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(object_id,)).fetchone()
        receipt=plan[editor].edit_object(action_name=name+'.edit',action_version=1,intent_id='audit-partial-'+name,
            type_name=name,object_id=object_id,expected_revision=revision,properties=patch)
        after,current=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(object_id,)).fetchone()
        observations.append({'type':name,'before':before,'patch':patch,'after':after,'revision':current,'receipt':receipt,
            'preserved':after=={**before,**patch}})
    output=tmp_path/'partial-edit-observation.json'
    output.write_text(json.dumps(observations,indent=2)+'\n')
    assert all(item['preserved'] for item in observations), 'Generic governed partial edit discarded unchanged published properties'
