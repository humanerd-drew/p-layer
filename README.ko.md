# p-layer

**에이전트를 위한 거버넌스 메모리.** 의존성 0(Python 표준 라이브러리만)으로 동작하는 메모리 레이어 — SQLite + FTS5 + 플러그형 임베딩, 그리고 P0-P6 레이어 거버넌스를 **프롬프트가 아니라 코드로 강제**.

```
7 layers. 1 memory. Every write audited.
```

[English](./README.md)

## 왜 만들었나

P0-P6 "뇌 레이어" 메모리 개념(drewgent, p-layer)은 훌륭하지만, 레퍼런스 구현들은 같은 핵심 결함을 공유한다:

| 결함 | drewgent / p-layer | p-layers |
|---|---|---|
| 스키마 관리 | `CREATE TABLE IF NOT EXISTS` 남발, 버전 없음 | forward-only + 체크섬 마이그레이션(`schema_migrations`) |
| 검색 인덱스 | external-content FTS5 + 트리거(취약, p-layer `forget`은 이를 깨뜨림) | standalone FTS5, 트리거 결합 없음 |
| 이중 구현 | TS+Python, SQLite+Pg — 드리프트하며 기능 유실 | 단일 구현, 단일 스키마 |
| 거버넌스 | README 표에만 존재("P0이 이긴다") | **코드로 강제** — 레이어 ACL이 `WriteDenied` 예외 발생 |
| remember 툴 | layer=P6 하드코딩, 자기 거버넌스를 우회 | 레이어는 쓰기 파라미터, ACL 검사됨 |

이 저장소는 프로덕션급 재건이다: p-layer의 거버넌스 아이디어를 이식하고, 원본에 없던 스키마 규율을 더하고, **거버넌스가 검색 품질을 높인다는 것을 숫자로 증명하는 이벨 하네스**를 갖췄다.

## 기능

| 기능 | 설명 |
|---|---|
| **P0-P6 레이어 ACL** | 레이어별 쓰기 주체를 코드로 강제 (P0는 system만 … P6는 agent+manual). 거부된 쓰기도 감사 로그에 기록 |
| **하이브리드 recall** | FTS5 + 시맨틱(Ollama 또는 플러그형), RRF 퓨전, confidence × freshness 랭킹, 타입 다양화, superseded 제외 |
| **supersede-not-delete** | forget/update는 항목을 대체. 이력 보존, recall에서는 사라짐 |
| **스냅샷** | 활성 항목을 버전 라벨로 고정, 롤백 시 이후 항목 일괄 대체 |
| **감사 로그** | 모든 쓰기 + 모든 거부된 쓰기 기록 — 거버넌스 준수 증거 |
| **모순 스캔** | LLM 없이 휴리스틱: 규칙 우선순위 충돌, 레이어 간 중복 |
| **P5 위키 컴파일** | 활성 메모리를 레이어별 마크다운(provenance 포함) + INDEX로 오프라인 컴파일 |
| **MCP 서버** | 12개 툴, 의존성 0 stdio 구현 — opencode/Claude/Cursor 어디든 |
| **이식 도구** | `import-drewgent` — 기존 drewgent `knowledge.db`를 스키마 재검증·재임베딩하며 이식 (세션은 episodes로 이관) |
| **그래프 & 추론** | `graph_explore` / `graph_trace` / `graph_rca`(caused/fixed_by 체인) / `transitive_closure` — drewgent graph_query.py 패리티, 사이클 안전 |
| **볼트 인제스트** | `import-rules`(rules.md → rules), `import-incidents`(P6 사건 → episodes) — 볼트는 파일로 유지, p-layers가 참조 |
| **p-layers 호환** | 이 패키지는 PyPI에 **`p-layers`**로 발행된다 (GitHub 레포: p-layers). `p_layer/`가 0.1.x `KnowledgeDB` API와 `knowledge_*` MCP 툴을 이 엔진 위에 노출 — 기존 p-layers 설치가 코드 수정 없이 업그레이드 |
| **재임베딩 잡** | `reembed` — 모델 전환 후 임베딩 백필. 벡터는 버전관리(이전 버전 유지), recall은 현재 버전만 조회. 멱등 |
| **Consolidation** | `consolidate` — 미압축 에피소드를 `insight` 다이제스트로 압축(episodic → semantic). 오프라인 결정적 요약 + LLM 훅, 멱등, 감사 기록 |
| **PostgreSQL 백엔드** | `PgStore` — SQLite `Store`와 동일 인터페이스·거버넌스, 패리티 테스트로 검증 (pg_trgm ILIKE로 CJK, pgvector 시맨틱). 운영 잡은 SQLite 전용, 명시적으로만 |

## 증명: 거버넌스가 검색 품질을 높인다

같은 데이터, 두 엔진, 한 명령어(`p-layer eval suite.json`):

```
recall@k (same data, two engines):
  drewgent baseline : 0.667 (2/3)      ← naive FTS OR-join, 삽입 순서
  p-layer            : 1.000 (3/3)      ← confidence/freshness 랭킹
  delta             : +0.333
ACL compliance: 100.0% (30/30) enforcement cases correct
```

베이스라인은 메타데이터가 없어 움직일 수 없다. p-layers는 거버넌스 메타데이터(confidence, 레이어, supersession)를 검색 품질로 전환하고, ACL 30케이스 전부가 정확히 허가/거부된다.

## 빠른 시작

```bash
# 의존성 없음 (Python >= 3.9)
export P_LAYER_EMBED=hash   # 오프라인 폴백; 기본은 ollama
export P_LAYER_DB=~/.p_layer/memory.db

python3 -m p_layer init
python3 -m p_layer remember "switched to portone v2 for payments" --type decision --layer P5
python3 -m p_layer recall "portone"
python3 -m p_layer assemble --budget 12000     # 규칙 먼저, 최근 지식 다음
```

Python API:

```python
from p_layer.store import Store, WriteDenied

db = Store()
db.add_knowledge("client prefers weekly sync", type="preference", layer="P6", who="agent")
print(db.recall("weekly sync", limit=5))
try:
    db.add_knowledge("secret", layer="P0", who="agent")   # P0은 system만
except WriteDenied:
    pass
print(db.audit_log(denied_only=True))                     # 거부 기록이 남아 있다
```

MCP (opencode / Claude Desktop / Cursor):

```json
{
  "mcp": {
    "p-layers": {
      "type": "local",
      "command": ["python3", "-m", "p-layers", "serve"],
      "env": { "P_LAYER_DB": "~/.p_layer/memory.db" }
    }
  }
}
```

## 거버넌스 모델

| 레이어 | 목적 | 쓰기 허용 |
|---|---|---|
| P0 | 불변 규칙 | system만 |
| P1 | 정체성 & 페르소나 | system만 |
| P2 | 원시 세션 아카이브 | system, gateway, cron |
| P3 | 툴 통합 | system, gateway, cron |
| P4 | 스킬 & 성장 | system, cron, agent, manual |
| P5 | 컴파일된 지식 | system, cron, agent, manual, tool |
| P6 | 사건 & RCA | system, cron, agent, manual, tool |

우선순위는 프롬프트가 아니라 데이터다. `assemble()`은 토큰 예산 아래 프리시던스 순서로 규칙을 내보낸다.

## 개발

```bash
python3 -m unittest discover -s tests -v   # 133개 테스트 (PG 22개는 DSN 없으면 스킵)
```

## 예제

- `examples/quickstart.py` — API 워크스루
- `examples/demo_import_eval.sh` — 전체 이주 스토리: drewgent 픽스처(지식+세션+온톨로지+볼트 파일) → 이식 → 볼트 인제스트 → 거버넌스 전후 eval → 감사 → 그래프 → 모순 → 위키
- `examples/suite.example.json` — eval 스위트 형식
- `examples/opencode-p-layer.jsonc` — drewgent의 remember/recall 툴을 대체하는 MCP 설정 (복붙용)

## drewgent 메모리를 p-layers로 교체하기

볼트(정체성·페르소나·스킬 파일)는 파일로 유지한다 — 저장소 클래스가 다르며 DB가 되어서는 안 된다. p-layers는 **지식 레이어**를 대체한다:

```bash
# 1. 데이터 이주 (지식 + 엔티티 + 관계 + 세션)
python3 -m p_layer import-drewgent ~/.drewgent/.opencode/knowledge.db --embed ollama

# 2. 볼트 중 스토어에 속하는 것만 인제스트 (선택)
python3 -m p_layer import-rules ~/.drewgent/@identity/brain/rules.md
python3 -m p_layer import-incidents ~/.drewgent/P6-prefrontal/incidents

# 3. 에이전트를 MCP 서버로 연결 (examples/opencode-p-layer.jsonc),
#    AGENTS.md의 툴 지시도 p-layers 기준으로 교체
```

교체 후 `p-layer eval suite.json`이 증명한다: 같은 데이터, 거버넌스 메타데이터 적용 시 recall@k 0.667 → 1.000, ACL 30/30.

## PostgreSQL 백엔드 (멀티에이전트 / SMB 단계)

`PgStore`는 SQLite `Store` 인터페이스를 그대로 미러링한다 — 같은 메서드, 같은 거버넌스, 양 백엔드에 모든 행동 단언을 실행하는 공유 패리티 스위트로 검증된다.

```python
from p_layer.pgstore import PgStore

db = PgStore("dbname=memory host=localhost user=me")   # 또는 P_LAYER_PG_DSN
db.add_knowledge("switched to portone v2", type="decision", layer="P5")
print(db.recall("portone"))
```

- **FTS**: `to_tsvector('simple')` + ts_rank, pg_trgm ILIKE 보완(CJK 대응).
- **시맨틱**: pgvector(`vector(768)`), 선택 사양 — 없으면 FTS-only, `semantic_available: false`로 보고.
- **안전성**: `statement_timeout` + `connect_timeout` — 락 대기가 멈춤이 아니라 깨끗한 오류로.
- **운영 경계**: 단일 작성자 유지보수 잡(`reembed`, `consolidate`, `compile-wiki`)은 SQLite에서 실행, Pg에서는 `NotImplementedError`로 명시적 거부 (조용한 성능 저하 없음).
- **CI**: postgres 서비스 컨테이너로 전체 스위트를 실제 DB에서 실행.

## 크레딧


다음 프로젝트들의 아이디어를 프로덕션급으로 재건한 것입니다:

- [opencode-drewgent](https://github.com/humanerd-drew/opencode-drewgent) — P0-P6 볼트 개념, provenance 규약
- [p-layer](https://github.com/humanerd-drew/p-layer) — 레이어 권한/ACL 설계, supersede-not-delete, confidence/TTL 랭킹, 스냅샷
- [Gajae-Code](https://github.com/Yeachan-Heo/gajae-code) — 에이전트 오케스트레이션 관례

이 저장소를 만든 비판적 평가는 README 위쪽에, 좋은 아이디어의 출처는 크레딧에 있습니다.

## 라이선스

MIT
