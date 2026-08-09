// One fabric session, driven from a PLAIN process. Reads a JSON request on stdin, writes one
// JSON response on stdout, and exits 0 even on failure -- the caller reads the JSON, not the code.
//
// WHY THIS FILE EXISTS AT ALL. `agent_swarm` is dependency-free Python and fabric's programmatic
// surface is an ES module. This is the smallest possible bridge: it makes no decisions, applies no
// policy, and knows nothing about jobs, claims or verdicts. Every judgement about what a session
// produced happens in `agent_executor.py`, where it can be tested without a node.
//
// THE IMPORTS ARE DYNAMIC ON PURPOSE. A static `import './engine/node-client.mjs'` resolves
// relative to THIS FILE, not to the working directory -- so a driver that lives inside the Python
// package could never find a plugin that lives in the plugin cache. Importing by absolute file URL
// is what lets this file stay in `agent_swarm` instead of being copied into someone else's package.
//
// It is deliberately NOT a session manager. fabric already is one.

import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const request = JSON.parse(await new Promise((resolve) => {
  let buf = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (d) => { buf += d; });
  process.stdin.on('end', () => resolve(buf));
}));

const reply = (payload) => { process.stdout.write(JSON.stringify(payload)); };
const fromPlugin = (rel) => import(pathToFileURL(join(request.pluginDir, rel)).href);

const started = Date.now();
let handle = null;
try {
  const { loadFabricConfig } = await fromPlugin('engine/node-config.mjs');
  const { openRemoteSession } = await fromPlugin('engine/node-client.mjs');

  const fc = loadFabricConfig();
  const entries = Object.entries(fc.nodes || {});
  if (!entries.length) throw new Error('no fabric nodes configured');
  // The caller names the node or takes the first. Choosing by free memory would be SCHEDULING, and
  // scheduling lives in agent_swarm.admission -- not here, and not inside fabric.
  const picked = request.node ? entries.find(([name]) => name === request.node) : entries[0];
  if (!picked) throw new Error(`no configured fabric node named ${request.node}`);
  const [name, n] = picked;

  handle = await openRemoteSession({
    host: n.host, port: n.port, token: n.token || fc.token,
    provider: request.provider,
    model: request.model ?? undefined,
    write: !!request.write,
    project: request.project ?? undefined,
  });
  const spawnMs = Date.now() - started;

  const turn = await handle.send(request.prompt);
  const exitCode = await handle.close();
  handle = null;

  reply({
    ok: true,
    node: name,
    exitCode,
    // MEASURED: claude and codex both answer with the same {text, turn, usage?} shape, so no
    // provider-specific unwrapping is needed here or anywhere above it.
    text: typeof turn === 'string' ? turn : (turn?.text ?? ''),
    spawn_ms: spawnMs,
    total_ms: Date.now() - started,
  });
} catch (e) {
  // A transport failure is REPORTED, not thrown: the Python side must be able to tell "the node
  // refused the connection" from "the session ran and went badly", and an exit code cannot.
  reply({ ok: false, error: `${e.code ? `${e.code}: ` : ''}${String(e.message).slice(0, 500)}` });
} finally {
  try { await handle?.close(); } catch { /* the reply is already written; closing is best effort */ }
}
