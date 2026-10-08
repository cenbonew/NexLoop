// NexLoop integration probe against frozen Pi source; not a business Action test.
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { BACKGROUND_CONTEXT } from '@earendil-works/chord/context';
import { registerStorageConformance } from '../src/testing/index.ts';
import { openNodeSqliteDatabase } from '../src/storage/sqlite/node.ts';
import { SqliteStorage } from '../src/storage/sqlite/storage.ts';

registerStorageConformance({ describe, expect, it }, 'NexLoop frozen Pi SQLite FULL', async (use) => {
  const directory = await mkdtemp(join(tmpdir(), 'nexloop-pi-conformance-'));
  const db = await openNodeSqliteDatabase(join(directory, 'runtime.sqlite'));
  let storage: SqliteStorage | undefined;
  try {
    await db.exec('PRAGMA synchronous = FULL');
    expect((await db.get<{synchronous: number}>('PRAGMA synchronous'))?.synchronous).toBe(2);
    expect((await db.get<{journal_mode: string}>('PRAGMA journal_mode'))?.journal_mode).toBe('wal');
    storage = await SqliteStorage.open(db);
    await use(storage);
  } finally {
    if (storage) await storage.close(BACKGROUND_CONTEXT);
    else await db.close();
    await rm(directory, { recursive: true, force: true });
  }
});
