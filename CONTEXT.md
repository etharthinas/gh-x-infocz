# 인포시즈 GEO 프로젝트 — 컨텍스트

> 그로스해커스(GH) 26.4Q-S 사이드 프로젝트 · 2026-10-13 기준

## 목표

- 잠재 고객이 **비브랜드 쿼리**로 AI에 질문할 때 답변·인용에 인포시즈가 노출되게 한다.
- "인포시즈"를 직접 묻는 쿼리는 측정 본체에서 제외 (엔티티 정확도 확인용 통제 세트만 별도).
- 시장 분리: AI 보안 이상탐지(GOS)는 **미국, 영문** / 국내 사업 전반(AX + 보안)은 **국문**.
- 학회 역할: 측정·실험 중심 (콘텐츠 초안까지인지는 미확정).

## 인포시즈

- 온톨로지 기반 엔터프라이즈 AI 기업. 2018년부터 그래프 기술 독자 개발.
- **AXIOM**: 시맨틱 레이어 (온톨로지·지식그래프·워크플로), 환각 제어, 근거 추적
- **DELTAflow**: P&ID 도면 인식 → 지식그래프, HAZOP 초안, 다중 도면 연결 추적
- **GOS**: 그래프 기반 AI 보안 이상탐지 (Provenance Graph, APT·내부자 위협). 2026-10-07 출시, 국내 반도체 대기업 전사 공급. 미탐률 0%, 정상 로그 99.998% 필터링
- 고객: 삼성전자, SK하이닉스, KB금융, 두산에너빌리티, 한화 (사이트에는 익명 표기). 공공: 한국전력 'AI 버추얼 그리드' 실증 (2026-08)
- 웹 자산: infocz.co.kr (canonical), blog.infocz.ai, career.infocz.ai/.co.kr, LinkedIn infoczx
- 표기 흔들림: Infosys 이름 충돌, AXIOM/Axiom/액시엄, DELTAflow/DeltaFlow/델타플로우

## 핵심 진단

1. 브랜드 질의는 강하고 비브랜드 질의("팔란티어 대안 국내", "P&ID 도면 인식 AI" 등)에서는 부재.
2. 인용할 재료는 있으나 비교 페이지·FAQ·유스케이스·영문 페이지·제품 스펙이 없음.
3. 업계 담론이 "온톨로지란?"에서 "PoC가 왜 멈추나 / 에이전트의 Context Layer"로 이동.
4. 미국 보안: Gartner가 UEBA를 Insider Risk Management(IRM)로 재분류 → 영문 쿼리에 "insider risk management" 어휘 포함.
5. 국내 보안 경쟁사는 모두 DLP·로그 기반 행위 분석 → **그래프 기반 인과 분석**이 GOS 차별점.

## 측정 설계

- 축 정의·판별 규칙·샘플링: [`axes.json`](axes.json)
- 엔티티 레지스트리 (경쟁사·생태계·고객): [`db/seed_reference.sql`](db/seed_reference.sql)
- 쿼리: Supabase `queries` 테이블이 원본 (1,200개 축 조합 전부, `scripts/export_queries.py`로 TSV 덤프)
- 대상 엔진: ChatGPT, Perplexity, Gemini, 네이버, Google (엔진별 3~5회 반복)
- 기록 항목: 언급 여부, 순위, 인용 URL, 서술 정확도, 축 태그
- 분석: 축별 주효과 + 2요인 교호작용
