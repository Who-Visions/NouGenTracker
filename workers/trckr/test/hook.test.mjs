import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

test('Stop hook reads one turn per requestId with 5m/1h cache writes', () => {
  const dir = mkdtempSync(join(tmpdir(), 'trckr-hook-'));
  const usage = { input_tokens: 12, output_tokens: 340, cache_read_input_tokens: 48000, cache_creation_input_tokens: 2100, cache_creation: { ephemeral_5m_input_tokens: 100, ephemeral_1h_input_tokens: 2000 } };
  const rows = [
    { type: 'user', message: { content: 'hi' } },
    { type: 'assistant', requestId: 'req_A', timestamp: '2026-11-24T04:00:00Z', message: { model: 'claude-opus-5-5', usage } },
    { type: 'assistant', requestId: 'req_A', timestamp: '2026-11-24T04:00:01Z', message: { model: 'claude-opus-5-5', usage } },
    { type: 'assistant', requestId: 'req_B', timestamp: '2026-11-24T04:01:00Z', message: { model: 'claude-haiku-5-5', usage: { input_tokens: 5, output_tokens: 6, cache_creation_input_tokens: 7 } } },
    { type: 'assistant', requestId: 'req_C', message: { model: '<synthetic>', usage: { input_tokens: 0, output_tokens: 0 } } },
  ];
  const path = join(dir, 't.jsonl');
  writeFileSync(path, rows.map((r) => JSON.stringify(r)).join('\n'));
  const out = execFileSync('python3', ['-c', `import importlib.util,json,sys\nspec=importlib.util.spec_from_file_location('h',sys.argv[1]);h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)\nprint(json.dumps(h.turns_from_transcript(sys.argv[2],'s1','lane')))`, new URL('../hooks/trckr_stop_hook.py', import.meta.url).pathname, path]);
  const turns = JSON.parse(out);
  assert.equal(turns.length, 2);
  assert.deepEqual([turns[0].cache_write_5m_tokens, turns[0].cache_write_1h_tokens, turns[0].cache_read_tokens], [100, 2000, 48000]);
  assert.deepEqual([turns[1].cache_write_5m_tokens, turns[1].cache_write_1h_tokens], [7, 0]);
});
