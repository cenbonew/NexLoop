"""Generate TypeScript structural types and exact OpenAPI 3.1 schema components."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def type_name(name):return ''.join(part.title() for part in name.split('-'))


def ts(schema):
    if 'const' in schema:return json.dumps(schema['const'],ensure_ascii=False)
    if 'enum' in schema:return ' | '.join(json.dumps(x,ensure_ascii=False) for x in schema['enum'])
    if 'anyOf' in schema:return ' | '.join('('+ts(s)+')' for s in schema['anyOf'])
    kind=schema.get('type')
    if isinstance(kind,list):return ' | '.join(ts(schema|{'type':k}) for k in kind)
    if kind=='string':return 'string'
    if kind in ('number','integer'):return 'number'
    if kind=='boolean':return 'boolean'
    if kind=='null':return 'null'
    if kind=='array':return 'Array<'+ts(schema.get('items',{}))+'>'
    if kind=='object':
        props=schema.get('properties',{});required=set(schema.get('required',[]))
        fields=[json.dumps(k)+('' if k in required else '?')+': '+ts(v)+';' for k,v in props.items()]
        additional=schema.get('additionalProperties',True)
        if additional is not False:fields.append('[key: string]: unknown;')
        return '{ '+ ' '.join(fields)+' }'
    if not schema:return 'unknown'
    raise ValueError('unsupported structural type; do not silently weaken a contract')


def python_models(components):
    """Typed wire models; exact canonical rules run before Pydantic coercion.

    These models do not resolve refs, authenticate reviewers or grant permission.
    Embedded schemas keep installed wheels independent of repository paths.
    """
    lines=['# Generated from canonical schemas; do not edit.',
        'from __future__ import annotations','import json',
        'from typing import Any, ClassVar, Literal',
        'from pydantic import BaseModel, ConfigDict, model_validator',
        'from jsonschema import Draft202012Validator, FormatChecker',
        '', 'class _Contract(BaseModel):',
        '    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")',
        '    _canonical_schema: ClassVar[dict[str, Any]]',
        '    @classmethod',
        '    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:',
        '        return json.loads(json.dumps(cls._canonical_schema))',
        '    @model_validator(mode="wrap")',
        '    @classmethod',
        '    def _validate_wire(cls, value: Any, handler: Any) -> Any:',
        '        try:',
        '            if isinstance(value, cls): value = value.model_dump(mode="json", exclude_unset=True, warnings=False)',
        '            def check_json(item: Any) -> None:',
        '                if type(item) in (str, int, float, bool, type(None)): return',
        '                if type(item) is list:',
        '                    for child in item: check_json(child)',
        '                    return',
        '                if type(item) is dict and all(type(key) is str for key in item):',
        '                    for child in item.values(): check_json(child)',
        '                    return',
        '                raise ValueError()',
        '            check_json(value)',
        '            json.dumps(value, allow_nan=False)',
        '        except Exception: raise ValueError("canonical contract validation failed") from None',
        '        errors = list(Draft202012Validator(cls._canonical_schema, format_checker=FormatChecker()).iter_errors(value))',
        '        if errors: raise ValueError("canonical contract validation failed")',
        '        return handler(value)', '']
    emitted=[]
    def annotation(schema,name):
        if 'const' in schema:return 'Literal['+repr(schema['const'])+']'
        if 'enum' in schema:return 'Literal['+', '.join(repr(v) for v in schema['enum'])+']'
        if 'anyOf' in schema:return ' | '.join(annotation(v,name+str(i)) for i,v in enumerate(schema['anyOf']))
        kind=schema.get('type')
        if isinstance(kind,list):return ' | '.join(annotation(schema|{'type':v},name+str(i)) for i,v in enumerate(kind))
        if kind=='array':return 'list['+annotation(schema.get('items',{}),name+'Item')+']'
        if kind=='object':
            if not schema.get('properties'):return 'dict[str, Any]'
            fields=[];required=set(schema.get('required',[]))
            for key,value in schema['properties'].items():
                typ=annotation(value,name+type_name(key.replace('_','-')))
                fields.append('    '+key+': '+typ+('' if key in required else ' | None = None'))
            emitted.append('class '+name+'(BaseModel):\n    model_config = ConfigDict(extra="'+('forbid' if schema.get('additionalProperties') is False else 'allow')+'", strict=True)\n'+'\n'.join(fields)+'\n')
            return name
        if kind in {'string','number','integer','boolean','null'}:return {'string':'str','number':'float | int','integer':'int','boolean':'bool','null':'None'}[kind]
        if not schema:return 'Any'
        raise ValueError('unsupported Pydantic structural type')
    top=[]
    for name,schema in components.items():
        fields=[];required=set(schema.get('required',[]))
        for key,value in schema['properties'].items():
            typ=annotation(value,name+type_name(key.replace('_','-')))
            fields.append('    '+key+': '+typ+('' if key in required else ' | None = None'))
        top.append('class '+name+'(_Contract):\n    _canonical_schema = json.loads('+repr(json.dumps(schema,ensure_ascii=False,separators=(',',':')))+')\n'+'\n'.join(fields)+'\n')
    return '\n'.join(lines+emitted+top)


def render():
    files=sorted((ROOT/'packages/contracts').glob('*.schema.json'))
    if not files:raise ValueError('canonical contracts missing')
    components={};hashes={};schema_ids=set();lines=['// Generated from canonical schemas; do not edit.', '// Structural types only: enforce format, bounds, patterns and conditional world rules with JSON Schema at runtime.']
    for path in files:
        schema=json.loads(path.read_text());name=type_name(path.name.removesuffix('.schema.json'))
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        schema_id=schema.get('$id')
        if not isinstance(schema_id,str) or not schema_id or schema_id in schema_ids:
            raise ValueError('canonical contract ID missing or duplicated')
        schema_ids.add(schema_id)
        if name in components or schema.get('title')!=path.name.removesuffix('.schema.json'):
            raise ValueError('canonical contract name conflict')
        components[name]=schema
        hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append('export type '+name+' = '+ts(schema)+';')
    # This component document is deliberately not an implemented route catalog.
    document={'openapi':'3.1.0','info':{'title':'NexLoop target contract components','version':'1.0'},'paths':{},'components':{'schemas':components},'x-canonical-source-sha256':hashes}
    return {'contracts.py':python_models(components),'contracts.ts':'\n'.join(lines)+'\n','openapi-components.json':json.dumps(document,ensure_ascii=False,indent=2)+'\n'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');a=p.parse_args()
    target=ROOT/'packages/contracts/generated';output=render()
    python_target=ROOT/'packages/eios-core/src/nexloop_eios/contracts.py'
    if a.check:
        if any(not (python_target if name=='contracts.py' else target/name).is_file() or (python_target if name=='contracts.py' else target/name).read_text()!=content for name,content in output.items()):
            print('generated contract drift');return 1
    else:
        target.mkdir(exist_ok=True)
        for name,content in output.items():(python_target if name=='contracts.py' else target/name).write_text(content)
    count=len(json.loads(output["openapi-components.json"])["components"]["schemas"])
    print(f'{count} canonical contracts: generated components and types verified');return 0


if __name__=='__main__':raise SystemExit(main())
