# 레퍼런스 그래프 — 유사 프로젝트·지식·사례 수집

> 킥오프 1부와 2부 사이 레퍼런스 수집 단계 산출물 (2026-08-16). 2단계로 수집:
> **1차 눈덩이 탐색** (시드 3갈래: 발행 구현체 / GEO 지식 / 메이트 사례 — 아래 "노드 목록")
> **2차 매트릭스 병렬 스윕** (기능 5 × 지역 4 = 20셀 병렬 검색 → 검증 에이전트 판정 — 아래 "매트릭스 스윕 결과").
> 스윕 원시 데이터 전체(129건)는 [raw/reference-sweep-2026-08-16.json](raw/reference-sweep-2026-08-16.json) — 불변 보존.

## 결론 먼저 — 이 그래프가 프로젝트에 주는 것

| 발견 | 프로젝트 반영 |
|------|------|
| 인용수는 공식 확인 가능 (2026-06-04부터 서비스 프로필, 2026-01부터 누적 집계) | 측정 루프의 1차 지표 = 내 프로필 인용수. AI 브리핑 스크래핑은 경쟁 관찰용으로 축소 |
| AI 활용 동의를 켜야 인용 대상 | **선결 과제**: 블로그 설정에서 AI 활용 설정 ON |
| "AI 따발총"(일 100건 AI 글) 제재 강화 방침 | 완전 자동이어도 일 1~2건 + 품질 게이트는 생존 조건 |
| 커넥터스(인용 1위): 틈새 1차 정보가 레드오션 이김 | 주제 선정 로직 = "웹에 답이 부족한 질문" 탐지 방향 |
| 네이버 블로그는 마크다운·HTML 모드 없음 (스마트에디터 ONE만) | 발행 자동화는 에디터 UI 조작 전제. tokbiseo의 마크다운 투입 방식 이식 불가 |
| 2025-07부터 봇 행위(공감·댓글·서이추) 강력 차단 | 발행 외 상호작용 자동화는 하지 않는다 (계정 제재 사례 있음) |

## 노드 목록

### ① 발행 구현체 (코드)

- **[space-cap/naver-blog-mcp](https://github.com/space-cap/naver-blog-mcp)** — Python 3.13 + Playwright 1.55 + MCP.
  네이버 로그인 자동화(세션 저장/재사용), 스마트에디터 ONE 글 작성, 이미지 업로드(7포맷),
  tenacity 재시도, DOM 셀렉터 모듈(`automation/selectors.py`), 카테고리 조회. MIT. 미완성(~52%)이지만
  **로그인·에디터 셀렉터·세션 관리 코드가 최대 재사용 후보**. 마크다운 지원 불가를 문서로 확인해줌.
- **[lushlife99/automation-naver-blog](https://github.com/lushlife99/automation-naver-blog)** — Selenium, 공감·댓글·서이추 자동화.
  우리 목적 아님 + 이 계열이 바로 차단·제재 대상 → **반면교사 노드** (발행 외 상호작용 자동화 금지 근거).
- **[ikramhasan/AutoBlog-AI-Blog-Generator](https://github.com/ikramhasan/AutoBlog-AI-Blog-Generator)** — 로컬 LLM 대량 블로그 생성.
  "따발총" 계열 — 네이버에선 제재 대상 패턴이므로 생성 볼륨 참고만.
- 사내 자산: `kakaotalk_parser/tokbiseo-server` — LangGraph 작문 파이프라인(Filter→…→Writer),
  Playwright 발행 + storage_state 세션 3단 방어. **파이프라인 골격과 세션 패턴 이식**, 단 에디터 조작부는 재작성.

### ② GEO / AI 인용 최적화 (지식)

- **[리드젠랩 — 네이버 AI 브리핑 노출: C-rank·AEO 가이드](https://blog.lead-gen.team/naver-ai-briefing-seo-optimal-strategy)** —
  인용 잘 되는 형태: H2/H3 위계, 목록·표·넘버링, FAQ·"방법/이유/차이점" 구조, 발행일·최신성 명시,
  사실 기반 구체 정보. 출처 권위 = C-rank (한 주제 지속 생산). → **글 템플릿·품질 게이트의 채점 기준 재료**.
- [TBWA GEO 지침서](https://seo.tbwakorea.com/blog/tbwa-geo-guide/), [오픈타임 GEO 7단계](https://ai.idearabbit.co.kr/ai-search-optimization-guide/) — 일반 GEO 원칙 (미확장 노드, 필요 시 소화).
- **[nasmedia — AI 브리핑 인용 콘텐츠 70%가 UGC](https://blog.nasmedia.co.kr/entry/2606naspick)** — 인용의 주 무대가 블로그·카페임을 확인.

### ③ 메이트 프로그램·선정 사례 (사실)

- **[커넥터스 선정 후기 (brunch)](https://brunch.co.kr/@connectus/507)** — 프리미엄콘텐츠 4,323채널 중 인용 1위 (누적 435만, 월 103만).
  "AI가 모르는 이야기를 썼더니 AI가 가장 많이 인용" — 유통·물류 틈새 전문성 > 재테크 레드오션.
  **사전 동의(AI 활용 설정) 받은 콘텐츠만 인용 대상**이라는 사실도 여기서 확인.
- [ZDNet 2026-05-28](https://zdnet.co.kr/view/?no=20260528132349) — 프로그램 구조 (plan.md에 정리됨).
- [ZDNet 2026-06-22](https://zdnet.co.kr/view/?no=20260622165503) — 상위 100명 300만 / 최상위 10명 1,000만.
  선정 종합 기준: 인용수 + 주제 전문성·활동성·신뢰도·이용자 반응·검색 활용도·유료 판매 지수.
- [전자신문 2026-07-29](https://www.etnews.com/20260729000333) — 시행 후 AI 브리핑 인용 48% 증가 (경쟁 심화 신호).
- [Threads @blog.oppa](https://www.threads.com/@blog.oppa) — 인용수 확인 UI: 2026-06-04부터 각 서비스 프로필에서 본인 인용수 확인, 2026-01부터 누적 집계 (asserted — 실계정에서 검증 필요).

### ④ 제재·차단 신호 (리스크)

- **[뉴스버스 — 'AI 따발총' 저품질 블로그 제재 강화](https://www.newsverse.kr/news/articleView.html?idxno=9959)** —
  하루 100건씩 AI 글 발행 계정에 제재 강화 방침. 완전 자동 설계의 상한선을 정의하는 노드.
- 2025-07부터 공감·댓글·서이추 등 봇 행위 강력 차단 (검색 요약 다수에서 반복 — 원 출처 미확인, inferred).
- [전자신문 2025-07-16](https://www.etnews.com/20250716000347) — 네이버가 외부 AI 봇 크롤링 차단 (우리 관찰 스크래핑도 차단 대상일 수 있음 — 기술 난제).

## 매트릭스 스윕 결과 (2차, 기능×지역 20셀 병렬)

수치: 원시 160건 → URL 중복 제거 129건 → 검증 30건 (**keep 24 / drop 6**) + 미검증 99건.
검증은 실재·활성도·요약 일치를 에이전트가 URL 직접 열어 판정. 미검증분은 후순위 kind(지식·영상)라 상한에서 잘림 — 아래 하이라이트만 발췌, 전체는 raw JSON.

### 검증 통과 (keep 24) — 기능별

**자동 발행 (publish)**
- [konamgil/naver-blog-mcp](https://github.com/konamgil/naver-blog-mcp) — TS MCP. 발행·예약·삭제 + 세션 암호화 저장·만료 자동 복구 + SQLite 예약 폴링까지 12개 툴. 2026-05 단발 릴리스(스타 0)지만 **세션 복구·예약 설계가 우리 발행 모듈의 직접 본보기**
- [SIMHANSOL/Selenium-NaverBlogAutomaticPosting](https://github.com/SIMHANSOL/Selenium-NaverBlogAutomaticPosting) — 네이버 로그인·에디터 iframe 기본 패턴 (2022년산, 셀렉터는 낡음)
- [AIMedia](https://github.com/Anning01/AIMedia) — ★2,418. 핫이슈 수집→AI 작성→발행 통합 (중국). 후속작 MediaFlow/AiMaster도 볼 것
- [AIWriteX](https://github.com/iniwap/AIWriteX) — ★1,835, 활발. 공중호 전자동 + **'去AI味'(AI 티 제거) 품질 모듈** — AI 냄새 제거는 우리 품질 게이트 관심사
- [blog-auto-publishing-tools](https://github.com/ddean2009/blog-auto-publishing-tools) — ★284, 2년 비활성. 플랫폼별 어댑터 구조 참고
- [playwright-automation](https://github.com/iamtornado/playwright-automation) — Playwright 발행 엔진의 셀렉터·모듈 관리 사례
- [cross-post](https://github.com/shahednasser/cross-post) — ★131, 중단. 플랫폼별 발행 어댑터 인터페이스 설계
- [AUTO-blogger](https://github.com/AryanVBW/AUTO-blogger) — 생성→이미지→SEO→발행 단계 분리 구조 (소규모)
- [NoteClient2](https://github.com/Mr-SuperInsane/NoteClient2) — note.com 비공식 발행: 로그인만 Playwright + 이후 내부 API + 쿠키 세션 재사용 — **하이브리드 발행 패턴**
- [GitHub topic: blog-automation](https://github.com/topics/blog-automation?o=desc&s=updated) — 상시 탐색 입구 (당일까지 갱신 중)

**주제·키워드 발굴 (topic)**
- [naver-search-mcp](https://github.com/isnow890/naver-search-mcp) — ★81, 3주 전 커밋. **네이버 검색 API + 데이터랩 트렌드 MCP** — 주제 발굴 모듈에서 바로 호출 가능, 카카오 PlayMCP에도 등재
- [python_nevada](https://github.com/taegyumin/python_nevada) — 네이버 검색광고 API 래퍼(월간 검색량·연관 키워드). 6년 방치라 호출 패턴만 참고
- [naver-openapi-guide 데이터랩 예제](https://github.com/naver/naver-openapi-guide/blob/master/sample/python/APIExamDatalabTrend.py) — 공식 org, 트렌드 API 호출 형식
- [TrendRadar](https://github.com/sansan0/TrendRadar) — ★61k. 멀티 플랫폼 핫리스트 수집→키워드 필터→**텔레그램 브리핑** — 우리 구조와 가장 근접 (GPL-3.0, 구조만 참고)
- [DailyHotApi](https://github.com/imsyy/DailyHotApi) — ★4,004. 멀티소스 핫리스트를 단일 API로 정규화
- [hunter-ai-content-factory](https://github.com/Pangu-Immortal/hunter-ai-content-factory) — ★386. **주제 후보를 LLM이 '쓸 가치 있나' 점수화(선제 스코어링)** + ChromaDB 중복검사
- [spider-BaiduIndex](https://github.com/longxiaofei/spider-BaiduIndex) — ★800. 로그인 필수 트렌드 지표 사이트의 SDK화 패턴
- [seo-keyword-research-tool](https://github.com/chukhraiartur/seo-keyword-research-tool) — 자동완성+PAA+연관검색 3원천 병합 구조 (네이버판으로 치환)
- [trendspyg](https://github.com/flack0x/trendspyg) — 활발한 pytrends 대체재 (구글 트렌드 교차 검증용). ※ pytrends 본가는 archived — 검증에서 drop됨
- [python-for-seo](https://github.com/HasData/python-for-seo) — PAA 질문 트리·콘텐츠 갭 분석 기능 분해 참고 (유료 API 홍보용 코드)
- [Keyword-generator-SEO](https://github.com/sundios/Keyword-generator-SEO) — 접두/접미사 확장 키워드 마이닝 기법

**LLM 작문 파이프라인 (writing)**
- [STORM (stanford-oval)](https://github.com/stanford-oval/storm) — ★31k, MIT. **리서치→다관점 질문→아웃라인→인용 포함 아티클** — 작문 파이프라인의 정석 레퍼런스
- [choigpt-ai/naver-blog-automation](https://github.com/choigpt-ai/naver-blog-automation) — 2026-07 신생. 소재→**골든키워드(검색량÷문서수)**→SEO 글→썸네일→크롬 '임시저장'까지. 네이버 특화 흐름 전체가 참고
- [cd000242-sudo/naver](https://github.com/cd000242-sudo/naver) — QualityGate→PublishGuard→ExposureMonitor **품질 게이트 파이프라인 문서화** (AGPL, 구조만)

### 검증 탈락 (drop 6) — 재검토 방지용

movemin03/NaverBlog_Auto(낡은 단일 스크립트) · kwanwon/naver-blog-automation(개인 작업 덤프) · Neeraj-Sihag/WordPress-Post-Automation(방치 토이) · Qiita note 자동투고 글(404, 계정 삭제) · dig_baiduzhidao_keywords(Python2 유물) · **pytrends(archived — 기반 라이브러리로 쓰지 말 것, trendspyg로 대체)**

### 미검증 하이라이트 (99건 중 발췌 — 쓰기 전에 개별 검증)

- **네이버 직결**: [NAEO — 네이버 AI 인용/노출 최적화 국산 솔루션](https://www.naeo.kr/) · [코드잇 — 2026 네이버 알고리즘 변화(AI 브리핑·메이트)](https://sprint.codeit.kr/blog/naver-blog-algorithm-change-ai-briefing-clip-mate) · [아이보스 — 순위보다 AI 브리핑 인용](https://www.i-boss.co.kr/ab-6141-71497) · [Threads @wizsuni — AI 브리핑 인용 글쓰기 기준](https://www.threads.com/@wizsuni/post/DY8F0PNE8df) · [SEO NEWS — Top10 밖 콘텐츠도 인용됨](https://seonews.co.kr/naver-ai-briefing-geo-202605/) · [Lynny House — 네이버 자동 포스팅 ChatGPT 시리즈](https://lynny.kr/) · 마피아넷(키워드 도구) · 크리에이터 어드바이저 활용법
- **GEO 측정 도구(코드)**: [gego](https://github.com/AI2HU/gego)(프롬프트 스케줄 실행→인용 수집) · [GeoLook](https://github.com/aigclink/geolook)(★500, 진단→샘플링→개선 폐루프) · [deepseek-geo](https://github.com/DeepSeekGEO/deepseek-geo) · [GEORank](https://github.com/yaojingang/GEORank)(★398) · [SerpBear](https://github.com/towfiqi/serpbear)(자가호스팅 순위 추적, SQLite+크론+알림 — 우리 측정 루프와 동형) · [applleeee/Blog-Rank](https://github.com/applleeee/Blog-Rank)(네이버 검색 순위 조회) · [NaverBlogVisitorCntCrawler](https://github.com/krta2/NaverBlogVisitorCntCrawler)
- **작문·리서치(코드)**: [GPT Researcher](https://github.com/assafelovic/gpt-researcher)(★20k+, pip로 리서치 단계 통합 후보) · [wewrite](https://github.com/imraywang/wewrite)(★3,098, 핫토픽→선제→작문→SEO→품질검증→초안함) · [TrendPublish](https://github.com/liyown/ai-trend-publish)(★3,136) · [claude-blog](https://github.com/AgriciDaniel/claude-blog)(5-gate 품질 계약) · [article-writer](https://github.com/wordflowlab/article-writer)(Claude Code를 작문 엔진으로)
- **GEO 이론·표준**: [Princeton GEO 논문 (KDD 2024)](https://arxiv.org/abs/2311.09735) · [llms.txt 표준](https://github.com/answerdotai/llms-txt) · [awesome-generative-engine-optimization](https://github.com/amplifying-ai/awesome-generative-engine-optimization) · [Ahrefs — LLM 인용 얻는 법](https://ahrefs.com/blog/llm-citations/) · Qiita — 오가닉 순위권 밖인데 AIO에 인용된 사례(구조화 데이터+llms.txt)
- **경계 사례(반면교사)**: [HN — 커밋 하나에 AI 글 12,000개](https://news.ycombinator.com/item?id=47640722) · [Playwright 知乎 SPA 발행 '가짜 성공' 함정](https://juejin.cn/post/7657113781043871784)(발행 성공 오판 배드 케이스) · [dev.to — 매일 팩트체크 SEO 글 내는 자율 에이전트 운영기](https://dev.to/paul_irolla_6b5ae261d0224/im-running-an-autonomous-ai-agent-that-publishes-a-fact-checked-seo-article-every-day-heres-the-1753)

### 스윕이 기존 결론에 보태는 것

1. **측정 루프의 설계 원형이 이미 존재** — SerpBear(순위·유입 자동 수집→알림)와 GeoLook(질문 샘플링→인용 측정→개선 폐루프)이 우리가 만들려는 루프와 동형. 처음부터 발명할 필요 없음.
2. **품질 게이트에 '去AI味(AI 티 제거)' 차원 추가** — 중국 생태계는 AI 탐지 회피가 아니라 '읽히는 글'을 위해 이 모듈을 표준 장착. 네이버 어뷰징 필터 대응과 같은 방향.
3. **주제 선정은 점수화 가능** — 골든키워드(검색량÷문서수), 선제 스코어링(LLM이 작성 가치 판정) 두 패턴이 검증된 상태로 존재.
4. **발행은 하이브리드 패턴 후보** — NoteClient2처럼 로그인만 브라우저 + 이후 내부 API+쿠키 재사용이 가능한지 네이버에서 실험 가치. 불가면 konamgil MCP식 풀 Playwright.

## 미확장 노드 (필요해지면 소화)

- naver-blog-mcp(space-cap·konamgil 둘 다)의 코드 상세 (셀렉터·로그인 플로우) — 3부 스택 확정 후 정독
- TBWA·오픈타임 GEO 문서 원문, Princeton GEO 논문 — 글 템플릿 설계 시
- 크리에이터 어드바이저의 유입 키워드 통계 화면 — 실계정 확보 후 직접 확인
- 메이트 선정자 목록 공개 위치·형식 — 매월 관찰 대상으로 삼을 수 있는지
- 미검증 99건 — 쓰려는 시점에 개별 검증 (raw JSON에 전체)
