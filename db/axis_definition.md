# GEO 분석 축·값 정의서 (analysis 스키마)

## 공통 규칙

- 모든 축은 **단일 선택**이다. 한 대상(페이지·도메인·인용·응답)에 값 하나만 붙인다.
- 값이 여러 개 해당하면 각 축에 적힌 **우선순위** 순서로 첫 번째를 고른다. 어디에도 안 맞으면 `other`/`none`, 그래도 근거가 없으면 null.
- null은 "판정 불가·근거 없음"이다. 단 Answer Dominance의 null은 "답변에 경쟁사 없음"이다.
- 자동 축(Markup, Freshness, Length, Lang Match)은 규칙으로 계산하며 LLM이 판정하지 않는다.
- 인포시즈(InfoCZ)를 **자사(self)** 로 본다.

---

## A. 페이지 축

### 1. Format — 페이지의 문서 형식
페이지가 **주된 목적**에서 어떤 종류의 글인지. 발행 주체 특성이 뚜렷한 형식을 먼저 고른다.
우선순위: press_release·careers·faq·forum_qa·academic → how_to·comparison_list·case_study·interview → reference·tech_doc·report_whitepaper·product_page·company_intro·buyer_info·partner·news → blog_opinion → other

| 값 | 의미 | 판정 기준 |
|---|---|---|
| reference | 개념·용어를 설명하는 백과·위키·용어집 | "X란 무엇인가" 형태, 정의 중심, 중립 서술, 판매·절차 요소 없음 |
| tech_doc | 제품·API·아키텍처의 기술 문서·매뉴얼 | 코드·설정·스펙 포함, 개발자 대상 공식 문서 |
| academic | 논문·학위논문·연구 보고 | 초록·참고문헌·저자 소속 존재, 방법–결과 구조 |
| report_whitepaper | 시장·산업 보고서, 백서, 리서치 리포트 | 기관 발행, 조사·분석 결과 중심, 통계·도표 다수, 장문(PDF 포함) |
| how_to | 절차를 단계별로 안내 | 번호 단계, "방법·하는 법·가이드", 독자가 따라 하는 행위가 목적 |
| product_page | 제품·솔루션·서비스 소개·판매 | 기능·가격·도입 문의 버튼(CTA) 중심 |
| company_intro | 회사 소개·비전·연혁 | About 성격, 조직·소개 중심 |
| buyer_info | 구매·도입 검토자용 정보 | 도입 절차, 비용 구조, 선정 기준 등. 특정 제품 상세 설명은 product_page |
| careers | 채용 공고·채용 소개 | 직무·자격요건·지원 방법 |
| partner | 파트너·제휴·리셀러 프로그램 소개 | 파트너 모집·혜택, 제휴사 소개 |
| news | 언론의 뉴스 보도 | 제3자(매체)가 사건을 보도, 기자·일자 존재 |
| press_release | 기업·기관이 직접 낸 보도자료 | 자사 발행, 보도자료 문체·배포 문구 |
| case_study | 특정 고객·프로젝트의 도입 사례 | 고객명·문제·해결·성과 구조 |
| blog_opinion | 블로그·칼럼 등 필자 관점의 글 | 필자 해석·주장 중심, 위 형식에 안 맞는 일반 게시글 |
| comparison_list | 비교·순위·추천 목록 | "TOP N", "A vs B", "대안" 등 복수 대상을 나열·비교 |
| forum_qa | 커뮤니티 질의응답·게시판 | 이용자가 쓴 질문과 여러 답변 |
| faq | 운영 주체가 정리한 자주 묻는 질문 | 질문–답변 쌍을 운영 주체가 작성 |
| interview | 인터뷰·대담·강연 스크립트 | 발언자 중심, 질문–발언 구조 |
| other | 위 어디에도 안 맞음 | |

### 2. Genre — 글의 성격 (Format에서 파생)
Format에서 자동 파생되며 별도로 라벨링하지 않는다(`format_genre_map` 조인).

| 값 | 의미 | 해당 Format |
|---|---|---|
| descriptive | 정보를 설명·기술 | reference, tech_doc, academic, report_whitepaper |
| instructional | 행동·절차를 안내 | how_to |
| persuasive | 구매·지원·참여를 유도 | product_page, company_intro, buyer_info, careers, partner |
| narrative | 사건·사례를 서술 | news, press_release, case_study |
| opinion | 필자의 관점·평가 | blog_opinion, comparison_list |
| discussion | 다수 참여자의 문답 | forum_qa, faq |
| spoken | 구술 기반 | interview |
| other | 기타 | other |

### 3. Topic — 주제
본문의 **주된 주제 하나**. 제목 → 첫 문단 → 반복 키워드 순으로 본다. 여러 개면 제목에 나온 주제가 우선. `topic_keywords`에 한글 키워드 3~5개를 함께 적는다.

| 값 | 의미 |
|---|---|
| ontology | 온톨로지·지식그래프 모델링 |
| graphrag | GraphRAG·RAG·그래프 기반 검색 |
| hallucination | 환각(hallucination) 문제와 완화 |
| ai_security | AI·LLM 보안(프롬프트 인젝션, 모델 보안) |
| data_security | 데이터 보안·유출 방지·접근 통제 |
| insider_threat | 내부자 위협·내부 정보 유출 |
| ot_security | OT·산업제어(ICS) 보안 |
| digital_twin | 디지털 트윈 |
| on_prem_ai | 온프레미스·폐쇄망·sLLM 구축 |
| ai_agent | AI 에이전트·업무 자동화 |
| regulation | AI 규제·법·컴플라이언스 |
| ax_consulting | AX·DX 전환, 도입 전략·컨설팅 |
| cost_roi | 비용·ROI·TCO |
| gov_support | 정부지원사업·바우처·보조금 |
| palantir_alt | 팔란티어 및 대안·유사 플랫폼 |
| doc_ai | 문서 AI·OCR·문서 자동화 |
| brand | 특정 기업·브랜드 자체가 주제. 기술 주제와 겹치면 기술 주제 우선 |
| other | 위에 없음 |

### 4. Industry — 다루는 업종
페이지가 **명시적으로 다루는 업종 하나**. 여러 업종을 나열하거나 업종이 없으면 `general`. case_study는 고객사 업종 기준. (쿼리의 industry가 아니라 페이지 기준)

| 값 | 의미 |
|---|---|
| general | 업종 특정 없음·범용 |
| manufacturing | 제조(공장·생산·MES) |
| defense | 방산·국방 |
| finance | 은행·보험·증권 |
| energy_plant | 에너지·발전·플랜트 |
| semiconductor | 반도체 |
| public_sector | 공공·지자체 |
| healthcare | 의료·바이오 |
| telecom | 통신 |
| automotive | 자동차·모빌리티 |
| retail_logistics | 유통·물류 |
| construction_shipbuilding | 건설·조선 |

### 5. Persona — 대상 독자
글이 **누구를 위해 쓰였는지**. 용어 수준과 내용 깊이로 판정한다.

| 값 | 의미 | 판정 기준 |
|---|---|---|
| executive | 경영진·의사결정자 | 전략·비용·리스크 관점, 요약 중심 |
| practitioner | 실무·기술 담당자 | 구현·설정·코드·기술 용어 다수 |
| business | 사업·기획·구매 담당자 | 기능·효과·도입 검토 중심, 기술 깊이는 얕음 |

### 6. Tone — 어조
글 전체의 **문체 성격**.

| 값 | 의미 | 판정 기준 |
|---|---|---|
| neutral_info | 중립적 정보 전달 | 객관 서술, 홍보·주관 표현 거의 없음 |
| expert_analysis | 전문가 분석 | 근거에 기반한 해석·평가·시사점 |
| promotional | 홍보·판매 | 자사 우수성 강조, 최상급, 행동 유도(CTA) |
| opinion | 의견·주장 | 필자 주관, 1인칭, 주장 중심 |

### 7. Evidence — 근거 유형
주장을 뒷받침하는 **가장 비중 큰 근거**. 우선순위: stats → case → quote → source → authority → none

| 값 | 의미 | 판정 기준 |
|---|---|---|
| stats | 수치·통계·조사 결과 | 구체 수치, 비율, 조사 결과 제시 |
| case | 구체적 사례 | 고객·프로젝트 사례로 뒷받침 |
| quote | 전문가·고객 발언 인용 | 인용 부호와 발언자 명시 |
| source | 출처·참고문헌 명시 | 논문·보고서·링크 인용 |
| authority | 권위 내세우기 | 인증·수상·기관 소속·자격을 근거로 제시 |
| none | 근거 없음 | 위에 해당 없음 |

### 8. Markup — 구조화 정도 (자동)
`pages`의 크롤링 컬럼으로 계산한다.

| 값 | 판정 기준 |
|---|---|
| rich | schema_types ≥ 1개 **그리고** 제목(h) ≥ 3개 **그리고** (이미지 없음 **또는** alt 비율 ≥ 0.8) |
| basic | schema_types ≥ 1개 **또는** 제목(h) ≥ 1개 |
| none | 위 둘 다 아님 |
| null | 본문(`pages.text`)이 없음 |

### 9. Visual — 대표 시각 요소
본문에서 **가장 비중 큰 시각 요소**. 동률이면 video → infographic → diagram → table → screenshot → image → text_only 순.

| 값 | 의미 |
|---|---|
| text_only | 텍스트만 있음 |
| image | 사진·일러스트 |
| table | 표 중심 |
| diagram | 도식·플로차트·아키텍처 그림 |
| infographic | 수치를 시각화한 인포그래픽 |
| video | 영상 임베드 |
| screenshot | 화면 캡처 |

### 10. Language — 본문 언어
`pages.lang`이 있으면 그 값(앞 2글자)을 쓰고, 비어 있을 때만 본문의 **주된 문자·언어**로 판정한다.

| 값 | 의미 |
|---|---|
| ko / en / ja / zh | 한국어 / 영어 / 일본어 / 중국어 |
| other | 그 외 |

### 11. Freshness — 최신성 (자동)
기준일 = `modified_at`, 없으면 `published_at`. 비교 시점은 `fetched_at`.

| 값 | 판정 기준 |
|---|---|
| under_3mo | 90일 이내 |
| 3_6mo | 91~180일 |
| 6_12mo | 181~365일 |
| over_12mo | 365일 초과 |
| unknown | 기준일 둘 다 없음 |

### 12. Length — 글 길이 (자동)
`pages.word_count` 숫자 그대로 저장한다. 구간 나누기는 분석 단계에서 한다.

### 13. Entity Focus — 주로 다루는 주체
페이지가 **중심으로 다루는 기업·주체**.

| 값 | 의미 | 판정 기준 |
|---|---|---|
| self | 인포시즈 | 인포시즈·인포시즈 제품이 중심 |
| competitor | 경쟁사 | 동일 시장 사업자(예: 팔란티어 등)가 중심 |
| ecosystem | 협력·인프라 | 클라우드·파트너·기술 플랫폼·협회가 중심 |
| customer | 고객사 | 고객사(사례 주인공)가 중심 |
| none | 특정 주체 없음 | 일반 정보·개념 설명 |

### 14. Self Authored — 자사 작성 여부
인포시즈가 **직접 발행·작성한** 페이지면 true(자사 도메인·자사 공식 계정), 아니면 false.

### 15. Publisher — 발행 주체 유형

| 값 | 의미 |
|---|---|
| media | 언론·매체 |
| brand | 기업·제품 공식 채널 |
| community | 커뮤니티·개인 블로그·포럼·SNS 등 이용자 생성 |
| government | 정부·공공기관 |
| academic | 대학·연구기관·학술지 |
| other | 위에 안 맞음 |

기존 `page_labels.publisher_kind` 값이 있으면 그 값을 우선 쓰고, 비었을 때만 위 기준으로 판정한다.

---

## B. 도메인 축

### 16. Brand Role — 도메인의 브랜드 역할
`entities`(role, domains)로 먼저 매핑하고, 매핑이 안 되는 도메인만 이 기준으로 판정한다.

| 값 | 의미 | 판정 기준 |
|---|---|---|
| self | 자사 | 인포시즈 도메인 |
| competitor | 경쟁사 | 동일 시장 사업자 |
| ecosystem | 협력·인프라 | 클라우드·파트너·플랫폼·협회 |
| customer | 고객사 | 도입 고객사 |
| none | 해당 없음 | 언론·정부·일반 사이트 등 |

### 17. Authority — 도메인 공신력

| 값 | 의미 | 판정 기준 |
|---|---|---|
| high | 공신력·영향력 높음 | 정부·대학·주요 언론·대기업 공식 도메인 |
| mid | 보통 | 업계 전문 매체·중견기업·알려진 플랫폼 |
| low | 낮음 | 개인 블로그·신생·출처 불명·스팸성 |

---

## C. 인용 축

### 18. Query Match — 질의 적합도 (0~1)
인용된 페이지가 질의와 얼마나 맞는지. `citations.query_overlap`이 있으면 그 값을 쓰고, 비었을 때만 질의 핵심어가 인용 페이지 제목·본문에 포함된 비율로 계산해 저장한다. 구간 나누기는 분석 단계에서 한다.

### 19. Passage Type — 인용된 구절의 형태
우선순위: table → list → stats → quote → definition → other

| 값 | 의미 | 판정 기준 |
|---|---|---|
| definition | 정의·설명 문장 | "X는 ~이다" 형태 |
| stats | 수치·통계 | 구체 수치·비율 포함 |
| table | 표 | 표 형태 데이터 |
| list | 목록 | 불릿·번호 항목 |
| quote | 인용구 | 발언자·인용 부호 |
| other | 그 외 | |

### 20. Lang Match — 언어 일치 (자동)

| 값 | 판정 기준 |
|---|---|
| match | 페이지 언어(`pages.lang` 앞 2글자)가 질의 언어(`queries.language`)와 같음 |
| mismatch | 다름 |
| null | 페이지 언어 또는 질의 언어를 알 수 없음 |

---

## D. 응답 축

### 21. Engine — 답변을 낸 엔진
`responses.engine`을 그대로 쓴다. 분석 대상은 chatgpt, gemini, claude, naver_ai_briefing, google_aio 5개이며 Perplexity는 제외한다.

### 22. Answer Form — 답변 형식
**답변의 주된 구조**. 기존 `response_labels.answer_form`을 조인해 쓴다(재계산하지 않음).

| 값 | 의미 |
|---|---|
| explanation | 개념·원리 설명 |
| how_to | 절차 안내 |
| comparison | 둘 이상을 비교 |
| recommendation | 특정 대상을 추천하며 선택을 유도 |
| list | 항목을 나열(후보·순위 목록, 추천 이유 없이) |
| other | 기타 |

### 23. Overall Sentiment — 답변 전체 논조
답변 전체의 감성. 기존 `response_labels.overall_sentiment`를 조인해 쓴다(재계산하지 않음).

| 값 | 의미 |
|---|---|
| positive | 긍정적 논조 |
| neutral | 중립 |
| negative | 부정적 논조 |

### 24. Answer Dominance — 답변 내 인포시즈의 우위
**한 응답 안에서 인포시즈(self)가 경쟁사 대비 얼마나 우세하게 다뤄지는가**. 응답 1건당 값 1개이며 `analysis.response_label`에 저장한다.

| 값 | 의미 | 어떤 답변인가 |
|---|---|---|
| dominant | 인포시즈가 우세 | 경쟁사보다 먼저 나오고, 상위 순위이며, 더 길게 다뤄지고, 더 긍정적·추천 입장으로 서술됨 |
| neutral | 비슷한 비중 | 인포시즈와 경쟁사가 대등하게 나열되거나 우열이 갈림 |
| recessive | 인포시즈가 열세 | 경쟁사만 있고 인포시즈가 없거나, 둘 다 있지만 인포시즈가 뒤·하위·짧게·덜 긍정적으로 다뤄짐 |
| null | 경쟁사 없음 | 답변에 경쟁사가 하나도 없음(인포시즈 유무는 상관없음). 비교 대상이 없어 우위를 판정할 수 없다 |

**판정 순서**
1. 답변에 경쟁사가 없으면 → null. (여기서 끝)
2. 경쟁사만 있고 인포시즈가 없으면 → recessive. (여기서 끝)
3. 둘 다 있으면 아래 4개 신호를 인포시즈 vs 가장 두드러진 경쟁사로 비교한다.

| 신호 | 인포시즈 우세(+1) | 인포시즈 열세(−1) | 동률·판단 불가(0) |
|---|---|---|---|
| 등장 순서 | 먼저 등장 | 나중에 등장 | 같은 문장·항목 |
| 목록 순위 | 더 높은 순위 | 더 낮은 순위 | 순위 없는 서술 |
| 언급 분량 | 문장·글자 수가 더 많음 | 더 적음 | 비슷함 |
| 감성·입장 | 더 긍정·추천 | 더 부정·비추천 | 같거나 없음 |

4. 합계 ≥ +2 → dominant, ≤ −2 → recessive, 그 사이 → neutral.

※ 3~4번의 점수 규칙은 판정을 일관되게 하기 위해 제안한 운영 규칙이다. 팀에서 임계값(±2)을 바꿔도 된다.