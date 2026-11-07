-- NX-054 P5 rollback (docs/design/relation-read-p5.md §4): the body of a NEW migration, never an edit of relation_read_only.
-- It puts the P4a union (relations united with the property rule) back under the reader's name. The readers are not touched
-- (they call runtime.nexloop_object_consumers_read by name). The relation-only function is dropped.
drop function runtime.nexloop_object_consumers_read(text,text,text,text);
alter function runtime.nexloop_object_consumers_read_p4union(text,text,text,text) rename to nexloop_object_consumers_read;
revoke all on function runtime.nexloop_object_consumers_read(text,text,text,text) from public;
