"""ADR-031 equivalence harness (test databases only, opt-in with NEXLOOP_TEST_READ_OBJECT_COMPARE=<result file>).

After the catalog is applied, each function of PAIRS (ADR-031 read_object; ADR-032 batch fact loader and read check) is
wrapped so that every call (from Python and from inside other SQL functions) runs both the current implementation and the
previous body kept by its migration with the same arguments in the same transaction, each in its own subtransaction, and
compares the returned jsonb or the error (SQLSTATE and message). A difference raises 'read_object divergence'; the counts are appended to the result file
when the test database stops. Nothing here is installed outside an explicit comparison run.
"""
import os

# (function, previous body kept by its migration, argument list): each is wrapped the same way when both exist.
PAIRS = (('authz.nexloop_read_object', 'authz.nexloop_read_object_before_derivation_once_v0420', 'p_digest text, p_world text, p_text text, p_signature text'),
         ('authz.nexloop_load_authority_facts', 'authz.nexloop_load_authority_facts_before_identity_once_v0421', 'p_digest text, p_world text, p_keys jsonb'),
         ('authz.nexloop_assert_read_authority_inner_before_erasure_v0142', 'authz.nexloop_assert_read_authority_inner_before_identity_once_v0421',
          'p_digest text, p_world text, p_claims jsonb'))

SEQUENCES = '''
alter database postgres set track_functions='all';
create sequence if not exists public.zz_read_compare_total;
create sequence if not exists public.zz_read_compare_diverged;
grant usage on sequence public.zz_read_compare_total, public.zz_read_compare_diverged to nexloop_owner;
'''


def wrap(function, previous, arguments):
    """SQL replacing `function` by a comparing wrapper; the current body is renamed to <name>_zz_new (grants copied)."""
    schema, name = function.split('.')
    types = ','.join(a.split()[-1] for a in arguments.split(','))
    names = ','.join(a.split()[0] for a in arguments.split(','))
    new = f'{schema}.{name[:40]}_zz_new'
    return f'''
alter function {function}({types}) rename to {name[:40]}_zz_new;
create function {function}({arguments}) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r1 jsonb;r2 jsonb;e1 text;e2 text;s1 text;
begin
 begin r1:={new}({names});
 exception when others then e1:=sqlstate||':'||sqlerrm;s1:=sqlstate;end;
 begin r2:={previous}({names});
 exception when others then e2:=sqlstate||':'||sqlerrm;end;
 if r1 is distinct from r2 or e1 is distinct from e2 then
  if current_setting('transaction_read_only')='off' then perform nextval('public.zz_read_compare_diverged');end if;
  raise exception '{name} divergence new=% old=%',coalesce(e1,'ok'),coalesce(e2,'ok') using errcode='XX000';end if;
 if e1 is not null then raise exception '%',substr(e1,7) using errcode=s1;end if;
 return r1;
end $$;
alter function {function}({types}) owner to nexloop_owner;
revoke all on function {function}({types}) from public;
do $do$ declare g text; begin
 for g in select distinct a.grantee::regrole::text from pg_proc p, aclexplode(p.proacl) a
  where p.oid='{new}({types})'::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>0 and a.grantee<>p.proowner loop
  execute format('grant execute on function {function}({types}) to %I',g);
 end loop; end $do$;
'''


def enabled():
    return bool(os.environ.get('NEXLOOP_TEST_READ_OBJECT_COMPARE'))


def install(connection):
    with connection.transaction():
        connection.execute(SEQUENCES)
        for function, previous, arguments in PAIRS:
            types = ','.join(a.split()[-1] for a in arguments.split(','))
            if connection.execute('select to_regprocedure(%s)', (f'{previous}({types})',)).fetchone()[0]:
                connection.execute(wrap(function, previous, arguments))


def record(conninfo, test_id):
    """Comparisons = calls of the wrappers (track_functions, flushed when the backends ended); a divergence also failed the test."""
    import psycopg
    try:
        with psycopg.connect(conninfo, autocommit=True) as db:
            if db.execute("select to_regclass('public.zz_read_compare_total')").fetchone()[0] is None:
                return
            names = [f.split('.')[1] for f, _, _ in PAIRS]
            total = db.execute('select coalesce(sum(calls),0) from pg_stat_user_functions where schemaname=%s and funcname=any(%s)', ('authz', names)).fetchone()[0]
            diverged = db.execute('select last_value*(is_called::int) from public.zz_read_compare_diverged').fetchone()[0]
    except Exception:
        return
    with open(os.environ['NEXLOOP_TEST_READ_OBJECT_COMPARE'], 'a') as out:
        out.write(f'{test_id}\t{total}\t{diverged}\n')
