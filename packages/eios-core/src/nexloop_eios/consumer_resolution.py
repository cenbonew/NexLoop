"""Bounded authorized identity candidates; no name-based identity merge.

Internal projection only. known_candidate_ids is not complete search. NX-021 may
supply a separately authorized bounded candidate provider; every result still
requires current Object + display_name Property READ through this reader.
"""
import re
from dataclasses import dataclass
from nexloop_eios.object_reads import AuthorizedObjectReader
from nexloop_eios.conversation_messages import ConversationMessagePort


@dataclass(frozen=True)
class ConsumerIdentityCandidates:
    status:str
    candidates:tuple[str,...]
    verified_session_consumer:str|None
    automatic_merge:bool=False


class ConsumerNameResolver:
    def __init__(self,reader:AuthorizedObjectReader,conversations:ConversationMessagePort):
        if type(reader) is not AuthorizedObjectReader or type(conversations) is not ConversationMessagePort:
            raise ValueError('actual authorized projection ports required')
        if reader.pool is not conversations.pool or reader.session.world!=conversations.session.world or reader.session.authentication.tenant_id!=conversations.session.authentication.tenant_id:
            raise ValueError('reader and actual Human conversation must share backend tenant/world')
        self.reader,self.conversations=reader,conversations

    def resolve(self,display_name:str,known_candidate_ids:tuple[str,...]):
        if type(display_name) is not str or not 1<=len(display_name)<=256 or '\0' in display_name:
            raise ValueError('invalid exact display name')
        if type(known_candidate_ids) is not tuple or len(known_candidate_ids)>32 or len(set(known_candidate_ids))!=len(known_candidate_ids) or any(type(x) is not str or re.fullmatch('[a-f0-9]{64}',x) is None for x in known_candidate_ids):
            raise ValueError('bounded exact known candidate identities required')
        # No swallowed authorization/DB error: one inaccessible ID fails closed.
        matched=[]
        for object_id in known_candidate_ids:
            obj=self.reader.get('Consumer',object_id,fields=('display_name',))
            if obj['properties'].get('display_name')==display_name:matched.append(object_id)
        # Never caller-provided binding: authenticated Human's server ownership.
        consumers=set();cursor=''
        for unused in range(10):
            page=self.conversations.list_conversations(after=cursor,limit=100)
            consumers.update(row['consumer_id'] for row in page['items'])
            if not page['next_cursor']:break
            cursor=page['next_cursor']
        else:raise ValueError('conversation identity projection exceeds bounded scan')
        if len(consumers)>1:raise ValueError('server session has multiple consumer bindings; no identity inferred')
        verified=next(iter(consumers),None)
        return ConsumerIdentityCandidates('ambiguous' if len(matched)>1 else 'candidate' if matched else 'no_match',tuple(matched),verified)


def consumer_profile_schema():
    """Fresh-tenant initialization input only; never overwrite published v1.

    Existing minimal Consumer v1 needs NX-044 compatible evolution. Returning
    this typed declaration does not publish or change any schema.
    """
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    return ObjectTypeDefinition(type_name='Consumer',version=1,only_edit_via_actions=True,
        properties=(PropertyDefinition(property_name='display_name',value_type=PropertyValueType('string')),))
