"""ADR-031 equivalence harness (test databases only, opt-in with NEXLOOP_TEST_READ_OBJECT_COMPARE=<result file>).

After the catalog is applied, authz.nexloop_read_object is wrapped so that every call (from Python and from inside other
SQL functions) runs both the 0420 implementation and the previous body (authz.nexloop_read_object_before_derivation_once_v0420)
with the same arguments in the same transaction, each in its own subtransaction, and compares the returned jsonb or the
error (SQLSTATE and message). A difference raises 'read_object divergence'; the counts are appended to the result file
when the test database stops. Nothing here is installed outside an explicit comparison run.
"""
import os

WRAP = r'''
create sequence if not exists public.zz_read_compare_total;
create sequence if not exists public.zz_read_compare_diverged;
grant usage on sequence public.zz_read_compare_total, public.zz_read_compare_diverged to nexloop_owner;
alter function authz.nexloop_read_object(text,text,text,text) rename to zz_read_object_new;
create function authz.nexloop_read_object(p_digest text, p_world text, p_text text, p_signature text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r1 jsonb;r2 jsonb;e1 text;e2 text;s1 text;
begin
 begin r1:=authz.zz_read_object_new(p_digest,p_world,p_text,p_signature);
 exception when others then e1:=sqlstate||':'||sqlerrm;s1:=sqlstate;end;
 begin r2:=authz.nexloop_read_object_before_derivation_once_v0420(p_digest,p_world,p_text,p_signature);
 exception when others then e2:=sqlstate||':'||sqlerrm;end;
 perform nextval('public.zz_read_compare_total');
 if r1 is distinct from r2 or e1 is distinct from e2 then
  perform nextval('public.zz_read_compare_diverged');
  raise exception 'read_object divergence new=% old=%',coalesce(e1,'ok'),coalesce(e2,'ok') using errcode='XX000';end if;
 if e1 is not null then raise exception '%',substr(e1,7) using errcode=s1;end if;
 return r1;
end $$;
alter function authz.nexloop_read_object(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_object(text,text,text,text) from public;
do $$ declare g text; begin
 for g in select distinct a.grantee::regrole::text from pg_proc p, aclexplode(p.proacl) a
  where p.oid='authz.zz_read_object_new(text,text,text,text)'::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>0 and a.grantee<>p.proowner loop
  execute format('grant execute on function authz.nexloop_read_object(text,text,text,text) to %I',g);
 end loop; end $$;
'''


def enabled():
    return bool(os.environ.get('NEXLOOP_TEST_READ_OBJECT_COMPARE'))


def install(connection):
    with connection.transaction():
        connection.execute(WRAP)


def record(conninfo, test_id):
    import psycopg
    try:
        with psycopg.connect(conninfo, autocommit=True) as db:
            total = db.execute("select case when to_regclass('public.zz_read_compare_total') is null then null else "
                               "(select last_value*(is_called::int) from public.zz_read_compare_total) end").fetchone()[0]
            if total is None:
                return
            diverged = db.execute('select last_value*(is_called::int) from public.zz_read_compare_diverged').fetchone()[0]
    except Exception:
        return
    with open(os.environ['NEXLOOP_TEST_READ_OBJECT_COMPARE'], 'a') as out:
        out.write(f'{test_id}\t{total}\t{diverged}\n')
