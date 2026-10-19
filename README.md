# gh-x-infocz

인포시즈 GEO 측정 프로젝트 (그로스해커스 26.4Q-S). 잠재 고객이 **비브랜드 질문**을 했을 때 AI 답변(ChatGPT·Perplexity·Gemini·Google AI Overviews·네이버 AI 브리핑·Claude)에 인포시즈가 언급·인용되는지 측정한다.

에이전트는 작업 전에 이 문서와 다음 두 파일을 읽는다.

- [`CONTEXT.md`](CONTEXT.md): 프로젝트 목표, 인포시즈 팩트, 경쟁 지형, 미결 사항
- [`axes.json`](axes.json): 쿼리 분류 5축, 판별 규칙, 샘플링, 알려진 축 간 겹침

## 저장소 구조

```
CONTEXT.md              프로젝트 컨텍스트 (사람이 관리)
axes.json               축 정의 · 판별 규칙 · 샘플링 (DB enum 코드와 동일)
db/schema.sql           테이블·enum 정의 (Supabase Postgres)
db/seed_reference.sql   축 라벨 · 엔진 · 엔티티 레지스트리 초기값
scripts/db.py           DB 연결 헬퍼 — 모든 코드는 이걸로 연결
scripts/setup_db.py     빈 DB에 스키마 + 참조 데이터 생성
scripts/import_queries.py   TSV → queries (upsert)
scripts/export_queries.py   queries → TSV (stdout, 백업·검토용)
scripts/export_cookies.py   (화면 있는 PC) 로그인 → data/cookies/<engine>.json
scripts/import_cookies.sh   (서버) 쿠키 JSON → 크롤러 프로필
crawler/                웹 UI 기반 측정 크롤러 (Playwright) → responses 등에 저장
data/                   크롤러 로그인 프로필·증거 파일 (gitignore)
```

**쿼리의 원본은 DB다.** 저장소에는 쿼리 파일이 없다. 필요하면 `export_queries.py`로 덤프한다.

## 환경 설정

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium      # 크롤러를 돌릴 때만
```

`.env` (커밋 금지): `connection_string=postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres`

- `db.<ref>.supabase.co`는 **IPv6 전용**이라 IPv4 망에서는 붙지 않는다. `scripts/db.py`의 `connect()`가 같은 계정으로 IPv4 세션 풀러(`aws-0-ap-southeast-2.pooler.supabase.com`, 사용자 `postgres.<ref>`)에 연결한다. 다른 풀러를 쓰려면 `.env`에 `pooler_host=`를 넣는다.
- `psql`이 없을 수 있다. Python으로 질의한다.

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from db import connect
print(connect().execute('select count(*) from queries').fetchone())"
```

## 데이터 모델

```
queries (seed · control · fanout)
  ├─ query_entities ─ entities          쿼리에 나오는 고유명사
  └─ responses (엔진 × 회차 × 반복)
       ├─ response_fanouts ─ queries(origin='fanout')   AI가 실제로 날린 하위 검색어
       ├─ citations ─ entities           인용 URL (domain, is_infocz 자동 계산)
       └─ response_mentions ─ entities   답변에 언급된 회사와 순위
engines, axis_labels                     참조 테이블
```

| 테이블 | 핵심 열 | 메모 |
|---|---|---|
| `queries` | `query_text`, `language`, `origin`, 5축(`market`, `industry`, `awareness_stage`, `anchor_type`, `constraint_type`), `template`, `is_active`, `review_note`, `parent_query_id` | `origin='seed'`이면 5축 필수. `'fanout'`이면 `parent_query_id` 필수. `(query_text, parent_query_id)` 유일 |
| `entities` | `name`, `aliases[]`, `role`(self/competitor/ecosystem/customer), `markets[]`, `domains[]` | 고유명사 앵커 판별, 답변 언급 매칭, 인용 도메인 매칭에 공통으로 쓴다 |
| `responses` | `query_id`, `engine`, `model`, `batch`, `run_no`, `settings`, `response_text`, `raw`, `infocz_mentioned`, `infocz_rank`, `accuracy` | `(batch, query_id, engine, model, run_no)` 유일. `accuracy`에 Infosys 혼동은 `'confused_infosys'` |
| `response_fanouts` | `response_id`, `query_id`, `position` | |
| `citations` | `response_id`, `url`, `position`, `snippet`, `fanout_query_id`, `entity_id` | `domain`, `is_infocz`는 생성 열이라 쓰지 않는다 |
| `response_mentions` | `response_id`, `entity_id`, `rank`, `context` | `rank`는 self·competitor만 매긴다 |

- enum 값과 한국어 라벨은 `axis_labels` 테이블과 `axes.json`에 있다.
- 모든 테이블에 RLS가 켜져 있고 정책은 없다. anon 키로는 접근할 수 없고, `postgres` 계정(=`connect()`)은 RLS를 우회한다.

## 쿼리 세트 (현재 상태)

| origin | 개수 | 설명 |
|---|---|---|
| `seed` | 1,299 | 3 시장 × 5 산업 × 4 인지 × 4 고유명사 × 5 운영조건 = **1,200개 조합 전부** |
| `control` | 7 | "인포시즈는 어떤 회사야?" 같은 브랜드 직접 질의. 엔티티 정확도·Infosys 혼동 확인용, 노출률 분모에서 제외 |
| `fanout` | 0 | 측정하면서 쌓인다 |

- **경쟁사 변형**: 초기 competitor 슬롯 47개는 같은 문장 틀에 경쟁사만 바꾼 변형 146개다. `template`이 같으면 같은 슬롯이므로, 분석할 때 슬롯 단위로 묶거나 경쟁사 간 비교에 쓴다.
- **미국 시장 × 고객사 앵커**: 국내 고객사를 미국 구매자 관점으로 쓴다 (예: KB금융의 미국 지점, 미국에서 알려진 두산에너빌리티 원전 기자재, 한화큐셀 미국 태양광).
- `is_active=false`인 쿼리는 크롤러가 가져가지 않는다. 지금은 0개다.
- 한 슬롯(5축 조합)에 쿼리가 여러 개일 수 있다. 슬롯별 집계는 5축으로 `group by`한다.

```sql
-- 슬롯별 활성 쿼리 수
select market, industry, awareness_stage, anchor_type, constraint_type, count(*)
from queries where origin = 'seed' and is_active
group by 1,2,3,4,5;
```

## 작업별 안내

### 1. 측정 실행 (크롤러)

```bash
.venv/bin/python -m crawler login chatgpt          # 최초 1회, 헤드풀 브라우저에서 직접 로그인
.venv/bin/python -m crawler run chatgpt --batch baseline-2026-10 --runs 3 --market kr_ax --limit 10
```

- 구현된 채널: `chatgpt`(로그인 선택), `claude`(로그인 필수), `gemini`(비로그인 전용), `google_aio`·`naver_ai_briefing`(비로그인 전용, 검색 결과 페이지의 AI 개요·AI 브리핑).
- Google AI 개요·네이버 AI 브리핑은 쿼리마다 뜨지 않을 수 있다(같은 쿼리도 실행마다 다름). 안 뜨면 `response_text`가 비고 `notes='no_ai_answer'`로 기록한다 (노출률 분모). Google 인용 링크는 `google.com/goto` 리디렉트라 302 `Location`으로 실제 URL을 얻는다.
- **답변 맞춤화 금지.** 로그인 계정은 ChatGPT 임시 채팅·Claude 시크릿 채팅으로 묻고, 세션 시작 때 계정 설정을 확인해 하나라도 켜져 있으면 `PersonalizationError`로 그 채널을 중단한다. 확인 결과는 `responses.settings.personalization`에 남는다.
  - ChatGPT: 저장된 메모리 참조(`sunshine`)·채팅 기록 참조(`moonshine`) 꺼짐, 맞춤 지침 비어 있음, 저장된 메모리 0
  - Claude: 메모리 생성·과거 대화 검색(`enabled_saffron*`, `enabled_melange`) 꺼짐, 개인 선호 지침·저장된 메모리 비어 있음
  - Gemini: Google 세션은 서버로 옮기면 무효화되고 맞춤화를 확인할 방법도 없어 비로그인으로만 측정한다. 로그인 상태가 감지되면 중단
- claude.ai는 headless 브라우저를 Cloudflare로 막는다. 서버에서는 `xvfb-run -a .venv/bin/python -m crawler run claude …`처럼 헤드풀로 실행한다.
- 화면 없는 서버에서는 헤드풀 로그인 대신 화면 있는 PC에서 로그인해 쿠키를 옮긴다 (`data/cookies/`는 커밋 금지, gitignore됨):
  ```bash
  python scripts/export_cookies.py claude chatgpt            # PC: 엔진마다 Chrome이 뜨면 로그인 → Enter → data/cookies/<engine>.json
  scp data/cookies/*.json <server>:gh-x-infocz/data/cookies/
  scripts/import_cookies.sh                                  # 서버: 프로필에 넣고 로그인 확인 (= python -m crawler login <engine> --cookies …)
  ```
  Cookie-Editor 확장으로 내보낸 JSON 배열도 `--cookies`에 그대로 넣을 수 있다. 세션이 만료되면 다시 내보낸다. 헤드풀 실행이 필요하면 `xvfb-run -a`로 감싼다.
- 엔진별로 얻을 수 있는 것: Google·네이버는 fanout·모델명이 없다. ChatGPT 비로그인은 인용은 있지만 하위 검색어(fanout)·모델명이 노출되지 않는다. ChatGPT 로그인은 2026-10부터 스트림이 끝난 요청으로 잡히지 않아 답변 뒤 대화 JSON(`/backend-api/conversation/<id>`)에서 모델·인용(답변의 `turnNsearchM` ref → `search_result_groups`)을 읽는다. 이 JSON에는 하위 검색어가 없어 fanout은 비어 있다. Gemini는 fanout이 없고 `model`에 모드 이름(예: Flash-Lite)을 남긴다. Claude는 검색어·인용·모델을 스트림에서 모두 얻는다.
- `--batch`는 측정 회차 라벨이다. 같은 `(batch, query, engine, run_no)`는 건너뛰므로 중단 후 다시 실행해도 된다.
- 채널(엔진별 크롤러)은 `crawler/channels/`에 `BaseCrawler`를 상속해 `submit` / `wait_until_done` / `extract`만 구현한다. `engine` 값은 `engines.code`와 같아야 한다. 저장·엔티티 매칭·증거 보관은 `BaseCrawler`가 맡는다.
- 미국 시장(영문) 측정은 locale만 바뀌고 IP 지역은 그대로다. VPN을 쓰면 `settings`에 남긴다.
- **병렬 실행**: `--workers N`이면 쿼리를 N개 프로세스로 나눠 돈다 (Playwright sync API는 스레드 공유가 안 된다). 워커 1번부터는 `data/profiles/<engine>-wN`에 프로필 복사본을 만들어 쓴다. 다시 로그인했으면 `profiles/<engine>-w*`를 지워야 새 세션이 복사된다. 로그인 채널(ChatGPT·Claude)은 워커가 같은 계정 사용량 한도를 나눠 쓰므로 2개 정도로 둔다.
- **증거 파일**: 기본은 실행마다 스크린샷(JPEG)·페이지 HTML(gzip)을 `data/artifacts/`에 남긴다. 응답·인용·원문은 DB에 있으므로 대량 측정은 `--no-artifacts`로 돌린다 (실패한 실행만 남는다). 디스크가 10GB라 전량 측정에 증거 파일을 남기면 버티지 못한다.
- **계정 여러 개**: `--account acct1`이면 프로필은 `data/accounts/acct1/profiles/<engine>`이다 (gitignore됨) (`crawler login claude --account acct1 --cookies data/cookies/claude-acct1.json`). 같은 배치를 두 계정으로 동시에 돌릴 때 하나는 `--reverse`로 반대쪽 끝부터 돈다. 같은 행을 동시에 저장하면 늦은 쪽은 건너뛴다. `settings.account`에 계정 이름이 남는다.
- **Claude 사용량 상한**: `--usage-cap 50`이면 실행마다 계정 사용량(5시간·주간 %)을 확인해 넘으면 그 채널을 멈춘다 (러너가 1시간 뒤 다시 시도).
- **프록시**: `--proxy socks5://127.0.0.1:1080`. 서버 IP가 Google에서 `/sorry` 차단을 받으면 화면 있는 PC(맥북)를 출구로 쓴다. PC에서 역방향 동적 포워딩을 열면 서버의 127.0.0.1:1080에 그 PC를 거쳐 나가는 SOCKS 프록시가 생긴다:
  ```bash
  caffeinate -is ssh -N -R 1080 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes collector@<server>   # PC (OpenSSH 7.6+)
  .venv/bin/python -m crawler run google_aio --batch baseline-2026-10 --proxy socks5://127.0.0.1:1080       # 서버
  ```
  세션마다 실제 나가는 IP를 확인해 `settings.proxy`·`settings.exit_ip`에 남긴다. 터널이 끊기면 Chromium은 직접 연결로 돌아가지 않고 실패하며, 5번 연속 실패하면 채널을 멈춘다.

### 2. 크롤러 밖에서 얻은 결과 기록 (API·수동)

`crawler.store.Store.save_response(ResponseRecord(...))`를 쓰면 응답·인용·fanout·언급이 한 트랜잭션으로 저장되고, fanout 쿼리도 `queries`에 upsert된다. 직접 SQL로 넣을 때도 같은 순서를 따른다: `responses` → fanout `queries`(`origin='fanout'`, `parent_query_id`) → `response_fanouts` → `citations` → `response_mentions`.

### 3. 쿼리 추가·수정

새 쿼리는 `axes.json`의 판별 규칙을 지켜야 MECE가 유지된다. 지난 리뷰에서 실제로 걸린 규칙:

- **산업**: 산업어나 산업 고유 대상이 문장에 있어야 한다. 고유명사만으로 산업을 정하지 않는다 (예: "SK하이닉스 같은 회사" ✗ → "SK하이닉스 같은 반도체 회사" ✓). 영문은 `power plant`와 `factory`로 구분한다.
- **시장**: 국내 AX와 국내 보안은 같은 한국어라 목적 단서가 필요하다. Neo4j·AWS 같은 중립 도구만 나오면 "Neo4j 지식그래프" / "Neo4j 보안 그래프"처럼 목적을 붙인다.
- **운영조건**: `none`이면 폐쇄망·수치·법규·연동 단서가 없어야 하고, 그 밖의 값이면 해당 단서가 있어야 한다.
- **고유명사**: `query_entities`에 연결한 엔티티의 역할로 정한다. 여러 개면 competitor > ecosystem > customer, 없으면 general.
- **문장**: 한 문장, 국문 약 20~50자 / 영문 약 8~20단어. 실제 사용자가 채팅창에 치는 길이로 쓴다.

대량 추가는 TSV로 한다 (`{c}` 템플릿 문법은 `import_queries.py` 독스트링 참고).

```bash
.venv/bin/python scripts/import_queries.py new_queries.tsv
```

- **잘못된 쿼리는 삭제하고, 같은 슬롯(5축 조합)에 대체 쿼리를 넣어 1,200개 조합을 유지한다.** 단, 응답이 달린 쿼리는 삭제하지 않는다. 그런 쿼리는 `is_active=false`로 바꾸고 `review_note`에 사유를 적은 뒤 대체 쿼리를 추가한다.
- 문구를 고치면 이전 응답과 비교할 수 없게 된다. 측정이 시작된 뒤에는 응답이 달린 쿼리를 고치지 말고, 위 방식으로 대체한다.

### 4. 엔티티 추가

`entities`에 insert하고 같은 행을 `db/seed_reference.sql`에도 추가한다 (빈 DB 재생성용). 인용 매칭이 틀어지지 않도록 `domains`에는 그 회사 소유 도메인만 넣는다. `learn.microsoft.com` 같은 공용 도메인은 넣지 않는다.

### 5. 백업·초기화

```bash
.venv/bin/python scripts/export_queries.py > backup.tsv                 # 쿼리 백업 (import_queries.py로 복원)
.venv/bin/python scripts/setup_db.py --reset --i-understand-data-loss   # 전부 삭제 후 재생성
```

`--reset`은 쿼리·응답·인용을 모두 지운다. 사용자 확인 없이 실행하지 않는다.

## 분석 예시

```sql
-- 축별 인포시즈 언급률 (control 제외, 활성 쿼리만)
select q.awareness_stage, r.engine, avg(r.infocz_mentioned::int) as mention_rate, count(*)
from responses r join queries q on q.id = r.query_id
where q.origin = 'seed' and q.is_active and r.infocz_mentioned is not null
group by 1, 2 order by 1, 2;

-- 많이 인용된 도메인 (인포시즈 여부 포함)
select domain, bool_or(is_infocz) as infocz, count(*)
from citations group by domain order by 3 desc limit 30;
```

## 주의

- "인포시즈/Infocz"는 Infosys와 자주 혼동된다. 언급 판정은 `entities.aliases`로 하고, 정확도는 `responses.accuracy`에 기록한다.
- CONTEXT.md의 미결 사항(Neo4j "국내 유일" 파트너, Gartner 관계)은 확인 전까지 사실로 쓰지 않는다.
- 이해관계를 숨긴 후기, 위키 직접 편집, 자기 1위 리포트 자동 생성은 하지 않는다.
