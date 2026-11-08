# Urban Scale

서울·신도시·수도권의 도시계획·개발·행정 뉴스를 하루 여섯 번 모아 분야·지역·이슈별로 정리하는 개인 공부용 사이트.

- 사이트: https://anachtice.github.io/urban-scale/
- 기사 출처: 네이버 뉴스 검색 API(NAVER API HUB). 제목·짧은 요약·원문 링크만 보여 주고 본문은 저장하지 않는다.
- AI를 쓰지 않는다. 수집·분류·이슈 묶기·순위 모두 규칙이다.

## 파일 구성

| 파일 | 하는 일 |
|---|---|
| `collect.py` | 기사 수집 → 지역·분야 분류 → 이슈 묶기 → `docs/data/`에 저장 |
| `config/queries.json` | 검색어 (100여 개) |
| `config/categories.json` | 분야 8개와 분야별 낱말 |
| `config/regions.json` | 서울 25개 구·동네, 신도시, 경기·인천 시 이름, 거를 비수도권 지명 |
| `config/press.json` | 언론사 도메인 → 이름, 뺄 매체(연예·스포츠) |
| `config/settings.json` | 모으는 시간 범위, 사설·칼럼 머리말, 이슈 묶는 기준 |
| `docs/index.html` | 사이트 화면 |
| `docs/data/` | 날짜별 기사 파일 (자동으로 쌓인다) |
| `.github/workflows/collect.yml` | 하루 여섯 번 자동 실행 (한국시간 7·10·13·16·19·22시) |

## 처음 한 번 설정

### 1. 네이버 검색 API 키 받기 (NAVER API HUB)

2026년부터 네이버 검색 API는 네이버 개발자센터가 아니라 **네이버 클라우드 플랫폼의 NAVER API HUB**에서 발급한다. 개발자센터 애플리케이션 목록에 '검색'이 안 보이는 게 그래서다.

1. https://www.ncloud.com 에 회원가입하고 **콘솔**에 들어간다.
2. **결제수단을 먼저 등록한다.** 콘솔 → **과금정보 및 비용관리 → 결제 정보 관리** → 카드 등록. 결제수단이 없으면 API HUB에서 '서비스 이용이 불가능한 계정입니다'가 뜬다. 등록만으로 돈이 나가지는 않는다.
3. 콘솔 왼쪽 위 **Menu → All Services → Application Services → NAVER API HUB** 로 가서 **Subscription**에서 이용 신청을 한다.
4. 왼쪽 **Application** → **Application 등록** → 쓸 API로 **검색 > 뉴스**를 고르고, 이름(예: `urban-scale`)을 넣어 등록한다.
5. 등록한 Application을 누르고 **API 관리 → 인증 정보**에서 **Client ID**와 **Client Secret**을 확인한다.
6. API HUB는 종량제인데 지금은 한시적으로 무료다(유료로 바뀌면 미리 공지한다고 한다). 콘솔에서 **이용 한도와 한도 초과 통보**를 설정해 둔다. 이 사이트는 지난 수집 이후 기사만 받아서 하루 수백~천 번 안팎을 부른다.
7. 결제수단을 등록했는데도 같은 문구가 뜨면 네이버 클라우드 고객지원(1544-5876)에 문의한다.

Client Secret은 비밀번호와 같다. 채팅·코드·스크린샷에 붙이지 말고 2번의 GitHub Secrets에만 넣는다. 예전 개발자센터에서 받은 키가 있다면 그 키를 넣어도 된다(코드가 옛 주소로 알아서 넘어간다).

### 2. GitHub에 키 넣기

저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** 을 두 번 한다.

| Name | Secret |
|---|---|
| `NAVER_CLIENT_ID` | 1에서 받은 Client ID |
| `NAVER_CLIENT_SECRET` | 1에서 받은 Client Secret |

### 3. 사이트 켜기

저장소 → **Settings** → **Pages** → Source: **Deploy from a branch** → Branch: **main**, 폴더: **/docs** → **Save**.

### 4. 첫 수집 돌리기

저장소 → **Actions** → 왼쪽 **뉴스 수집** → **Run workflow**. 2~3분 뒤 초록 체크가 뜨면, 1~2분 더 기다렸다가 사이트에 들어간다. 이후로는 하루 여섯 번 저절로 돈다.

## 규칙 고치기

GitHub에서 `config/` 안의 파일을 열고 연필 아이콘으로 고친 뒤 **Commit changes** 를 누르면 된다.

- 분야·지역·언론사·이슈 기준을 고치면 저장된 오늘·어제 기사에 바로 다시 적용된다(Actions가 1~2분 돈다).
- 검색어를 고치면 다음 수집부터 반영된다.
- JSON 파일이라 쉼표와 따옴표가 하나라도 빠지면 수집이 실패한다. 고친 뒤 Actions 탭에 빨간 X가 뜨면 그 부분부터 본다.

## 내 컴퓨터에서 시험하기

```
python collect.py --source rss     # 키 없이 구글 뉴스 RSS로 제목만 모아 본다 (시험용)
python collect.py                  # .env 파일에 네이버 키를 넣었다면 실제와 같이 모은다
python collect.py --reprocess      # 사전만 고쳤을 때, 저장된 오늘·어제 기사에 다시 적용
python -m http.server 8000 --directory docs    # 그다음 http://localhost:8000 에 들어간다
```

`.env.example`을 복사해 `.env`로 이름을 바꾸고 키를 넣는다. `.env`는 GitHub에 올라가지 않는다.

## 알아 둘 점

- GitHub의 예약 실행은 정각보다 늦게 돌 때가 많다. 사이트 위쪽의 'OO:OO 업데이트'가 실제 수집 시각이다.
- 공개 저장소에서 60일 동안 아무 변화가 없으면 GitHub가 예약 실행을 멈춘다. 수집이 매일 커밋을 남기니 보통은 괜찮지만, 멈췄다면 Actions 탭에서 다시 켠다.
- 스크랩·메모와 관심어는 그 브라우저에만 저장된다. 스크랩 탭에서 백업 파일을 받아 둘 수 있다.
- 기사 파일은 `config/settings.json`의 '보관 일수'(기본 365일)가 지나면 지워진다.
