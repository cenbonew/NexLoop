"""ADR-042 (0390): the configurator's group-only object type increment (v N over N-1: only new property groups of existing,
ungrouped properties) as its own step; every other increment is refused, and the 0381 manifest still refuses any v >= 2 it does not
find present and equal. Synthetic types."""
import copy
import uuid

import pytest
from nexloop_eios.trusted_configuration import ConfigurationRejected,apply_group_increments
from test_trusted_configuration_pg import apply,configured  # noqa: F401


def with_type(name='S77Probe'):
    """A real ObjectTypeDefinition dump (the shape 0050 stores), from the production model."""
    from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyValueType
    return ObjectTypeDefinition(type_name=name,version=1,only_edit_via_actions=True,properties=tuple(PropertyDefinition(property_name=p,
        value_type=PropertyValueType(k)) for p,k in (('active','boolean'),('title','string'),('amount','string')))).model_dump(mode='json')


def bump(base,version=2,groups=({'group_name':'probe_lifecycle','display_name':'状态','property_names':['active']},)):
    item=copy.deepcopy(base);item['version']=version;item['property_groups']=list(base['property_groups'])+copy.deepcopy(list(groups));return item


def body(f,revision,types):
    return {**f['manifest'],'manifest_id':str(uuid.uuid4()),'expected_revision':revision,'object_types':types,'authority_facts':[],
            'service_credentials':f['manifest']['service_credentials']}


def stored(admin,f,name='S77Probe'):
    return {v:d for v,d in admin.execute('select version,definition from ontology.object_type_versions where tenant_id=%s and type_name=%s order by version',(f['tenant'],name))}


def publish(f,types,unused=None):
    """One manifest at the tenant's current authority revision (read here; a manifest changes it)."""
    import psycopg
    with psycopg.connect(f['pg']) as db:
        revision=db.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()[0]
    return apply(f,body(f,revision,types))['authority_revision']


def revision(f):
    import psycopg
    with psycopg.connect(f['pg']) as db:
        return db.execute('select authority_revision from control.nexloop_tenants where tenant_id=%s',(f['tenant'],)).fetchone()[0]


def increment(f,items,**kw):
    return apply_group_increments(f['tenant'],items,database_url_file=f['paths']['dsn'],signing_key_file=f['paths']['signing'],
                                  signing_key_id='explicit-configuration',expected_revision=kw.pop('expected_revision',None) or revision(f),**kw)


def test_a_group_only_increment_is_accepted_and_replays(configured,admin):
    f=configured;base=with_type();publish(f,[base])
    two=bump(base)
    with pytest.raises(ConfigurationRejected):publish(f,[base,two])  # the 0381 manifest alone still refuses a new v2
    assert set(stored(admin,f))=={1}
    assert increment(f,[two])['inserted']==1
    publish(f,[base,two])  # the manifest now finds v2 present and equal
    assert stored(admin,f)=={1:base,2:two}
    assert increment(f,[two])['inserted']==0  # an equal version: nothing changes
    mid=str(uuid.uuid4());rev=revision(f);first=increment(f,[two],manifest_id=mid,expected_revision=rev)
    assert increment(f,[two],manifest_id=mid,expected_revision=rev)==first  # replay of the same manifest id and digest: the recorded result
    with pytest.raises(ConfigurationRejected):increment(f,[bump(base,groups=({'group_name':'probe_other','display_name':'','property_names':['amount']},))],manifest_id=mid,expected_revision=rev)
    other=bump(base,groups=({'group_name':'probe_lifecycle','display_name':'状态','property_names':['title']},))
    with pytest.raises(ConfigurationRejected):increment(f,[other])  # same version, other content: conflict
    assert stored(admin,f)[2]==two
    # v3 over v2 keeps v2's group exactly and groups another ungrouped property.
    three=bump(two,3,({'group_name':'probe_text','display_name':'文本','property_names':['title']},))
    assert increment(f,[three])['inserted']==1 and set(stored(admin,f))=={1,2,3}
    with pytest.raises(ConfigurationRejected):increment(f,[bump(with_type('S77Probe2'))])  # stale expected revision is not the cause here: no v1
    published=admin.execute("select count(*) from control.nexloop_configuration_publications where tenant_id=%s and public_manifest->>'kind'='group_increment'",(f['tenant'],)).fetchone()
    assert published==(4,)  # two inserts, the equal no-op, and the replayed manifest id once


@pytest.mark.parametrize('change',['added_property','removed_property','changed_type','required','display','unknown_member','grouped_twice',
                                   'no_new_group','duplicate_member','skipped_version','no_previous','reused_group_name','version_one'])
def test_every_other_increment_is_refused_and_leaves_nothing(configured,admin,change):
    f=configured;base=with_type();publish(f,[base])
    item=bump(base)
    if change=='added_property':item['properties'].append({**item['properties'][0],'property_name':'extra'})
    elif change=='removed_property':item['properties']=item['properties'][:-1]
    elif change=='changed_type':item['properties'][2]['value_type']={'kind':'integer'}
    elif change=='required':item['properties'][1]['required']=not item['properties'][1].get('required',False)
    elif change=='display':item['display_name']='改名'
    elif change=='unknown_member':item['property_groups'][0]['property_names']=['missing']
    elif change=='grouped_twice':item['property_groups'].append({'group_name':'probe_again','display_name':'','property_names':['active']})
    elif change=='no_new_group':item['property_groups']=[]
    elif change=='duplicate_member':item['property_groups'][0]['property_names']=['active','active']
    elif change=='skipped_version':item['version']=3
    elif change=='no_previous':item=bump(with_type('S77Fresh'))
    elif change=='version_one':item=base
    elif change=='reused_group_name':
        two=bump(base);increment(f,[two])
        item=bump(two,3,({'group_name':'probe_lifecycle','display_name':'','property_names':['title']},))
    before=stored(admin,f,item['type_name']);rev=revision(f)
    with pytest.raises(ConfigurationRejected):increment(f,[item])
    assert stored(admin,f,item['type_name'])==before and revision(f)==rev


def test_a_changed_old_group_and_a_stale_revision_are_refused(configured,admin):
    f=configured;base=with_type();publish(f,[base])
    two=bump(base);increment(f,[two])
    three=bump(two,3,({'group_name':'probe_text','display_name':'','property_names':['title']},))
    three['property_groups'][0]['property_names']=['active','amount']
    with pytest.raises(ConfigurationRejected):increment(f,[three])
    good=bump(two,3,({'group_name':'probe_text','display_name':'','property_names':['title']},))
    with pytest.raises(ConfigurationRejected):
        increment(f,[good],expected_revision=revision(f)-1)
    assert set(stored(admin,f))=={1,2}


def test_only_the_configurator_calls_it(configured,admin):
    import psycopg
    for role in ('nexloop_api','nexloop_domain_worker','nexloop_runtime'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege),admin.transaction():
            admin.execute(f'set local role {role}')
            admin.execute("select control.nexloop_configure_group_increment('x','{}','x','x',decode(repeat('00',32),'hex'))")


def raw_increment(f,items):
    """The 0390 entry called directly as the configurator, past the Python model (which would already drop or refuse these
    shapes): the database is the only check a later manifest relies on (L6 K1)."""
    import hashlib
    import psycopg
    from nexloop_eios.postgres_artifacts import canonical_payload
    from nexloop_eios.private_configuration import read_private_text
    body={'schema_version':'1.0','kind':'group_increment','manifest_id':str(uuid.uuid4()),'tenant_id':f['tenant'],'expected_revision':revision(f),'object_types':items}
    text=canonical_payload(body)
    with psycopg.connect(read_private_text(f['paths']['dsn'],maximum=16384)) as db:
        return db.execute('select control.nexloop_configure_group_increment(%s,%s,%s,%s,%s)',(f['tenant'],text,hashlib.sha256(text.encode()).hexdigest(),
            'explicit-configuration',bytes.fromhex(read_private_text(f['paths']['signing'],maximum=128)))).fetchone()[0]


@pytest.mark.parametrize('group',[
    {'group_name':'probe_lifecycle','display_name':'状态','property_names':['active'],'extra':'1'},          # an extra key
    {'group_name':'Probe_Lifecycle','display_name':'状态','property_names':['active']},                    # an upper-case name
    {'group_name':None,'display_name':'状态','property_names':['active']},                                 # a null name
    {'group_name':'1probe','display_name':'状态','property_names':['active']},                             # not the 0084 group name
    {'group_name':'probe_lifecycle','display_name':None,'property_names':['active']},                     # a non-string display name
    {'group_name':'probe_lifecycle','property_names':['active']},                                         # a missing key
    'probe_lifecycle',                                                                                     # not an object
])
def test_the_database_alone_refuses_a_malformed_new_group(configured,admin,group):
    import psycopg
    f=configured;base=with_type();publish(f,[base])
    good=bump(base);assert raw_increment(f,[good])['inserted']==1  # the raw path itself works
    item=bump(good,3,(group,));rev=revision(f)
    with pytest.raises(psycopg.Error):raw_increment(f,[item])
    assert set(stored(admin,f))=={1,2} and revision(f)==rev
