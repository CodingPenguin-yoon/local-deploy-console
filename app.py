"""Loopback-only Git image deployment console. Never executes repository scripts."""
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml
import charts

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.state'
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
DATA = {'targets': [], 'history': []}
PORT = int(os.environ.get('PORT', '8765'))
VERIFIED_REPOSITORIES = set()
PREVIEWS = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def run(args, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', AWS_PAGER='')
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        # Git output can contain credential-bearing URLs; never expose it.
        if args[0] == 'git':
            raise RuntimeError('Git 조회 실패: 저장소 주소·브랜치·로컬 인증을 확인하세요.')
        raise RuntimeError((result.stderr or result.stdout or '명령 실패')[-2000:])
    return result.stdout.strip()


def save():
    STATE.mkdir(mode=0o700, exist_ok=True)
    path = STATE / 'state.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(DATA, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def verify_repository(repository):
    url = urlparse(repository)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('토큰 없는 HTTPS 저장소 주소를 입력하세요.')
    refs = run(['git', 'ls-remote', '--symref', repository, 'HEAD', 'refs/heads/*'], 60)
    branches = sorted({line.split('refs/heads/', 1)[1] for line in refs.splitlines() if '\trefs/heads/' in line})
    if not branches:
        raise ValueError('읽을 수 있는 브랜치가 없습니다.')
    default = next((line.split('refs/heads/', 1)[1].split()[0] for line in refs.splitlines() if line.startswith('ref: refs/heads/')), branches[0])
    VERIFIED_REPOSITORIES.add(repository)
    return {'branches': branches, 'default_branch': default}


def validate(body):
    keys = ['name', 'repository', 'branch', 'values_file', 'context', 'namespace', 'deployment', 'container']
    target = {key: str(body.get(key, '')).strip() for key in keys}
    if not all(target.values()):
        raise ValueError('모든 항목을 입력하세요.')
    url = urlparse(target['repository'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('토큰 없는 HTTPS Git 저장소 주소를 입력하세요. 기존 Git credential helper를 사용합니다.')
    for key in ['branch', 'context', 'namespace', 'deployment', 'container']:
        if target[key].startswith('-') or not re.fullmatch(r'[a-zA-Z0-9_./:@-]+', target[key]):
            raise ValueError(f'{key} 형식이 올바르지 않습니다.')
    path = PurePosixPath(target['values_file'])
    if path.is_absolute() or '..' in path.parts or ':' in str(path):
        raise ValueError('태그 파일은 저장소 내부 상대 경로여야 합니다.')
    target.update(id=secrets.token_hex(6), auto=False, status='미확인', current='', desired='', commit='', error='')
    return target


def image_from_yaml(content):
    value = yaml.safe_load(content)
    spec = value.get('image', {}) if isinstance(value, dict) else {}
    repository, tag = spec.get('repository'), spec.get('tag')
    if not isinstance(repository, str) or not isinstance(tag, str):
        raise ValueError('YAML에 image.repository와 문자열 image.tag가 필요합니다.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_-]*', repository) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
        raise ValueError('이미지 주소 또는 태그 형식 오류')
    if tag in ['latest', 'not-built-yet']:
        raise ValueError('빌드 완료된 고유 태그를 사용하세요. latest/not-built-yet은 배포하지 않습니다.')
    return f'{repository}:{tag}'


def kube(target, *args):
    return ['kubectl', '--context', target['context'], '--namespace', target['namespace'], '--request-timeout=20s', *args]


def resource(target):
    return target.get('kind', 'Deployment').lower() + '/' + target['deployment']


def preview_chart(body):
    repository = str(body.get('repository', '')).strip()
    if repository not in VERIFIED_REPOSITORIES:
        raise ValueError('먼저 저장소 연결을 확인하세요.')
    config = {k: str(body.get(k, '')).strip() for k in ['repository', 'branch', 'chart_path', 'values_file', 'release', 'context', 'namespace']}
    if not config['context'] or config['context'].startswith('-'):
        raise ValueError('Kubernetes context를 지정하세요.')
    repo, commit = charts.fetch(STATE, repository, config['branch'], run)
    rendered = charts.render(repo, commit, config['chart_path'], config['values_file'], config['release'], config['namespace'], run)
    preview_id = secrets.token_hex(12)
    # Store only supported workload metadata, never Helm Secret manifests.
    PREVIEWS.clear()
    PREVIEWS[preview_id] = (config, rendered, commit)
    return {'preview_id': preview_id, 'workloads': rendered, 'commit': commit}


def register_chart(body):
    config, rendered, commit = PREVIEWS[body['preview_id']]
    selected = body.get('selected', [])
    if not selected:
        raise ValueError('등록할 워크로드를 선택하세요.')
    added = []
    for index in selected:
        if not isinstance(index, int) or not 0 <= index < len(rendered):
            raise ValueError('워크로드 선택 오류')
        item = rendered[index]
        target = dict(config, **item, id=secrets.token_hex(6), name=item['deployment'], auto=False, current='', commit=commit, status='미확인', error='')
        signature = lambda t: (t['context'], t['namespace'], t.get('kind', 'Deployment'), t['deployment'], t['container'])
        if any(signature(t) == signature(target) for t in DATA['targets'] + added):
            raise ValueError('이미 등록된 워크로드입니다: ' + item['deployment'])
        # Verify namespace access and workload existence before persisting any cards.
        live = json.loads(run(kube(target, 'get', resource(target), '-o', 'json')))
        target['current'] = next(c['image'] for c in live['spec']['template']['spec']['containers'] if c['name'] == target['container'])
        target.update(ready=live.get('status', {}).get('readyReplicas', 0), replicas=live['spec'].get('replicas', 1), checked_at=now())
        target['status'] = '이미지 일치' if target['current'] == target['desired'] else '변경 있음'
        added.append(target)
    DATA['targets'].extend(added)
    save()


def inspect(target):
    repo, commit = charts.fetch(STATE, target['repository'], target['branch'], run)
    if target.get('chart_path'):
        rendered = charts.render(repo, commit, target['chart_path'], target['values_file'], target['release'], target['namespace'], run)
        item = next((w for w in rendered if (w['kind'], w['deployment'], w['container']) == (target['kind'], target['deployment'], target['container'])), None)
        if item is None:
            raise ValueError('Git에서 등록한 워크로드/컨테이너가 사라졌습니다. 재등록을 확인하세요.')
        desired = item['desired']
        target['related'] = item['related']
    else:
        content = run(['git', '-C', str(repo), 'show', f"{commit}:{target['values_file']}"])
        desired = image_from_yaml(content)
    deployment = json.loads(run(kube(target, 'get', resource(target), '-o', 'json')))
    target['ready'] = deployment.get('status', {}).get('readyReplicas', 0)
    target['replicas'] = deployment.get('spec', {}).get('replicas', 1)
    target['observed_generation'] = deployment.get('status', {}).get('observedGeneration', 0)
    target['generation'] = deployment['metadata'].get('generation', 0)
    containers = deployment['spec']['template']['spec']['containers']
    current = next((c['image'] for c in containers if c['name'] == target['container']), None)
    if current is None:
        raise ValueError('지정한 컨테이너가 Deployment에 없습니다.')
    target.update(commit=commit, desired=desired, current=current, checked_at=now(), error='')
    target['status'] = '변경 있음' if desired != current else '이미지 일치'


def change_image(target):
    if run(kube(target, 'auth', 'can-i', 'patch', resource(target))) != 'yes':
        raise RuntimeError('선택한 워크로드의 이미지 변경 권한이 없습니다.')
    if target.get('kind') == 'StatefulSet':
        obj = json.loads(run(kube(target, 'get', resource(target), '-o', 'json')))
        strategy = obj['spec'].get('updateStrategy', {})
        if strategy.get('type', 'RollingUpdate') != 'RollingUpdate' or strategy.get('rollingUpdate', {}).get('partition', 0):
            raise ValueError('자동 교체되지 않는 StatefulSet 전략입니다. 이미지 배포를 중지합니다.')
    # Only the named container image is changed. No apply/prune or repo scripts.
    run(kube(target, 'set', 'image', resource(target), target['container'] + '=' + target['desired']))


def deploy(target):
    old = target['current']
    target['status'] = '배포 중'
    save()
    if old != target['desired']:
        change_image(target)
    run(kube(target, 'rollout', 'status', resource(target), '--timeout=180s'), 200)
    live = json.loads(run(kube(target, 'get', resource(target), '-o', 'json')))
    actual = next(c['image'] for c in live['spec']['template']['spec']['containers'] if c['name'] == target['container'])
    if actual != target['desired']:
        raise RuntimeError('배포 도중 이미지가 다른 값으로 변경됐습니다. 다른 배포 도구를 확인하세요.')
    target.update(current=actual, status='배포 성공', last_success_commit=target['commit'], ready=live.get('status', {}).get('readyReplicas', 0), replicas=live['spec'].get('replicas', 1))
    if old == actual:
        target['status'] = '이미지 일치'
        return
    DATA['history'].insert(0, dict(time=now(), target=target['name'], result='성공', previous=old, image=actual, commit=target['commit']))
    del DATA['history'][100:]


def operate(target, action):
    with LOCK:
        success = True
        try:
            inspect(target)
            if action == 'deploy' or (action == 'poll' and target['auto'] and target['commit'] != target.get('last_success_commit')):
                deploy(target)
        except Exception as exc:
            success = False
            target.update(status='실패 · 자동 배포 중지', error=str(exc), auto=False)
            DATA['history'].insert(0, dict(time=now(), target=target['name'], result='실패', image=target.get('desired', ''), error=str(exc)))
            del DATA['history'][100:]
        save()
        return success


def poll():
    while True:
        with LOCK:
            targets = [t for t in DATA['targets'] if t['auto']]
        for target in targets:
            operate(target, 'poll')
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value, mime='application/json'):
        payload = json.dumps(value, ensure_ascii=False).encode() if mime == 'application/json' else value.encode()
        self.send_response(code)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        return self.headers.get('Host') in [f'localhost:{PORT}', f'127.0.0.1:{PORT}']

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {'error': 'localhost only'})
        if self.path == '/':
            return self.reply(200, (ROOT / 'static/index.html').read_text().replace('__TOKEN__', TOKEN), 'text/html')
        if self.path == '/api/state':
            # Read-only snapshot remains available while a rollout is running.
            return self.reply(200, DATA)
        self.reply(404, {})

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Local-Token') != TOKEN:
            return self.reply(403, {'error': '잘못된 요청'})
        if not LOCK.acquire(blocking=False):
            return self.reply(409, {'error': '작업 진행 중입니다. 완료 후 다시 시도하세요.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 16384:
                raise ValueError('요청 크기 오류')
            body = json.loads(self.rfile.read(length))
            if self.path == '/api/repository':
                return self.reply(200, verify_repository(str(body.get('repository', '')).strip()))
            if self.path == '/api/catalog':
                if body.get('repository') not in VERIFIED_REPOSITORIES:
                    raise ValueError('먼저 저장소 연결을 확인하세요.')
                repo, commit = charts.fetch(STATE, body['repository'], body['branch'], run)
                return self.reply(200, charts.files(repo, commit, run))
            if self.path == '/api/preview':
                return self.reply(200, preview_chart(body))
            if self.path == '/api/register-chart':
                register_chart(body)
            elif self.path == '/api/targets':
                if str(body.get('repository', '')).strip() not in VERIFIED_REPOSITORIES:
                    raise ValueError('먼저 저장소 연결을 확인하세요.')
                target = validate(body)
                signature = lambda t: tuple(t[k] for k in ['context', 'namespace', 'deployment', 'container'])
                if any(signature(t) == signature(target) for t in DATA['targets']):
                    raise ValueError('이미 등록된 배포 대상입니다.')
                DATA['targets'].append(target)
                save()
            elif self.path == '/api/action':
                target = next(t for t in DATA['targets'] if t['id'] == body['id'])
                action = body['action']
                if action == 'logs':
                    logs = run(kube(target, 'logs', resource(target), '-c', target['container'], '--tail=100', '--limit-bytes=16000'), 30)
                    return self.reply(200, {'logs': logs})
                if action == 'auto':
                    target['auto'] = not target['auto']
                    save()
                elif action in ['check', 'deploy']:
                    # Runs synchronously under the lock; other deploy requests are rejected.
                    if not operate(target, action):
                        return self.reply(502, {'error': target['error']})
                else:
                    raise ValueError('지원하지 않는 명령')
            else:
                return self.reply(404, {})
            self.reply(200, {'ok': True})
        except Exception as exc:
            self.reply(400, {'error': str(exc)})
        finally:
            LOCK.release()


if __name__ == '__main__':
    STATE.mkdir(mode=0o700, exist_ok=True)
    if (STATE / 'state.json').exists():
        DATA = json.loads((STATE / 'state.json').read_text())
        # Never resume unattended deployments just because the app restarted.
        for target in DATA['targets']:
            target['auto'] = False
    threading.Thread(target=poll, daemon=True).start()
    print(f'Local Deploy Console: http://127.0.0.1:{PORT}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
