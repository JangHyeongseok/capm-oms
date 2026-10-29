# Daily CAPM Portfolio & OMS

미국 섹터 ETF 9개의 비중을 매일 계산하는 자산배분 백테스트와 별도의 모의 주문 관리 시스템이다.
핵심 흐름은 **과거 수익률 → CAPM 기대수익·공분산 → 평균·분산 비중 → 다음 거래일 리밸런싱**이다.

## 코드 읽는 순서

| 순서 | 파일·함수 | 하는 일 |
| --- | --- | --- |
| 1 | [run.py · run](research/run.py) | 데이터 읽기부터 백테스트 실행까지 전체 흐름 |
| 2 | [model.py · estimate](research/model.py) | 과거 252일로 CAPM 기대수익과 공분산 추정 |
| 3 | [model.py · optimize](research/model.py) | 기대수익 − 위험 벌점이 최대인 비중 계산 |
| 4 | [backtest.py · build_targets / simulate](research/backtest.py) | 일별 신호 생성, 매수·매도 차액과 비용·자산 기록 |
| 5 | [service.py · OrderService](oms/service.py) | 모의 주문의 중복 방지·체결·취소·상태 대사 |

이해하기 쉬운 변수명과 함수, 명시적인 계산 순서를 우선한다. 계산·회계 검증은 코드와 테스트에서 확인한다.

## 실행

Python 3.12 이상. 아래 명령은 프로젝트 루트에서 실행한다. 동봉 데이터로 실행할 때는 API 키나 데이터 다운로드가 필요 없다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,research]'

python -m research.run --overwrite
python -m pytest -q
```

성과는 터미널에 표시된다. 일별 자산·매매·비중 CSV와 지표 JSON은 `results/backtest/`에 저장되며 Git에서는 제외한다.
`--output results/experiment-02`로 실험 결과를 따로 저장할 수 있다. 데이터 수집은 [data.py](research/data.py), 설정은 [daily-capm.json](configs/daily-capm.json)에 있다.

거래 CSV를 다시 읽어 자산·비용을 검산하려면 `python -m research.audit`, 모의 주문을 시연하려면 `python -m oms.demo`를 실행한다.
주문 API가 필요하면 `python -m uvicorn oms.api:create_app --factory --host 127.0.0.1 --port 8000` 실행 후 `/docs`를 연다.

## 모델의 기준

- 대상: XLB(소재), XLE(에너지), XLF(금융), XLI(산업재), XLK(기술), XLP(필수소비재), XLU(유틸리티), XLV(헬스케어), XLY(경기소비재).
- 기대수익: `평균 RF + 베타 × 평균 시장 초과수익`을 연율화한다. 시장 대용치는 SPY다.
- 목적함수: `max μᵀw − λ/2 × wᵀΣw`. 비중 합 100%, 공매도 없음, ETF당 최대 25%, λ=5다. λ는 초기 실험 가정이다.
- 공분산: 252일 표본 공분산을 연율화하고 대각 행렬로 10% 축소한다.
- 시점: t일 종가로 비중 계산 → t+1일 종가 체결 → 이후 수익부터 새 비중에 반영한다.
- 비용: 매수·매도 금액 각각 0.05%. 비용 차감 후 투자금을 계산해 차입을 방지한다.

## 검증 결과와 범위

2010~2025년 동봉 데이터 기준 비용 차감 후 CAGR은 CAPM 평균·분산 **9.84%**, 동일비중 **12.44%**, 최소분산 **11.63%**, SPY 보유 **13.92%**였다. 이 실험은 CAPM 전략의 초과수익을 입증하지 못했다. 성과에 맞춘 파라미터 탐색은 하지 않았다.

테스트는 알려진 최적해, 미래 가격·RF 변조 시 과거 신호·자산 불변, 체결 지연, 수수료와 자산 회계를 검사한다. OMS는 중복 주문·부분 체결·타임아웃 후 대사·동시 요청을 검증한다.

가격은 Yahoo 조정 종가, RF는 Kenneth French 일별 자료다. 원본과 가공 데이터 해시를 검증한다. 조정가격의 분수 단위 연구 원장이므로 실제 주식 수량·체결 이력은 아니다. 현재 선택한 ETF 집합과 사후 수정 데이터에 대한 한계가 있으며 세금·호가·시장 충격은 제외했다. 마지막 평가액에 최종 청산 비용은 적용하지 않는다.

백테스트와 OMS는 독립 모듈이다. OMS는 정수 주식 지정가 매수와 수동 모의 체결만 지원하며, 백테스트 거래를 OMS나 실제 증권사로 전달하지 않는다.

## 이력서 요약

- CAPM 기대수익과 공분산을 이용해 미국 섹터 ETF 9개의 일별 평균·분산 리밸런싱 시스템 구현.
- 체결 지연·거래비용을 반영한 2010~2025년 백테스트로 SPY·동일비중·최소분산 전략 비교 및 미래 참조·회계 검증.
- FastAPI·SQLite 모의 OMS에 주문 중복 방지, 부분 체결, 응답 유실 후 상태 대사를 구현하고 자동 테스트로 검증.

출처: [Yahoo Finance](https://finance.yahoo.com/quote/SPY/history/), [Kenneth French](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_factors.html).
