# 청약레이더 v4 · 친구별 로그인 + 맞춤 웹 푸시

**실제 앱 화면, 자격/로또 분석, 설정 기능을 유지하면서** 친구별 이메일 로그인과 Supabase 클라우드 저장, 푸시 구독 등록 및 GitHub Actions 발송 코드를 추가한 버전입니다.

## 가장 간단한 설정 순서

1. [Supabase](https://supabase.com/dashboard)에서 무료 프로젝트 생성 → **SQL Editor**에서 `schema.sql` 전체 실행. 친구별 데이터는 RLS로 보호합니다.
2. **Authentication → URL Configuration**에서 GitHub Pages 앱 주소를 **Site URL** 및 **Redirect URLs**에 등록합니다. (예: `https://사용자.github.io/cheongyak-radar/`)
3. **Project Settings → API**(또는 Connect 창)에서 **Project URL**과 **publishable/anon 키**를 복사하여 `config.js`의 `supabaseUrl`, `supabaseAnonKey`에 입력합니다. publishable/anon 키만 브라우저에 공개해도 되며, **service_role 키는 절대 넣지 않습니다**.
4. VAPID 키 만들기: 컴퓨터에서 `pip install py-vapid` 후 `vapid --gen`을 사용할 수 있지만, 키 파일 양식에 따라 변환이 필요할 수 있습니다. 간단한 방법은 `npx web-push generate-vapid-keys` (Node.js 설치 필요)입니다. 출력의 **Public Key**를 `config.js`의 `vapidPublicKey`에 입력합니다. **Private Key는 공개 파일에 넣지 마세요**.
5. GitHub 저장소에 **압축 풀린 파일 전체**를 업로드하고 `Settings → Pages → Deploy from a branch → main /(root)`로 배포합니다. `config.js`에는 공개키만 있습니다.
6. GitHub 저장소 `Settings → Secrets and variables → Actions`에서 다음 **Repository secrets**를 각각 추가합니다.
   - `SUPABASE_URL`: Supabase Project URL
   - `SUPABASE_SERVICE_ROLE_KEY`: Supabase secret/service_role 키 (GitHub Secrets에만)
   - `VAPID_PRIVATE_KEY`: 4번의 VAPID Private Key
   - `VAPID_SUBJECT`: `mailto:본인이메일@example.com` 형태 (실제 관리 이메일)
   - 청약홈 수집용 `DATA_GO_KR_KEY`는 기존처럼 별도 등록 (사용자가 나중에 연결)
7. 앱에서 친구마다 **내 조건 → 이메일 로그인 → 메일 링크 클릭 → 조건 저장**을 진행합니다. **알림 → 이 기기 푸시 알림 켜기**로 수신 동의 후 구독을 저장합니다. 아이폰은 Safari에서 **홈 화면에 추가**하여 설치한 상태에서 푸시를 켭니다.
8. 실제 공고 API는 **나중에 연결해도** 됩니다. `data.json`에 검증된 실제 공고 데이터가 없으면 `push_sender.py`는 발송하지 않습니다. 나중에 API 연동 후 `Actions → 친구별 맞춤 푸시 알림 → Run workflow`로 테스트할 수 있습니다. 자동 실행 기본 간격은 3시간입니다.

## 핵심 보안 및 한계

- 친구의 프로필은 `auth.uid()` 기준 RLS 보호, 푸시 엔드포인트도 본인만 조회/수정 가능.
- 실제 공고 연동 전에 **실공고 자동 알림은 발송되지 않습니다**. 앱 내 기존 알림·테스트 알림은 동작합니다.
- 자격 판정은 공고문 없이 확정하지 않습니다. 시세 출처·기준일 없는 공고를 로또라고 단정하지 않습니다.
- 로그인하지 않아도 로컬 체험은 가능하나, 친구별 클라우드 저장과 웹 푸시는 로그인 및 외부 서비스 설정이 필수입니다.
- 설정 저장은 브라우저와 클라우드의 동기화 방식이며, 다중 기기에서 동시에 수정할 때 마지막 저장이 우선합니다.
- 이메일 로그인 링크 발송에는 Supabase 기본 이메일 발송 한도가 적용됩니다. 소규모 초대용 테스트에 맞춰 사용하세요.
- **중복 방지**는 push_deliveries 기록으로 처리합니다. 여러 기기 중 일부만 성공한 경우 다음 실행의 기기별 재시도는 하지 않습니다.
- 민감한 개인정보를 입력받기 전에 친구들의 동의를 받고, 공유 기기 사용을 피하세요.

## 코드 구성
`index.html` (앱/UI), `config.js`(브라우저 공개 설정), `schema.sql`(RLS 테이블), `sw.js`(웹 푸시 수신), `push_sender.py`(서버 발송), `.github/workflows/push.yml`(자동 푸시), `collect.py`(공고 수집 연결 준비)
