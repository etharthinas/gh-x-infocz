"""채널 크롤러. 각 모듈의 BaseCrawler 하위 클래스는 `engine`을 지정하는 순간 BaseCrawler.registry에 등록된다.

engine 코드는 engines 테이블과 일치해야 한다:
    google_aio   Google AI Overviews / AI Mode
    naver_ai_briefing  네이버 AI 브리핑 (구 Cue:)
    chatgpt      ChatGPT          (chatgpt.py, 로그인 선택)
    gemini       Gemini           (gemini.py, 비로그인 전용)
    claude       Claude           (claude.py, 로그인 필수)
    perplexity   Perplexity
"""

import importlib
import pkgutil

for _mod in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_mod.name}")
