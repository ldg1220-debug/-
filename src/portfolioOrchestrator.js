// A(박스권)/B(공격적 매매)/C(델타뉴트럴 펀딩아빗) 3버킷을 실제 계좌 구조에 묶어
// 운용하는 상위 오케스트레이터. 신호 생성(signalEngine)·리스크 감시(fundingGuard)·
// 자금 재배분(rebalancer) 로직은 이미 완성되어 있고, 이 파일은 그것들을 "언제 호출하고
// 어디로 주문/이체를 보낼지" 배선만 담당한다. 실제 주문/이체는 exchangeClient의
// 스텁을 통하므로, API 키 연동 전까지는 dryRun:true로 액션 로그만 쌓인다.

import { checkRisk } from "./fundingGuard.js";
import { planRebalance } from "./rebalancer.js";
import * as exchangeClient from "./exchangeClient.js";

// 전략/레그 ↔ 계좌·컨테이너 매핑 (사용자 확정 구조)
export const ACCOUNT_MAP = {
  A: { account: "sub", container: "perp2", role: "박스권 매매" },
  B: { account: "main", container: "perp2", role: "공격적 매매" },
  C_long: { account: "main", container: "spot", role: "델타뉴트럴 매수" },
  C_short: { account: "sub", container: "perp1", role: "델타뉴트럴 매도" },
};

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
