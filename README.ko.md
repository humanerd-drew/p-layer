# P-Layer

**7개의 계층. 1개의 메모리. 제로 카오스.**

AI 에이전트의 메모리를 불변 규칙(P0)부터 인시던트 회고(P6)까지 거버넌스 계층으로 조직화하세요. 각 계층은 계약을 가집니다: 누가 쓸 수 있는지, 언제 질의하는지, 어떻게 유지하는지.

```python
pip install p-layers          # repo: p-layer, PyPI: p-layers, import: p_layer
python3 -m p_layer.mcp.server  # SQLite로 MCP 서버 시작 (zero config)
```

## 7개 계층

| 계층 | 이름 | 목적 | 질의 시점 | 쓰기 권한 |
|------|------|------|-----------|----------|
| **P0** | brainstem | 불변 규칙 | 모든 세션 시작 시 | 시스템 전용 |
| **P1** | limbic | 정체성 및 페르소나 | 세션 시작 + 출력 시 | 사람 전용 |
| **P2** | hippocampus | 원시 세션 아카이브 | **최후의 수단** | 추가 전용 |
| **P3** | sensors | 도구 통합 | 디버깅 시 | 시스템 + 크론 |
| **P4** | cortex | 스킬 및 성장 | 스킬 선택 시 | 에이전트 + 수동 |
| **P5** | ego | **컴파일된 위키** | **1순위** | 자동 생성 |
| **P6** | prefrontal | 인시던트 및 RCA | RCA 수행 시 | 에이전트 + 수동 |

## 실제 흐름

AI 어시스턴트가 빌드 파이프라인에서 버그를 발견합니다:

1. **P6** — 타임라인 + 근본 원인이 포함된 인시던트 리포트 작성
2. **P0** — 근본 원인이 규칙 위반이었다면 P0 수정안 제안
3. **`knowledge_recall`** — 몇 주 후 비슷한 증상이 나타나면 MCP 서버가 인시던트를 신뢰도 + 신선도 + 세렌디피티 순으로 표시
4. **`wiki_compile.py`** — 하루가 끝나면 모든 인시던트와 수정 사항이 P5 위키 페이지로 컴파일
5. **쿼리 라우팅** — 다음 세션에서 컴파일된 지식을 즉시 찾음 (P5 우선, P2 최후)

같은 버그는 두 번 발생하지 않습니다 — 에이전트가 기억해서가 아니라 거버넌스 계층이 학습했기 때문입니다.

## MCP 서버

7개의 도구, 모두 포함:

| 도구 | 기능 |
|------|------|
| `knowledge_remember` | 신뢰도, TTL, 버전 레이블로 사실 저장 |
| `knowledge_recall` | 순위 검색 — 신뢰도 + 신선도 + 5% 세렌디피티 |
| `knowledge_forget` | 소프트 삭제 (대체, 절대 파괴하지 않음) |
| `knowledge_update` | ID로 업데이트 — 이전 버전은 대체되고 기록 보존 |
| `knowledge_memory-stats` | 계층별 항목 수 |
| `knowledge_snapshot-create` | 현재 상태를 버전 레이블로 스냅샷 |
| `knowledge_snapshot-rollback` | 스냅샷 이후 생성된 항목 대체 |

### MCP 클라이언트 설정

**opencode** (`opencode.jsonc`):
```json
{
  "mcp": {
    "p-layer": {
      "type": "local",
      "command": ["python3", "-m", "p_layer.mcp.server"],
      "env": { "KNOWLEDGE_PG_DSN": "{env:KNOWLEDGE_PG_DSN}" },
      "enabled": true
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "p-layer": {
      "command": "python3",
      "args": ["-m", "p_layer.mcp.server"],
      "env": { "KNOWLEDGE_PG_DSN": "" }
    }
  }
}
```

## 아키텍처

```
P0-brainstem (rules) ─────────── 모든 계층 쓰기 권한 통제
P1-limbic (persona) ──────────── 에이전트 목소리 정의
                                         │
P2-hippocampus (raw data) ──────────────┤
  │                                      │
  ├──→ sessions/ (추가 전용 로그)         │
  ├──→ memories/ (추출된 항목)            │
  └──→ knowledge/ (수집된 아티팩트)       │
                                           ▼
P3-sensors ──→ MCP 설정 ──→    P_LAYER KNOWLEDGEDB   ←── P4-cortex 스킬 인덱스
                                     (Pg + SQLite)                  │
                                           │                        │
                ┌──────────────────────────┤                        │
                ▼                          ▼                        ▼
      knowledge_recall           P5-ego/wiki/compiled/      P6-prefrontal
      (순위 FTS + 벡터)         (매일 자동 생성)           (인시던트 + RCA)
                                      │
                                wiki_lint.py
                                (깨진 링크 검사)
```

### 백엔드 선택

| 변수 | 효과 |
|------|------|
| `KNOWLEDGE_PG_DSN` 미설정 | SQLite 모드 (`.knowledge/knowledge.db`) |
| `KNOWLEDGE_PG_DSN=dbname=...` | PostgreSQL 기본, SQLite 폴백 |
| `KNOWLEDGE_DB_DIR=/path` | 사용자 지정 SQLite 디렉토리 |

## 쿼리 라우팅 (우선순위)

에이전트가 정보를 검색할 때:

```
1. P5-ego/wiki/compiled/       ← 컴파일된 위키 (먼저 확인)
2. P5-ego/memory/               ← 저장된 선호도
3. P2-hippocampus/knowledge/    ← 원시 수집 지식
4. P2-hippocampus/memories/     ← 원시 세션 메모리
5. P2-hippocampus/sessions/     ← 원시 세션 로그 (최후 수단)
6. KnowledgeDB (SQLite/Pg)      ← 교차 폴백
```

1-2단계가 약 80%의 쿼리를 커버합니다. 3+단계는 갭 → 다음 wiki-compile 사이클에서 처리.

## 프로젝트에 p-layers 사용하기

`p-layers/` 디렉토리에는 표준 거버넌스 계약이 포함되어 있습니다. 각 계층은 프로젝트의 런타임 디렉토리에 매핑됩니다:

```
your-project/
├── p-layers/               ← 계약 문서 (표준, 읽기 전용)
│   ├── P0-brainstem/README.md
│   └── ...
├── P2-hippocampus/         ← 런타임 데이터 (세션, 아카이브)
│   └── sessions/
├── P5-ego/
│   └── wiki/compiled/      ← wiki_compile.py가 자동 생성
└── P6-prefrontal/
    └── incidents/          ← 인시던트 리포트
```

**복사** → `cp -r p-layers/ your-project/` (직접 소유, 자유롭게 커스터마이징)\
**서브모듈** → `git submodule add <url>` (동기화 유지)\
**참조** → 에이전트 초기화 워크플로를 `p-layers/P0-brainstem/README.md`로 지정

## 스크립트

| 스크립트 | 목적 |
|----------|------|
| `scripts/ontology_setup.py` | 엔티티 타입 계층 + 관계 제약 초기화 |
| `scripts/seed_knowledge_db.py` | 스키마 + 시드로 knowledge.db 부트스트랩 |
| `scripts/ingest_fact.py` | CLI에서 단일 사실 삽입 |
| `scripts/ingest_instructions.py` | .md 파일을 KnowledgeDB에 배치 수집 |
| `scripts/inference.py` | 전이 폐쇄, 역추적, 모순 감지 |
| `scripts/wiki_compile.py` | KnowledgeDB → Markdown 위키 페이지 + INDEX.json |
| `scripts/wiki_lint.py` | 깨진 링크 감지, INDEX 일관성 확인 |

## 온톨로지 계층

6개 루트 카테고리에 걸친 24개 엔티티 타입:

```
artifact    → doc, code, project
agent       → persona, tool, script, skill
decision    → pattern, preference
event       → incident, session
knowledge   → concept, paper, reference
meta        → category, _task, fact
```

관계 제약은 삽입 시 타입 안전성을 강제합니다:

| 관계 | 출처 → 대상 |
|------|-------------|
| `depends_on` | any → tool/script/skill |
| `fixed_by` | incident → pattern/decision |
| `caused` | decision/pattern → incident |
| `led_to` | decision → decision |
| `cites` | paper → paper |
| `contradicts` | decision/pattern → decision/pattern |

## 라이선스

MIT
