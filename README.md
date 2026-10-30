# Simple OMS

FastAPI와 SQLite로 만든 가상 주문 관리 서버다. 종목·수량·가격을 받아 주문을 저장하고, HTTP 요청으로 접수·부분 체결·전체 체결·취소를 진행한다. 서버 코드를 한 함수씩 읽고 설명할 수 있는 크기를 목표로 한다.

## 실행

Python 3.12 이상. 프로젝트 루트에서 실행한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m uvicorn oms.api:create_app --factory --host 127.0.0.1 --port 8000
```

이미 가상환경을 만들었다면 활성화한 뒤 서버 실행 명령만 사용하면 된다.
[API 문서](http://127.0.0.1:8000/docs)에서 각 요청을 직접 실행할 수 있다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `oms/domain.py` | 요청·응답 데이터의 형태와 기본 입력 검사 |
| `oms/api.py` | HTTP 주소와 처리 함수 연결 |
| `oms/store.py` | 주문 처리와 SQLite 저장·조회 |
| `oms/__main__.py` | 전략 실행 → OMS 주문·체결 → 일별 계좌 기록 |
| `test/oms_audit.py` | OMS 결과와 수학 모델 비교·DB 검산 |

읽는 순서는 **domain → api → store**다. 요청이 들어오면 `api.py`가 `Store`의 메서드를 호출하고, 그 결과를 JSON으로 반환한다.

## 주문 데이터

매수·매도 주문을 제출할 때는 네 항목을 보낸다.

```json
{
  "symbol": "AAPL",
  "quantity": 10,
  "price": 100.0,
  "kind": "buy"
}
```

수량은 양의 정수, 가격은 양의 실수다. 종목명은 앞뒤 공백을 제거하고 대문자로 저장한다. 시간은 서버가 UTC로 기록한다. 취소는 원주문 번호를 주소에 넣어 요청한다.

저장하는 종류는 `buy`(매수), `sell`(매도), `cancel`(취소) 세 가지다. 응답에는 기본 항목 외에 다음 처리 정보가 포함된다.

| 항목 | 의미 |
| --- | --- |
| `order_id` | 서버가 발급한 주문 번호 |
| `time` | 주문·요청이 생성된 시간 |
| `status` | 현재 처리 상태 |
| `filled_quantity` | 매수·매도 주문에서 누적 체결된 수량 |
| `original_order_id` | 취소 대상 원주문 번호. 매수·매도 주문은 `null` |

## 상태와 API

매수·매도 주문은 다음 순서로 진행한다.

```text
submitted(제출) → accepted(접수됨) → partially_filled(일부 처리됨) → completed(처리됨)
                                 └───────────────────────────→ completed(전량 체결)

진행 중인 주문 → canceled(취소됨)
```

체결은 API로 수량을 입력해 직접 발생시킨다. 한 번에 전량을 체결할 수도 있다. 별도 브로커나 시세에 의한 자동 체결은 없다.

| 요청 | 기능 | 요청 본문 |
| --- | --- | --- |
| `GET /health` | 서버 상태 확인 | 없음 |
| `POST /orders` | 매수·매도 주문 제출 | `symbol`, `quantity`, `price`, `kind` |
| `GET /orders` | 원주문과 취소 요청 전체 조회 | 없음 |
| `GET /orders/{id}` | 한 건 조회 | 없음 |
| `POST /orders/{id}/accept` | 접수 처리 | 없음 |
| `POST /orders/{id}/fills` | 입력 수량만큼 가상 체결 | `quantity` |
| `POST /orders/{id}/cancel` | 남은 수량 취소 | 없음 |

취소는 원주문을 `canceled`로 바꾸고, 취소한 잔량을 새 취소 요청으로 저장한다. 이미 체결된 수량은 유지한다. 예를 들어 10주 중 3주를 체결한 뒤 취소하면 남은 7주가 취소된다. 취소 API의 응답은 **새로 생성한 요청 기록**이고, 변경된 원주문은 `GET /orders/{id}`로 확인한다. 취소 요청 자체는 적용 즉시 `completed`가 된다.

접수 전 주문도 취소할 수 있다. 완료·취소된 주문은 더 체결하거나 취소하지 않는다. 입력 형태·양수 조건이 맞지 않으면 HTTP 422, 없는 주문이면 404, 현재 주문에 적용할 수 없는 요청이면 409를 반환한다.

## 직접 실행해 보기

아래 명령은 빈 DB에서 첫 주문 번호가 1인 경우다. 기존 기록이 있으면 응답의 `order_id`로 바꾼다.

```bash
# 매수 10주 제출 → submitted
curl -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"AAPL","quantity":10,"price":100,"kind":"buy"}'

# 접수 → accepted
curl -X POST http://127.0.0.1:8000/orders/1/accept

# 3주 체결 → partially_filled
curl -X POST http://127.0.0.1:8000/orders/1/fills \
  -H 'Content-Type: application/json' -d '{"quantity":3}'

# 원주문은 3주 체결, 7주 미체결 상태
curl http://127.0.0.1:8000/orders/1

# 남은 7주 취소 → 취소 요청 기록 반환
curl -X POST http://127.0.0.1:8000/orders/1/cancel

# 매수·취소 기록 함께 조회
curl http://127.0.0.1:8000/orders
```

## DB

기본 파일은 `data/simple-oms.sqlite3`다. `orders` 테이블 하나에 원주문과 취소 요청을 저장한다. 주문 번호에만 기본키를 두고, 외래키·별도 상태 이력·잔고·체결 테이블은 두지 않는다. 원주문에는 주문 정보와 누적 체결 수량을 저장하며, 개별 체결 시각·체결별 가격 이력은 저장하지 않는다.

조회와 수정은 `sqlite3`로 직접 실행한다. 수정 작업은 짧은 트랜잭션 안에서 처리하고, 취소는 원주문 갱신과 요청 기록 생성을 함께 commit한다. 수량 초과 체결과 완료 주문 변경 등 주문 흐름에 필요한 기본 검사만 둔다.

서버를 재시작해도 같은 파일의 기록이 남는다. 다른 파일을 사용하려면 다음처럼 실행한다.

```bash
OMS_DB_PATH=data/practice-02.sqlite3 python -m uvicorn oms.api:create_app --factory --host 127.0.0.1 --port 8000
```

DB 파일은 Git에서 제외한다. 기존 계좌·브로커 DB와는 다른 새 파일을 사용하므로 기존 기록은 보존된다.

## 전략 실행과 검증

동봉 데이터로 2010-01-04~2025-12-31 전체 기간을 재생한다. λ=5, ETF 9개, 종목별 목표 비중 최대 25%, ETF 목표 비중 합계 100%, 편도 비용 5bp, 초기 자산 100,000 USD다. 투자 가능한 무위험자산은 추가하지 않는다. CAPM 추정·샤프지수와 미투자 잔돈의 이자는 기존 수학 백테스트와 같은 RF를 사용한다.

```bash
python -m pip install -e '.[backtest]'
python -m oms --output results/oms-run
```

기본값은 비용을 고려해 거래를 결정하는 `capm_cost_aware`와 매일 재조정하는 `capm_mean_variance`를 모두 실행한다. `--strategy capm_cost_aware`로 하나만 선택하거나 `--end 2010-01-08`로 짧게 확인할 수 있다. 결과가 있는 출력 폴더는 덮어쓰지 않으므로 재실행할 때는 새 경로를 지정한다.

`oms/__main__.py`는 전일 정보로 전략을 판단하고, 현금 이자를 반영한 뒤 정수 수량으로 주문·접수·체결하고 당일 계좌를 기록한다. 내부 실행은 API도 사용하는 `Store.create()`, `accept()`, `fill()`을 직접 호출한다. 각 전략 폴더에 `orders.sqlite3`, `oms_orders.csv`, `oms_curve.csv`, `metrics.json`을 저장한다.

수학 모델 비교와 DB 검산은 별도 명령으로 실행한다.

```bash
python -m test.oms_audit --output results/oms-audit-lambda5
```

검증 명령은 같은 OMS 실행 루프를 호출하고 다음 세 결과를 비교한다. 기존 수학 백테스트는 소수점 조정가격 단위를 사용하고 현재 OMS는 정수 수량만 받는다.

1. 기존 소수점 수량 수학 백테스트를 다시 계산한다.
2. 수수료를 반영한 목표 수량을 정수로 내림한 수학 원장을 계산한다. 매도 후 매수하며 차입·공매도는 허용하지 않는다.
3. OMS 실행 루프가 실제로 주문·접수·체결한 결과를 SQLite에서 다시 읽는다.

`test/oms_audit.py`가 DB를 다시 열어 모든 거래일의 수량·현금·수수료·평가액을 2번과 대조한다. 현금·잔고·수수료 회계는 `oms/__main__.py`가 담당하며, 주문을 저장하는 `Store`에는 계좌 원장이 없다. 조정가격 단위이므로 실제 증권사에서 거래하는 원주식 수량과는 다르다.

검증 결과 폴더의 `summary.json`에 성과와 오차가, 전략별 `daily_comparison.csv`에 일별 비교가 저장된다. `audit.json`, `run.json`, `checksums.json`에 검증 결과와 입력·코드·출력 해시를 남긴다. 과거 신호·체결 날짜는 CSV에 기록하고, DB의 `time`은 실제 재생 실행 시각이다.

비용 고려 전략의 현재 `initial_investment` 판정은 ETF 보유 비중 합계가 거의 정확히 100%여야 초기 투자가 끝났다고 본다. 정수 수량으로 내림해 잔돈이 남으면 이 조건을 만족하지 않아 매일 초기 투자로 판단할 수 있다. 따라서 정수 OMS 결과가 기존 소수점 수량 모델과 같다고 가정하면 안 된다. 일별 원장 일치 여부와 이 전략 판단의 차이는 별도로 확인한다.

## 이전 버전

단순화 전 코드는 로컬 Git 브랜치 `codex/oms-before-simplification`에 보관했다. 브로커 접수·상태 대사·잔고 예약·수수료·전략 연결이 있는 버전이다. 해당 브랜치의 `README.md`에 이전 실행 방법이 있다.

전략 연구용 `test/`, `configs/`, `datasets/`와 기존 결과는 별도로 남겨 두었다. `python -m oms`는 이 전략 코드와 데이터로 주문을 재생한다. HTTP 서버의 `api.py`, `domain.py`, `store.py`는 전략 코드나 데이터 없이 실행할 수 있다.
