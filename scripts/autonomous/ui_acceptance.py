"""Real standalone-browser UI acceptance.
Run: uv run --with playwright python scripts/autonomous/ui_acceptance.py
Requires built apps/web/dist, installed harness dependencies and Docker image.
Only model-provider SSE is scripted; no product API/state is mocked.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
import zipfile

import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect
from p2r_api.main import create_app
from p2r_api.auth import IdentityService, csrf_token
from p2r_artifacts.store import LocalArtifactStore
from p2r_persistence.models import UserRecord

ROOT = Path(__file__).resolve().parents[2]
BASE = '/api/v1/autonomous'


def main():
    evidence = ROOT / '.p2r-e2e-audit/ui' / time.strftime('%Y%m%d-%H%M%S')
    evidence.mkdir(parents=True)
    os.environ['P2R_AUTONOMOUS_DIR'] = str(evidence / 'control')
    port = int(os.getenv('P2R_UI_PORT', '8173'))
    origin = f'http://127.0.0.1:{port}'
    result = {'assertions': [], 'evidence': str(evidence), 'origin': origin}
    captured = []
    release = threading.Event()
    proposed = {'objective': 'UI controlled protocol', 'mode': 'exploratory', 'method': 'browser approved method',
                'entrypoint': 'main.py', 'evaluation': {'adapter': 'regression', 'metric': 'mae'},
                'expected_artifacts': ['main.py'],
                'budget': {'wall_time_seconds': 300, 'max_model_calls': 30, 'max_model_tokens': 200000,
                           'storage_bytes': 5368709120, 'gpu_devices': []}}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append(body)
            number = len(captured)
            if number in (2, 4):
                release.wait(150)
                return
            calls = [('execute', {'command': 'printf browser-real-docker-evidence', 'timeout_seconds': 15})] if number == 1 else [('propose_protocol', {'protocol': proposed, 'reason': 'UI exact approval acceptance'})]
            delta = {'role': 'assistant', 'tool_calls': [
                {'index': i, 'id': f'ui_{number}_{i}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(calls)]}
            chunks = [
                {'id': 'ui', 'object': 'chat.completion.chunk', 'model': 'ui-scripted', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                {'id': 'ui', 'object': 'chat.completion.chunk', 'model': 'ui-scripted', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}}]
            raw = (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(raw))); self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    app = create_app(db_url=f'sqlite:///{evidence / "api.sqlite"}', allowed_origin=origin, secure_cookies=False,
                     artifact_store=LocalArtifactStore(evidence / 'artifacts'))
    svc = app.state.runtime['autonomous_service']
    with svc.sessions() as db:
        db.add(UserRecord(id='ui-owner', display_name='UI acceptance', can_create_project=True)); db.flush()
        secret = IdentityService(db).create_session('ui-owner'); db.commit()
    app.mount('/', StaticFiles(directory=ROOT / 'apps/web/dist', html=True), name='ui')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    http = httpx.Client(base_url=origin, cookies={'p2r_session': secret}, headers={'Origin': origin, 'X-CSRF-Token': csrf_token(secret)}, timeout=25)
    task_id = None
    context = browser = None

    def check(name):
        result['assertions'].append(name)
        print('PASS:', name, flush=True)
    def write(method, path, body):
        response = http.request(method, path, json=body, headers={'Idempotency-Key': uuid4().hex})
        assert response.is_success, response.text
        return response.json()['data']
    def wait(observe, predicate, timeout=75):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            last = observe()
            if predicate(last): return last
            time.sleep(.25)
        raise AssertionError(f'Observation timed out: {last}')
    def task(): return http.get(BASE + f'/tasks/{task_id}').json()['data']
    def events(): return http.get(BASE + f'/tasks/{task_id}/events').json()['data']['events']

    try:
        wait(lambda: server.started, bool, 20)
        project = write('POST', '/api/v1/projects', {'name': 'UI acceptance existing project'})
        project_id = project['id']
        paper = write('POST', f'/api/v1/projects/{project_id}/papers/text', {'text': 'Controlled browser acceptance paper. Method: bounded local execution. Metric: mean absolute error. This is test data, not a scientific claim.', 'title_hint': 'Browser acceptance paper', 'source_processing_consent': True})
        result['fixtures'] = {'project': project, 'paper': paper}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            context.add_cookies([{'name': 'p2r_session', 'value': secret, 'url': origin, 'httpOnly': True, 'sameSite': 'Lax'}])
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on('pageerror', lambda error: result.setdefault('page_errors', []).append(str(error)))
            try:
                page.goto(origin + '/#/autonomous')
                expect(page.get_by_role('heading', name='自主实验', exact=True)).to_be_visible()
                page.locator('.auto-field').filter(has=page.locator('span').get_by_text('项目', exact=True)).locator('select').select_option(project_id)
                expect(page.get_by_label('论文版本')).not_to_have_value('', timeout=15000)
                check('existing project and immutable paper version selected in browser')
                page.get_by_label('实验目标', exact=True).fill('UI controlled protocol')
                page.get_by_label('配置 ID', exact=True).fill('ui-scripted')
                page.get_by_label('Base URL', exact=True).fill(f'http://127.0.0.1:{provider.server_port}/v1')
                page.get_by_label('模型名称', exact=True).fill('ui-scripted')
                page.get_by_label('API Key（仅本次内存）', exact=True).fill('ui-only-fake-key')
                page.get_by_role('button', name='保存并使用模型', exact=True).click()
                expect(page.get_by_label('API Key（仅本次内存）', exact=True)).to_have_value('')
                expect(page.locator('.auto-field').filter(has=page.locator('span').get_by_text('已有模型配置', exact=True)).locator('select')).to_have_value('ui-scripted')
                profiles = http.get(BASE + '/profiles').json()['data']
                assert profiles[0]['secret_configured'] and 'ui-only-fake-key' not in json.dumps(profiles)
                check('profile saved through UI; password cleared; API excludes credential')
                # UI does not expose context/max-token fields. Persist profile options through real API
                # so abort reservation does not consume the entire cumulative test budget.
                write('POST', BASE + '/profiles', {'id': 'ui-scripted', 'base_url': f'http://127.0.0.1:{provider.server_port}/v1', 'model': 'ui-scripted', 'api': 'openai-completions', 'api_key': 'ui-only-fake-key', 'context_window': 4096, 'max_tokens': 128})
                skill = page.locator('.auto-skill').first
                skill.locator('input[type=checkbox]').check()
                page.get_by_label('最长运行时间（秒）', exact=True).fill('300')
                page.get_by_label('模型 Token 上限', exact=True).fill('200000')
                page.get_by_label('本地项目工作区', exact=True).fill('ui-workspace')
                page.get_by_role('button', name='保存配置', exact=True).click()
                expect(page.get_by_role('status').filter(has_text='项目配置已保存')).to_be_visible()
                saved = http.get(BASE + f'/projects/{project_id}/config').json()['data']
                assert len(saved['config']['harness']['skills']) == 1
                check('skill selection and configuration persisted via real API')
                expect(page.get_by_role('button', name='启动自主实验', exact=True)).to_be_disabled()
                page.get_by_label('我授权', exact=False).check()
                with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith(f'/projects/{project_id}/tasks')) as started:
                    page.get_by_role('button', name='启动自主实验', exact=True).click()
                response = started.value
                assert response.status == 202, response.text()
                task_id = response.json()['data']['id']; result['task_id'] = task_id
                check('authorization gates launch and real task API returns 202')
                wait(events, lambda es: any(e['type'] == 'tool_completed' for e in es))
                tool = next(e for e in events() if e['type'] == 'tool_completed')
                assert not tool['payload']['is_error'], tool
                assert 'browser-real-docker-evidence' in json.dumps(tool)
                expect(page.get_by_role('list', name='任务事件')).to_contain_text('tool_completed', timeout=15000)
                check('real SDK execute ran inside Docker and appeared in UI event stream')
                selected_option = page.locator('.auto-field').filter(has=page.locator('span').get_by_text('项目任务', exact=True)).locator('select option:checked')
                expect(selected_option).to_contain_text('运行中', timeout=15000)
                page.screenshot(path=str(evidence / '01-running.png'), full_page=True)
                wait(lambda: len(captured), lambda n: n >= 2)
                page.get_by_role('button', name='暂停', exact=True).click()
                wait(task, lambda t: t['state'] == 'paused')
                time.sleep(2)
                expect(selected_option).to_contain_text('已暂停', timeout=15000)
                check('pause button persisted paused state and synchronized selected task option')
                page.get_by_role('button', name='恢复 / 继续', exact=True).click()
                pending = wait(task, lambda t: t.get('pending_decision') is not None)
                check('ordinary resume restarted real SDK and received protocol proposal')
                decision = pending['pending_decision']
                expect(page.get_by_role('heading', name='等待方案审批')).to_be_visible(timeout=15000)
                expect(page.get_by_text('方案 SHA256：' + decision['proposed']['sha256'], exact=True)).to_be_visible()
                expect(page.get_by_text('审批 ID：' + decision['id'], exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='恢复 / 继续', exact=True)).to_be_disabled()
                from p2r_contracts.autonomous import freeze_protocol
                assert decision['proposed'] == freeze_protocol(proposed)
                expect(selected_option).to_contain_text('已暂停', timeout=15000)
                page.screenshot(path=str(evidence / '02-approval.png'), full_page=True)
                check('UI renders exact frozen protocol/hash/id and disables generic resume')
                wait(lambda: svc.workers[task_id]['thread'].is_alive(), lambda active: not active, 15)
                for invalid in ({'action': 'resume'}, {'action': 'resume', 'message': 'approve:wrong-id'}):
                    denied = http.post(BASE + f'/tasks/{task_id}/control', json=invalid, headers={'Idempotency-Key': uuid4().hex})
                    assert denied.status_code == 409 and denied.json()['error']['code'] == 'PROTOCOL_APPROVAL_REQUIRED', denied.text
                    assert task()['pending_decision']['id'] == decision['id']
                check('real API rejects ordinary resume and incorrect approval ID without changing proposal')
                with page.expect_request(lambda r: r.url.endswith(f'/tasks/{task_id}/control') and r.method == 'POST') as approving:
                    page.get_by_role('button', name='批准此方案并继续', exact=True).click()
                assert approving.value.post_data_json == {'action': 'resume', 'message': 'approve:' + decision['id']}
                wait(events, lambda es: any(e['type'] == 'protocol_approved' for e in es))
                wait(lambda: len(captured), lambda n: n >= 4)
                assert task()['pending_decision'] is None
                check('approval submits exact approve:<id>, records protocol_approved, resumes SDK')
                page.get_by_role('button', name='停止', exact=True).click()
                wait(task, lambda t: t['state'] == 'stopped')
                expect(page.locator('.auto-monitor').get_by_role('status')).to_contain_text('已停止', timeout=15000)
                expect(selected_option).to_contain_text('已停止', timeout=15000)
                check('stop button terminates task and UI monitor/selected option display stopped')
                with page.expect_download() as download:
                    page.get_by_role('button', name='导出 ZIP', exact=True).click()
                destination = evidence / 'task-export.zip'; download.value.save_as(destination)
                with zipfile.ZipFile(destination) as archive:
                    assert archive.testzip() is None
                    result['zip_entries'] = archive.namelist()
                    assert {'task.json', 'protocol.json', 'logs/events.json', 'manifest.json'} <= set(archive.namelist())
                    exported = json.loads(archive.read('task.json'))
                    assert exported['id'] == task_id and exported['state'] == 'stopped'
                    assert json.loads(archive.read('protocol.json')) == decision['proposed']
                    assert b'ui-only-fake-key' not in b''.join(archive.read(name) for name in archive.namelist())
                check('browser ZIP download contains exact task/protocol/events and excludes fake credential')
                page.screenshot(path=str(evidence / '03-stopped.png'), full_page=True)
                result['result'] = 'passed'
            except Exception:
                page.screenshot(path=str(evidence / 'failure.png'), full_page=True)
                result['page_text'] = page.locator('body').inner_text()
                raise
            finally:
                context.tracing.stop(path=str(evidence / 'trace.zip'))
                browser.close()
    except Exception:
        result['result'] = 'failed'
        result['error'] = traceback.format_exc()
        print(result['error'], flush=True)
    finally:
        if task_id:
            result['final_task'] = task(); result['events'] = events()
            try: write('POST', BASE + f'/tasks/{task_id}/control', {'action': 'stop'})
            except Exception: pass
        svc.shutdown()
        containers = []
        # Only inspect/remove task containers created by this isolated service.
        for worker in svc.workers.values():
            target = worker.get('target')
            if target and getattr(target, 'container', None): containers.append(target.container)
        for container in set(containers):
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=30)
        result['cleaned_containers'] = containers
        release.set(); provider.shutdown(); provider.server_close()
        server.should_exit = True; thread.join(timeout=15); http.close()
        result['provider_calls'] = len(captured)
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('Evidence:', evidence, flush=True)
    return 0 if result['result'] == 'passed' else 1

if __name__ == '__main__':
    raise SystemExit(main())
