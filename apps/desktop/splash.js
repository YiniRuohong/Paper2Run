'use strict';

const status = document.getElementById('status');
const failure = document.getElementById('failure');
const bar = document.getElementById('bar');
const log = document.getElementById('log');

window.p2rSplash.onState((state) => {
  const failed = state.state === 'failed';
  document.body.classList.toggle('failed', failed);
  bar.hidden = failed;
  failure.hidden = !failed;
  status.textContent = failed ? state.message : '正在启动本地服务…';
  log.textContent = failed ? state.log || '' : '';
  if (failed) log.scrollTop = log.scrollHeight;
});

document.getElementById('retry').addEventListener('click', () => window.p2rSplash.retry());
document.getElementById('logs').addEventListener('click', () => window.p2rSplash.openLogs());
document.getElementById('quit').addEventListener('click', () => window.p2rSplash.quit());
