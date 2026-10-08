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


def render():
    files=sorted((ROOT/'packages/contracts').glob('*.schema.json'))
    if len(files)!=6:raise ValueError('expected six canonical contracts')
    components={};hashes={};lines=['// Generated from canonical schemas; do not edit.', '// Structural types only: enforce format, bounds, patterns and conditional world rules with JSON Schema at runtime.']
    for path in files:
        schema=json.loads(path.read_text());name=type_name(path.name.removesuffix('.schema.json'))
        components[name]=schema
        hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append('export type '+name+' = '+ts(schema)+';')
    # This component document is deliberately not an implemented route catalog.
    document={'openapi':'3.1.0','info':{'title':'NexLoop target contract components','version':'1.0'},'paths':{},'components':{'schemas':components},'x-canonical-source-sha256':hashes}
    return {'contracts.ts':'\n'.join(lines)+'\n','openapi-components.json':json.dumps(document,ensure_ascii=False,indent=2)+'\n'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');a=p.parse_args()
    target=ROOT/'packages/contracts/generated';output=render()
    if a.check:
        if any(not (target/name).is_file() or (target/name).read_text()!=content for name,content in output.items()):
            print('generated contract drift');return 1
    else:
        target.mkdir(exist_ok=True)
        for name,content in output.items():(target/name).write_text(content)
    print('six canonical contracts: generated components and structural types verified');return 0


if __name__=='__main__':raise SystemExit(main())
