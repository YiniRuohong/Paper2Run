import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
export function bridge() {
  const child = spawn(process.execPath, [fileURLToPath(new URL('../bridge.mjs', import.meta.url))], { stdio: ['pipe', 'pipe', 'pipe'] });
  const messages = [], waiters = [];
  let stderr = '';
  child.stderr.on('data', data => stderr += data);
  createInterface({ input: child.stdout }).on('line', line => {
    const message = JSON.parse(line); messages.push(message);
    for (const w of [...waiters]) if (w.predicate(message)) { waiters.splice(waiters.indexOf(w), 1); clearTimeout(w.timer); w.resolve(message); }
  });
  return {
    child, messages, stderr: () => stderr,
    send: message => child.stdin.write(JSON.stringify(message) + '\n'),
    wait(predicate, ms = 15000) {
      const found = messages.find(predicate); if (found) return Promise.resolve(found);
      return new Promise((resolve, reject) => {
        const w = { predicate, resolve, timer: setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`bridge timeout: ${JSON.stringify(messages)} ${stderr}`)); }, ms) };
        waiters.push(w);
      });
    },
    done: once(child, 'exit'),
  };
}
export function config(cwd, base_url, overrides = {}) {
  return { cwd, session_dir: cwd, system_prompt: 'You are a test assistant.', model: { id: 'test', api: 'openai-completions', base_url, api_key: 'secret-bridge-test-key', context_window: 32768, max_tokens: 128 }, tools: [], limits: { max_model_calls: 3, max_model_tokens: 10000, wall_time_seconds: 10 }, ...overrides };
}
