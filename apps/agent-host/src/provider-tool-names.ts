import { RuntimeError } from './runtime-adapter.js';

/** Stable wire-only names. Canonical EIOS registrations/execute closures remain unchanged. */
export function providerToolNames(names: readonly string[], deepseek: boolean): readonly string[] {
  if (names.some(name => !/^nexloop\.[A-Za-z0-9_.-]+$/.test(name)) || new Set(names).size !== names.length)
    throw new RuntimeError('runtime_tool_refused');
  const wire = names.map(name => deepseek ? name.replaceAll('.', '_') : name);
  if (deepseek && (wire.some(name => !/^[A-Za-z0-9_-]{1,128}$/.test(name)) || new Set(wire).size !== wire.length))
    throw new RuntimeError('runtime_tool_refused');
  return Object.freeze(wire);
}
