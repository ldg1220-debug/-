// A(박스권)/B(공격적 매매)/C(델타뉴트럴 펀딩아빗) 3버킷을 실제 계좌 구조에 묶어
// 운용하는 상위 오케스트레이터. 신호 생성(signalEngine)·리스크 감시(fundingGuard)·
// 자금 재배분(rebalancer) 로직은 이미 완성되어 있고, 이 파일은 그것들을 "언제 호출하고
// 어디로 주문/이체를 보낼지" 배선만 담당한다. 실제 주문/이체는 exchangeClient의
// 스텁을 통하므로, API 키 연동 전까지는 dryRun:true로 액션 로그만 쌓인다.

import { checkRisk } from "./fundingGuard.js";
import { planRebalance } from "./rebalancer.js";
import { momentumChaseSignal } from "./signalEngine.js";
import * as exchangeClient from "./exchangeClient.js";

// 전략/레그 ↔ 계좌·컨테이너 매핑 (사용자 확정 구조)
export const ACCOUNT_MAP = {
  A: { account: "sub", container: "perp2", role: "박스권 매매" },
  B: { account: "main", container: "perp2", role: "공격적 매매" },
  C_long: { account: "main", container: "spot", role: "델타뉴트럴 매수" },
  C_short: { account: "sub", container: "perp1", role: "델타뉴트럴 매도" },
};

// B버킷(공격적 매매=모멘텀추격) 거래 대상은 메이저로 한정한다. 알트는 백테스트
// 기간을 바꿔도(OOS) 개별 종목 순위가 뒤집히고, 변동성 폭발 빈도 같은 사전
// 지표로도 미래 수익률을 예측할 수 없음을 검증 완료(BACKTEST_BASELINE.md
// "모멘텀추격 종목별 승패 원인 분석" 참조) — 메이저만 그룹 단위로 양(+) 패턴이
// 두 기간 모두 재현됐다.
export const MOMENTUM_CHASE_SYMBOLS = ["BTC", "ETH", "HYPE", "BNB"];

// 3) B버킷 모멘텀추격 신호 사이클 — 매 캔들마다 메이저별로 호출. positions는
// 심볼별 현재 보유 상태({side, entryPrice, entryIndex, extremeSinceEntry} | null)를
// 호출자가 들고 있다가 넘겨준다(이 함수는 상태를 들고 있지 않고 매번 입력받음).
// candles: { [symbol]: { prices, volumes } } — 심볼별 최신 시점까지의 시계열.
export function runMomentumChaseCycle(candles, positions, opts = {}) {
  const { dryRun = true, ...signalOpts } = opts;
  const results = {};

  for (const symbol of MOMENTUM_CHASE_SYMBOLS) {
    const data = candles[symbol];
    if (!data) continue;
    const position = positions[symbol] ?? null;
    const sig = momentumChaseSignal(data.prices, data.volumes ?? null, position, signalOpts);

    const orders = [];
    if (sig.action === "enter_long" || sig.action === "enter_short") {
      orders.push({ account: ACCOUNT_MAP.B.account, container: ACCOUNT_MAP.B.container, op: "placeOrder", symbol, side: sig.action === "enter_long" ? "buy" : "sell" });
    } else if (sig.action === "exit") {
      orders.push({ account: ACCOUNT_MAP.B.account, container: ACCOUNT_MAP.B.container, op: "closePosition", symbol });
    }

    if (!dryRun) {
      for (const o of orders) {
        if (o.op === "placeOrder") exchangeClient.placeOrder(o.account, o.container, { symbol: o.symbol, side: o.side, type: "market" });
        else exchangeClient.closePosition(o.account, o.container, o.symbol);
      }
    }

    results[symbol] = { ...sig, orders };
  }

  return results;
}

// 1) 델타뉴트럴 리스크 감시 — 매 사이클(예: 매 캔들/매 N분)마다 호출.
// markPrice/entryPrice/leverage는 exchangeClient.getPosition(C_short)에서 채워질 값.
export function monitorDeltaNeutral({ entryPrice, markPrice, leverage, warnRatio, criticalRatio }, { dryRun = true } = {}) {
  const { action, ratio } = checkRisk({ entryPrice, markPrice, leverage, side: "short", warnRatio, criticalRatio });

  if (action === "EMERGENCY_UNWIND") {
    const orders = [
      { account: ACCOUNT_MAP.C_short.account, container: ACCOUNT_MAP.C_short.container, op: "closePosition" },
      { account: ACCOUNT_MAP.C_long.account, container: ACCOUNT_MAP.C_long.container, op: "closePosition" },
    ];
    if (!dryRun) {
      for (const o of orders) exchangeClient.closePosition(o.account, o.container);
    }
    return { action, ratio, orders };
  }
  return { action, ratio, orders: [] };
}

// 2) 주기적 리밸런싱 — 예: 주 1회 호출. pnlA/pnlB/cAvailable은
// exchangeClient.getBalance(...)로 구간 시작/종료 잔고 차이를 계산해 채울 값.
export function runRebalanceCycle({ pnlA, pnlB, cAvailable }, { dryRun = true } = {}) {
  const { actions, netAB } = planRebalance({ pnlA, pnlB, cAvailable });

  const transfers = actions
    .filter((a) => a.from && a.to)
    .map((a) => ({ ...a, resolvedFrom: resolveLeg(a.from), resolvedTo: resolveLeg(a.to) }));

  if (!dryRun) {
    for (const t of transfers) {
      if (!t.resolvedFrom || !t.resolvedTo) continue; // "trading pool" 같은 가상 노드는 실제 이체 대상 아님
      exchangeClient.transfer(
        t.resolvedFrom.account, t.resolvedFrom.container,
        t.resolvedTo.account, t.resolvedTo.container,
        "USDT", t.amount,
      );
    }
  }

  return { netAB, actions, transfers };
}

// rebalancer가 쓰는 사람이 읽기 좋은 라벨("Main.perp2(B)" 등)을
// 실제 account/container 식별자로 역매핑.
function resolveLeg(label) {
  if (label === "Main.perp2(B)") return ACCOUNT_MAP.B;
  if (label === "Sub.perp2(A)") return ACCOUNT_MAP.A;
  if (label === "Main.spot(C_long)") return ACCOUNT_MAP.C_long;
  if (label === "Sub.perp1(C_short)") return ACCOUNT_MAP.C_short;
  return null; // "trading pool" / "trading pool surplus" 등 가상 노드
}
