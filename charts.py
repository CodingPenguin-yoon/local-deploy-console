"""Render existing Git charts; never install charts, execute hooks or fetch dependencies."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

import yaml


def relative_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or ':' in value or value.startswith('-'):
        raise ValueError('저장소 내부 상대 경로를 입력하세요.')
    return str(path)


def fetch(state, repository, branch, run):
    if branch.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_./-]+', branch):
        raise ValueError('브랜치 형식 오류')
    repo = state / ('repo-' + hashlib.sha256(repository.encode()).hexdigest()[:16])
    if not repo.exists():
        run(['git', 'init', '--bare', str(repo)])
    run(['git', '-C', str(repo), 'fetch', '--depth=1', '--no-tags', repository, 'refs/heads/' + branch], 120)
    commit = run(['git', '-C', str(repo), 'rev-parse', 'FETCH_HEAD'])
    return repo, commit


def files(repo, commit, run):
    names = run(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', commit]).splitlines()
    return {
        'charts': [str(PurePosixPath(p).parent) for p in names if p == 'Chart.yaml' or p.endswith('/Chart.yaml')],
        'values': [p for p in names if p.endswith(('.yaml', '.yml')) and '/templates/' not in p and PurePosixPath(p).name != 'Chart.yaml'],
    }


def image_check(image):
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_@-]*', image):
        raise ValueError('유효하지 않은 컨테이너 이미지')
    if '@sha256:' in image:
        if not re.fullmatch(r'[0-9a-f]{64}', image.split('@sha256:', 1)[1]):
            raise ValueError('이미지 digest 형식 오류')
    elif ':' not in image.rsplit('/', 1)[-1] or image.rsplit(':', 1)[-1] in ('latest', 'not-built-yet'):
        raise ValueError('이미지는 빌드 완료된 고유 태그 또는 digest로 지정하세요.')
    return image


def workloads(documents, namespace):
    docs = [d for d in documents if isinstance(d, dict)]
    results = []
    for doc in docs:
        kind = doc.get('kind')
        if kind not in ('Deployment', 'StatefulSet'):
            continue
        metadata = doc.get('metadata', {})
        if 'helm.sh/hook' in metadata.get('annotations', {}):
            continue
        if metadata.get('namespace', namespace) != namespace:
            raise ValueError('선택한 namespace 밖의 워크로드는 등록할 수 없습니다.')
        name = metadata['name']
        template = doc['spec']['template']
        labels = template.get('metadata', {}).get('labels', {})
        related = []
        service_names = set()
        for service in docs:
            if service.get('kind') != 'Service' or service.get('metadata', {}).get('namespace', namespace) != namespace:
                continue
            selector = service.get('spec', {}).get('selector', {})
            if selector and all(labels.get(k) == v for k, v in selector.items()):
                service_names.add(service['metadata']['name'])
                related.append({'kind': 'Service', 'name': service['metadata']['name'], 'detail': ', '.join(str(p['port']) for p in service.get('spec', {}).get('ports', []))})
        for ingress in docs:
            if ingress.get('kind') != 'Ingress' or ingress.get('metadata', {}).get('namespace', namespace) != namespace:
                continue
            spec = ingress.get('spec', {})
            routes = [route for rule in spec.get('rules', []) for route in rule.get('http', {}).get('paths', [])]
            backends = [r.get('backend', {}) for r in routes] + [spec.get('defaultBackend', {})]
            if any(b.get('service', {}).get('name') in service_names for b in backends):
                related.append({'kind': 'Ingress', 'name': ingress['metadata']['name'], 'detail': ', '.join(r.get('host', '') for r in spec.get('rules', []))})
        for volume in template['spec'].get('volumes', []):
            claim = volume.get('persistentVolumeClaim', {}).get('claimName')
            if claim:
                related.append({'kind': 'PersistentVolumeClaim', 'name': claim, 'detail': '워크로드가 참조하는 볼륨'})
        for container in template['spec'].get('containers', []):
            results.append({'kind': kind, 'deployment': name, 'container': container['name'], 'desired': image_check(container['image']), 'related': related})
    if not results:
        raise ValueError('Deployment 또는 StatefulSet을 찾지 못했습니다.')
    return results


def render(repo, commit, chart_path, values_file, release, namespace, run):
    chart_path = relative_path(chart_path)
    if values_file:
        values_file = relative_path(values_file)
    for value in (release, namespace):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', value):
            raise ValueError('release/namespace 형식 오류')
    archive = subprocess.run(['git', '-C', str(repo), 'archive', commit], capture_output=True, timeout=30, check=True).stdout
    with tempfile.TemporaryDirectory(prefix='deploy-chart-') as tmp:
        root = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                relative_path(member.name.rstrip('/'))
                if not (member.isdir() or member.isfile()):
                    raise ValueError('심볼릭 링크가 포함된 저장소는 지원하지 않습니다.')
                dest = root / member.name
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tar.extractfile(member).read())
        args = ['helm', 'template', release, str(root / chart_path), '--namespace', namespace]
        if values_file:
            args.extend(['--values', str(root / values_file)])
        try:
            rendered = run(args)
        except Exception:
            raise ValueError('Helm 렌더링 실패: release·values와 필수 값, vendored 의존 차트를 확인하세요. 의존성은 자동 다운로드하지 않습니다.') from None
    return workloads(yaml.safe_load_all(rendered), namespace)
