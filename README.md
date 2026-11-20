# Local Deploy Console

팀당 교육장 PC 한 대에서 실행하는 로컬 배포 콘솔입니다. 기존 Git 인증과 kubeconfig/AWS 프로필을 사용합니다. Argo CD 서버 또는 클러스터 컨트롤러를 설치하지 않으며 네트워크·권한 제한을 우회하지 않습니다.

## 부트스트랩

macOS에서 [Homebrew](https://brew.sh/)가 설치되어 있다면 다음 명령으로 필수 도구를 준비합니다. 이미 Python 3.10 이상, Git, Helm, kubectl이 있다면 바로 아래의 저장소 복제 단계부터 시작하세요.

```sh
brew install python git helm kubernetes-cli
```

새로 설치할 디렉터리의 상위 폴더에서 실행합니다.

```sh
git clone https://github.com/CodingPenguin-yoon/local-deploy-console.git
cd local-deploy-console
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

브라우저에서 [로컬 배포 콘솔](http://127.0.0.1:8765)을 엽니다. 이미 포트를 사용 중이면 `PORT=8766 .venv/bin/python app.py`로 실행하고 브라우저에서도 같은 포트를 사용하세요.

워크로드를 등록하려면 이 PC에서 배포 저장소를 읽을 수 있는 Git 인증과 대상 클러스터에 접근할 수 있는 kubeconfig가 필요합니다. kubeconfig가 AWS CLI 등 외부 인증 도구를 사용한다면 해당 도구도 준비하세요.

`Ctrl+C`로 콘솔을 종료하면 폴링도 멈춥니다. 다음부터는 저장소 폴더에서 `.venv/bin/python app.py`만 실행하면 됩니다. 재시작 후에는 원하는 카드에서 자동 배포를 다시 켜세요.

## 등록 순서

1. `차트 연결`에서 토큰 없는 HTTPS Git 저장소 주소를 입력합니다.
2. `연결 확인`으로 기존 Git credential helper의 읽기 권한을 검증합니다. 비공개 저장소도 지원합니다. 앱에는 GitHub/AWS 키를 입력하지 않습니다.
3. 실제 브랜치 목록, 차트 경로, values 파일을 선택합니다. values 파일을 선택하지 않으면 차트 기본값을 씁니다.
4. **기존 배포와 같은 release 이름**, Kubernetes context와 namespace를 입력합니다.
5. `워크로드 미리보기`를 누르면 Helm을 로컬에서 렌더링합니다. Deployment·StatefulSet의 컨테이너별 관리 카드를 찾습니다. 하나의 차트에 웹·서버·Redis가 있어도 각각 구분됩니다.
6. 선택한 항목을 등록합니다. 해당 namespace에서 워크로드가 실제 존재하고 읽을 수 있어야 합니다. 모든 자동 배포는 OFF로 시작합니다.

각 카드에서 현재/목표 이미지, Ready 수, Git 커밋을 확인하고 수동 배포 또는 자동 배포를 선택합니다. 상세에는 Service selector와 Ingress backend를 기준으로 연결한 리소스 및 참조 PVC를 표시합니다. 연결 관계는 **Git 차트 기준**이며 실제 Service/Ingress 상태를 별도로 조회하는 기능은 아닙니다. 로그는 최근 100줄, 최대 16KB를 조회하고 배포 이력에 저장하지 않습니다.

기존 이미지 YAML 방식의 API도 호환을 위해 유지하되 웹 등록은 Helm 차트를 사용합니다.

## 배포 동작

- GitHub Actions가 이미지 빌드·Harbor push를 완료하고 배포 저장소의 고유 태그를 갱신합니다.
- 자동 배포 ON인 대상만 60초마다 Git과 현재 워크로드를 조회합니다. 웹의 3초 새로고침은 로컬 저장 상태만 읽습니다. OFF인 대상은 `조회` 버튼으로 갱신합니다.
- `helm template` 결과에서 **등록한 워크로드와 컨테이너의 이미지 하나**만 읽습니다.
- 매 명령에 context와 namespace를 명시합니다. 배포 직전 patch 권한도 확인합니다.
- `kubectl set image`로 변경하고 최대 180초 동안 rollout을 기다립니다. 배포 후 이미지가 목표와 일치하는지 다시 확인합니다.
- Service·Ingress·PVC·Secret 생성/수정/삭제, chart install/upgrade, prune은 하지 않습니다. 저장소 스크립트와 Helm hooks는 실행하지 않습니다.
- 새로운 워크로드가 Git에 추가되어도 자동 등록하거나 생성하지 않습니다. 다시 차트를 미리 보고 추가하세요.
- 차트 의존성을 자동 다운로드하지 않습니다. 저장소에 필요한 의존 차트를 포함해야 합니다. 심볼릭 링크를 포함한 저장소는 지원하지 않습니다.
- StatefulSet은 일반 RollingUpdate만 지원합니다. OnDelete 또는 partition 전략은 이미지 변경 전에 중지합니다.
- 실패 시 자동 배포 OFF, 오류와 이력을 저장합니다. 자동 롤백하지 않습니다.
- 앱 안의 배포는 직렬화합니다. 이미 성공한 커밋은 자동 재배포하지 않고, 현재 이미지가 목표와 같으면 이미지 변경을 실행하지 않습니다.
- 앱 재시작 시 모든 자동 배포는 OFF입니다. 팀당 담당 PC 한 대만 사용하세요. PC 간 분산 잠금은 없습니다.

## 인증과 로컬 데이터

Git은 기존 credential helper, kubectl은 기존 kubeconfig의 exec 인증을 사용합니다. context 이름 자체가 권한이 아니며 서버 RBAC가 접근 범위를 결정합니다. 앱은 팀 고정 제품이 아니므로 등록 시 올바른 context/namespace를 확인해야 합니다. 다른 클러스터 인증이 PC에 있으면 해당 context도 사용할 수 있습니다.

설정·최근 배포 이력 100건·bare Git 캐시는 `.state/`에 저장되며 Git에서 제외됩니다. 토큰 포함 저장소 URL은 받지 않습니다. 웹은 IPv4 loopback 전용이며 Host 검사와 요청 토큰을 사용합니다. 외부에 공개하지 마세요. 로그 자체는 애플리케이션이 출력한 내용이므로 사용자의 앱에서도 비밀값을 로그에 기록하지 않아야 합니다.

기존 Argo CD나 다른 자동 배포 도구가 같은 리소스를 관리하지 않는 상태에서 사용하세요. 운영자가 허용한 로컬 배포 범위에서 사용합니다.

## 검증

```sh
.venv/bin/python -m unittest discover -s tests -v
```

테스트는 실제 클러스터를 수정하지 않습니다. 복수 워크로드 분리, Service/Ingress 연결, namespace 차단, 이미지 변경 명령, 중복 방지와 실패 처리를 검증합니다.

## 라이선스

[MIT License](LICENSE)
