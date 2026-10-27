// 펀딩비 델타뉴트럴(숏 perp + 롱 spot) 포지션의 청산 보호 매커니즘.
//
// perp와 spot은 별도 계정/컨테이너라서 마진이 공유되지 않는다. 가격이 급등하면
// spot 쪽은 이익이 나지만 perp 숏은 그 계정 자체의 마진비율로만 평가되어 강제
// 청산될 수 있다. 이 모듈은 "거래소가 강제청산하기 전에" 마진비율을 감시해서
// 두 다리를 동시에 정리(perp 청산 + spot 매도)하는 의사결정 로직을 제공한다.

// 격리마진 기준 근사 청산가. side="short"이면 가격 상승 방향으로 청산.
export function computeLiquidationPrice({ entryPrice, leverage, side, maintenanceMarginRate = 0.05 }) {
  const buffer = 1 / leverage - maintenanceMarginRate;
  return side === "short" ? entryPrice * (1 + buffer) : entryPrice * (1 - buffer);
}

// 1 = 진입 시점 마진비율(100%), 0 이하면 거래소 강제청산 트리거 지점.
export function marginRatio({ entryPrice, markPrice, leverage, side }) {
  const dir = side === "short" ? -1 : 1;
  const pnlPct = (dir * (markPrice - entryPrice)) / entryPrice * leverage;
  return 1 + pnlPct;
}

// 마진비율 구간별 권고 액션.
// OK: 정상, REDUCE_LEVERAGE: 레버리지/포지션 축소 권고, EMERGENCY_UNWIND: 양 다리 즉시 동시 정리.
export function checkRisk({ entryPrice, markPrice, leverage, side, warnRatio = 0.4, criticalRatio = 0.2 }) {
  const ratio = marginRatio({ entryPrice, markPrice, leverage, side });
  if (ratio <= criticalRatio) return { action: "EMERGENCY_UNWIND", ratio };
  if (ratio <= warnRatio) return { action: "REDUCE_LEVERAGE", ratio };
  return { action: "OK", ratio };
}

// markPrices: 시계열 perp mark price(=spot price와 동일 자산 가정).
// 델타뉴트럴이므로 가격 변동 자체는 spot 롱이 perp 숏의 손익을 상계한다 — 따라서
// 정상 상태에서는 가격 변동 손익이 net 0이고, 수익원은 오직 펀딩비뿐이다.
// 문제는 "마진비율"이 perp 계정 단독으로 계산되기 때문에 spot의 이득이 자동으로
// perp 마진에 반영되지 않는다는 것: 가격이 급등하면 spot은 벌어도 perp는 그 계정
// 안에서 단독으로 청산당할 수 있다. EMERGENCY_UNWIND가 트리거되면 두 다리를 동시에
// 정리하고 현재가로 즉시 재진입(재헤지)한다고 가정 — 이때 실제 비용은 가격差 손익이
// 아니라 "정리+재오픈 왕복 수수료"뿐이다(가격 손익은 spot 매도 차익이 정확히 상쇄).
export function simulateGuardedFundingArb(markPrices, fundingRates, opts = {}) {
  const {
    leverage = 3,
    maintenanceMarginRate = 0.05,
    warnRatio = 0.4,
    criticalRatio = 0.2,
    rehedgeFeePct = 0.038 + 0.038, // perp 정리+재오픈 + spot 매도+재매수 taker 수수료 근사
  } = opts;

  const events = [];
  let entryPrice = markPrices[0];
  let fundingAcc = 0;
  let unwindCount = 0;
  let cumPct = 0;

  for (let i = 1; i < markPrices.length; i++) {
    const markPrice = markPrices[i];
    fundingAcc += fundingRates[i] != null ? fundingRates[i] * 100 : 0; // 숏이 수취

    const { action, ratio } = checkRisk({ entryPrice, markPrice, leverage, side: "short", warnRatio, criticalRatio });

    if (action === "EMERGENCY_UNWIND") {
      const tradePct = fundingAcc - rehedgeFeePct;
      cumPct += tradePct;
      events.push({ index: i, type: "EMERGENCY_UNWIND", ratio, markPrice, entryPrice, tradePct });
      unwindCount++;
      entryPrice = markPrice; // 재헤지: 현재가로 양 다리 재오픈
      fundingAcc = 0;
    } else if (action === "REDUCE_LEVERAGE") {
      events.push({ index: i, type: "WARN", ratio, markPrice });
    }
  }

  cumPct += fundingAcc; // 청산 없이 종료된 마지막 구간 펀딩분
  return { cumPct, unwindCount, events };
}
