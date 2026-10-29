# Daily CAPM Portfolio & OMS

미국 섹터 ETF 9개의 비중을 매일 계산하는 자산배분 백테스트와 가상 주문 관리 시스템이다.
핵심 흐름은 **과거 수익률 → CAPM 기대수익·공분산 → 평균·분산 비중 → 다음 거래일 리밸런싱**이다.
가상 주문 모드에서는 같은 모델로 계산한 비중을 정수 매수·매도 주문으로 바꾸고, 로컬 HTTP 서버·가상 브로커를 거쳐 체결과 잔고 반영까지 실행한다.

## 코드 읽는 순서

| 순서 | 파일·함수 | 하는 일 |
| --- | --- | --- |
| 1 | [run.py · run](test/run.py) | 데이터 읽기부터 백테스트 실행까지 전체 흐름 |
| 2 | [model.py · estimate](test/model.py) | 과거 252일로 CAPM 기대수익과 공분산 추정 |
| 3 | [model.py · optimize](test/model.py) | 기대수익 − 위험 벌점이 최대인 비중 계산 |
| 4 | [backtest.py · build_targets / simulate](test/backtest.py) | 일별 신호 생성, 매수·매도 차액과 비용·자산 기록 |
| 5 | [excel.py · save_excel](test/excel.py) | 날짜별 계산·계좌·매매 내역을 엑셀에 저장 |
| 6 | [paper.py · rebalance](oms/paper.py) | 비중을 정수 주문으로 변환하고 HTTP로 가상 주문·체결 실행 |
| 7 | [service.py · OrderService](oms/service.py) | 모의 주문의 중복 방지·체결·취소·상태 대사 |
| 8 | [store.py · account_from_db / create_once](oms/store.py) | 체결로 잔고 복원, 미체결 주문의 현금·주식 예약 |

이해하기 쉬운 변수명과 함수, 명시적인 계산 순서를 우선한다.

## 실행

Python 3.12 이상. 아래 명령은 프로젝트 루트에서 실행한다. 동봉 데이터로 실행할 때는 API 키나 데이터 다운로드가 필요 없다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[backtest,demo]'

python -m test.run --overwrite
```

성과는 터미널에 표시된다. 일별 자산·매매·비중 CSV와 지표 JSON은 `results/backtest/`에 저장되며 Git에서는 제외한다.
같은 폴더의 `backtest.xlsx`에는 `일별 계좌`, `ETF별 계산`, `매매 내역` 시트가 생성된다. ETF별 연율 기대수익·변동성·분산·목표 비중·보유 수량과 날짜별 현금·총자산·비용을 확인할 수 있다. 엑셀은 Python 계산값을 저장한 결과이며, 설정을 바꾸면 명령을 다시 실행한다.
`--output results/experiment-02`로 실험 결과를 따로 저장할 수 있다. 데이터 수집은 [data.py](test/data.py), 설정은 [daily-capm.json](configs/daily-capm.json)에 있다.

입력 데이터는 `datasets/`에 모아둔다. `adjusted_close.csv`는 가격, `factors.csv`는 RF 등 일별 자료, `raw/`는 원본, `manifest.json`은 출처·검증 정보다.

거래 CSV를 다시 읽어 자산·비용을 검산하려면 `python -m test.audit`, 모의 주문을 시연하려면 `python -m oms.demo`를 실행한다.
기존 학습용 API는 `python -m uvicorn oms.api:create_app --factory --host 127.0.0.1 --port 8000`으로 실행한다.

## 가상 주문 실행

첫 번째 터미널에서 10만 달러 가상 계좌 서버를 실행한다. 학습용 DB와 분리해 `data/paper.sqlite3` 및 브로커 DB에 기록한다.

```bash
python -m uvicorn oms.api:create_paper_app --factory --host 127.0.0.1 --port 8001
```

두 번째 터미널에서 같은 가상환경을 활성화하고 실행한다.

```bash
python -m oms.paper
```

전 거래일까지의 252일 자료로 비중 계산 → 정수 목표 수량에서 기존 보유 수량 차감 → 매도 후 매수 → 브로커 체결 → OMS 상태 대사 → 현금·보유 수량 검산 순서다. `/orders`에 접수된 `NEW` 주문은 체결 확인 후 `FILLED`가 된다. 주문당 한도 외에 가용 현금·매도 가능 수량을 확인하고, 미체결 주문의 예약분도 차감한다.

API 문서는 [localhost:8001/docs](http://127.0.0.1:8001/docs), 잔고와 보유 수량은 [localhost:8001/account](http://127.0.0.1:8001/account)에서 확인한다. 결과 JSON은 `results/paper/`에 저장한다. 날짜를 지정하려면 `python -m oms.paper --date 2025-12-30`처럼 실행한다.

기본 가격 날짜는 동봉 데이터의 마지막 거래일인 2025-12-31이다. 전일 신호와 당일 관측된 원본 `close`를 사용하고, 이 시세를 고정해 지정가에 즉시 전량 체결하는 오프라인 시연이다. 실시간 가격·호가·유동성·수수료·슬리피지는 재현하지 않는다. 백테스트의 분수 단위 원장과 달리 정수 주식을 사용하므로 잔여 현금이 생긴다. 같은 시세와 모델로 다시 실행하면 목표 수량이 같아 추가 주문은 없다. 다른 날짜로 실행하면 기존 가상 보유분에서 다시 리밸런싱한다.

체결 기록을 지우지 않고 재시작할 수 있다. 미확정 주문이 있으면 가상 주문 실행을 멈추며, API에서 상태 대사 또는 취소 후 다시 실행한다.

## 모델의 기준

- 대상: XLB(소재), XLE(에너지), XLF(금융), XLI(산업재), XLK(기술), XLP(필수소비재), XLU(유틸리티), XLV(헬스케어), XLY(경기소비재).
- 기대수익: `평균 RF + 베타 × 평균 시장 초과수익`을 연율화한다. 시장 대용치는 SPY다.
- 목적함수: `max μᵀw − λ/2 × wᵀΣw`. 비중 합 100%, 공매도 없음, ETF당 최대 25%, λ=5다. λ는 초기 실험 가정이다.
- 공분산: 252일 표본 공분산을 연율화하고 대각 행렬로 10% 축소한다.
- 시점: t일 종가로 비중 계산 → t+1일 종가 체결 → 이후 수익부터 새 비중에 반영한다.
- 비용: 매수·매도 금액 각각 0.05%. 비용 차감 후 투자금을 계산해 차입을 방지한다.

## 백테스트 결과와 범위

2010~2025년 동봉 데이터 기준 비용 차감 후 CAGR은 CAPM 평균·분산 **9.84%**, 동일비중 **12.44%**, 최소분산 **11.63%**, SPY 보유 **13.92%**였다. 이 실험은 CAPM 전략의 초과수익을 입증하지 못했다. 성과에 맞춘 파라미터 탐색은 하지 않았다.

가격은 Yahoo 조정 종가, RF는 Kenneth French 일별 자료다. 원본과 가공 데이터 해시를 검증한다. 조정가격의 분수 단위 연구 원장이므로 실제 주식 수량·체결 이력은 아니다. 현재 선택한 ETF 집합과 사후 수정 데이터에 대한 한계가 있으며 세금·호가·시장 충격은 제외했다. 마지막 평가액에 최종 청산 비용은 적용하지 않는다.

백테스트와 가상 주문은 같은 추정·최적화 함수를 사용한다. 가상 주문은 백테스트 거래 CSV를 복사하지 않고 선택한 날짜의 비중을 다시 계산해 OMS에 전달한다. 실제 증권사 연결은 없다.

## 이력서 요약

- CAPM 기대수익과 공분산을 이용해 미국 섹터 ETF 9개의 일별 평균·분산 리밸런싱 시스템 구현.
- 체결 지연·거래비용을 반영한 2010~2025년 백테스트로 SPY·동일비중·최소분산 전략 성과 비교.
- 계산한 비중을 정수 주문으로 변환해 FastAPI·SQLite 가상 OMS의 매수·매도·체결·잔고 조회에 연결.

출처: [Yahoo Finance](https://finance.yahoo.com/quote/SPY/history/), [Kenneth French](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_factors.html).
