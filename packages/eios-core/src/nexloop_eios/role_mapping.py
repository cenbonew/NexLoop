"""Fixed governed role mapping actions. Mapping metadata never grants authority."""
from datetime import UTC,datetime

def _validity(valid_from,valid_until):
    if any(type(v) is not str or not v.endswith(('Z','+00:00')) for v in (valid_from,valid_until)):
        raise ValueError('UTC-aware validity required')
    start,end=map(datetime.fromisoformat,(valid_from,valid_until))
    if start.tzinfo is None or end.tzinfo is None or start>=end:raise ValueError('ordered UTC validity required')

class RoleMappingPort:
    def __init__(self,service):self.service=service
    def create_role(self,*,intent_id,name,responsibility,ceiling_ref,valid_from,valid_until):
        _validity(valid_from,valid_until)
        return self.service.create_object(action_name='RoleDefinition.create',action_version=1,intent_id=intent_id,type_name='RoleDefinition',properties=dict(name=name,responsibility=responsibility,ceiling_ref=ceiling_ref,active=True,valid_from=valid_from,valid_until=valid_until))
    def assign(self,*,intent_id,consumer_id,role_id,scope,valid_from,valid_until):
        _validity(valid_from,valid_until)
        return self.service.create_object(action_name='ConsumerRoleLink.create',action_version=1,intent_id=intent_id,type_name='ConsumerRoleLink',properties=dict(consumer_id=consumer_id,role_id=role_id,scope=scope,active=True,valid_from=valid_from,valid_until=valid_until))
    def end(self,*,intent_id,link_id,expected_revision):
        return self.service.edit_object(action_name='ConsumerRoleLink.edit',action_version=1,intent_id=intent_id,type_name='ConsumerRoleLink',object_id=link_id,expected_revision=expected_revision,properties={'active':False})
    def edit_role(self,*,intent_id,role_id,expected_revision,name,responsibility,ceiling_ref,active,valid_from,valid_until):
        _validity(valid_from,valid_until)
        if type(active) is not bool:raise ValueError('explicit active required')
        return self.service.edit_object(action_name='RoleDefinition.edit',action_version=1,intent_id=intent_id,type_name='RoleDefinition',object_id=role_id,expected_revision=expected_revision,properties=dict(name=name,responsibility=responsibility,ceiling_ref=ceiling_ref,active=active,valid_from=valid_from,valid_until=valid_until))
    def configure_link(self,*,intent_id,link_id,expected_revision,scope,active,valid_from,valid_until):
        _validity(valid_from,valid_until)
        if type(active) is not bool:raise ValueError('explicit active required')
        return self.service.edit_object(action_name='ConsumerRoleLink.edit',action_version=1,intent_id=intent_id,type_name='ConsumerRoleLink',object_id=link_id,expected_revision=expected_revision,properties=dict(scope=scope,active=active,valid_from=valid_from,valid_until=valid_until))
    def read(self,*,link_id):
        row=self.service.read_object(type_name='ConsumerRoleLink',object_id=link_id,fields=('consumer_id','role_id','scope','active','valid_from','valid_until'))
        role=self.service.read_object(type_name='RoleDefinition',object_id=row['properties']['role_id'],fields=('name','responsibility','ceiling_ref','active','valid_from','valid_until'))
        now=datetime.now(UTC)
        active=all(item['properties']['active'] is True and datetime.fromisoformat(item['properties']['valid_from'])<=now<datetime.fromisoformat(item['properties']['valid_until']) for item in (row,role))
        return {'mapping':row,'role':role,'currently_applicable':active,'grants_authority':False}


def role_mapping_schemas():
    """Explicit initial configurator declarations, never runtime Schema publication.

    UTC-aware interval semantics are checked by the typed port and owned SQL.
    ceiling_ref is responsibility metadata, not an EIOS permission grant.
    """
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    output=[]
    for kind,names in [('RoleDefinition',('name','responsibility','ceiling_ref','valid_from','valid_until')),('ConsumerRoleLink',('consumer_id','role_id','scope','valid_from','valid_until'))]:
        output.append(ObjectTypeDefinition(type_name=kind,version=1,only_edit_via_actions=True,
          properties=tuple(PropertyDefinition(property_name=n,value_type=PropertyValueType.STRING,required=True) for n in names)+(PropertyDefinition(property_name='active',value_type=PropertyValueType.BOOLEAN,required=True),)))
    return tuple(output)
