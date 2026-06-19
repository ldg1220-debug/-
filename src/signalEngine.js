// 규칙 기반 매매 신호 엔진
// 입력: 시계열 가격 배열 [{t, price}, ...] (오래된 -> 최신 순)
// 출력: 결정론적 매수/매도/관망 신호 + 근거 지표값 (LLM이 임의로 만들어내지 않도록 고정)

export function sma(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

export function ema(values, period) {
  const out = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    if (i === period - 1) {
      prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
      out[i] = prev;
    } else if (i >= period) {
      prev = values[i] * k + prev * (1 - k);
      out[i] = prev;
    }
  }
  return out;
}

// ATR (Average True Range) 근사치 — 일/시간봉 종가만 있는 경우 종가간 변동폭을
// True Range로 사용한다 (실제 OHLC 고저폭보다는 보수적으로 작게 잡힐 수 있음).
export function atr(values, period = 14) {
  const tr = values.map((v, i) => (i === 0 ? 0 : Math.abs(v - values[i - 1])));
  const out = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let avg = tr.slice(1, period + 1).reduce((a, b) => a + b, 0) / period;
  out[period] = avg;
  for (let i = period + 1; i < values.length; i++) {
    avg = (avg * (period - 1) + tr[i]) / period;
    out[i] = avg;
  }
  return out;
}

// Kaufman's Efficiency Ratio — 추세/횡보 판별용.
// ADX는 OHLC 고저가가 필요하지만 이 엔진은 종가 시계열만 받으므로 ADX를 쓸 수 없다.
// ER = |순변화량| / 변화량 절대값의 합 (0~1). 1에 가까우면 한 방향으로 곧게 움직인
// "추세장", 0에 가까우면 오르락내리락만 반복한 "횡보장"으로 본다.
export function efficiencyRatio(values, period = 14) {
  const out = new Array(values.length).fill(null);
  for (let i = period; i < values.length; i++) {
    const netChange = Math.abs(values[i] - values[i - period]);
    let volatility = 0;
    for (let k = i - period + 1; k <= i; k++) volatility += Math.abs(values[k] - values[k - 1]);
    out[i] = volatility === 0 ? 0 : netChange / volatility;
  }
  return out;
}

// 거래량 확인: 현재 거래량이 평균 대비 기준치 이상이면 신호에 신뢰도를 더한다.
export function volumeConfirmed(volumes, period = 20, multiplier = 1.2) {
  if (!volumes || volumes.length <= period) return null;
  const avg = sma(volumes, period);
  const last = volumes.length - 1;
  if (avg[last] == null) return null;
  return volumes[last] > avg[last] * multiplier;
}

// Wilder's RSI
export function rsi(values, period = 14) {
  const out = new Array(values.length).fill(null);
  if (values.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  let avgGain = gain / period, avgLoss = loss / period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    const g = d > 0 ? d : 0, l = d < 0 ? -d : 0;
    avgGain = (avgGain * (period - 1) + g) / period;
    avgLoss = (avgLoss * (period - 1) + l) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

// 지그재그 스윙 포인트 탐색 (엘리엇 파동 카운트 / 피보나치 기준점 추출용)
export function zigzag(values, pctThreshold = 0.05) {
  if (values.length < 2) return [];
  const pivots = [{ index: 0, price: values[0] }];
  let lastExtremeIdx = 0;
  let lastExtremeVal = values[0];
  let direction = null; // 'up' | 'down'

  for (let i = 1; i < values.length; i++) {
    const v = values[i];
    if (direction === null) {
      if (Math.abs(v - lastExtremeVal) / lastExtremeVal >= pctThreshold) {
        direction = v > lastExtremeVal ? "up" : "down";
        lastExtremeIdx = i;
        lastExtremeVal = v;
      }
      continue;
    }
    if (direction === "up") {
      if (v >= lastExtremeVal) {
        lastExtremeVal = v;
        lastExtremeIdx = i;
      } else if ((lastExtremeVal - v) / lastExtremeVal >= pctThreshold) {
        pivots.push({ index: lastExtremeIdx, price: lastExtremeVal });
        direction = "down";
        lastExtremeVal = v;
        lastExtremeIdx = i;
      }
    } else {
      if (v <= lastExtremeVal) {
        lastExtremeVal = v;
        lastExtremeIdx = i;
      } else if ((v - lastExtremeVal) / lastExtremeVal >= pctThreshold) {
        pivots.push({ index: lastExtremeIdx, price: lastExtremeVal });
        direction = "up";
        lastExtremeVal = v;
        lastExtremeIdx = i;
      }
    }
  }
  pivots.push({ index: lastExtremeIdx, price: lastExtremeVal });
  return pivots;
}

export function fibonacciLevels(low, high) {
  const range = high - low;
  const ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const retracement = {};
  ratios.forEach((r) => { retracement[r] = high - range * r; });
  // 확장 레벨 (목표가 산정용)
  const extension = { 1.272: high + range * 0.272, 1.618: high + range * 0.618 };
  return { retracement, extension, low, high };
}

// 단순화된 엘리엇 파동 추정: 지그재그 스윙 개수를 5(추진)/3(조정) 패턴에 매핑.
// 정식 엘리엇 룰(파동 간 비율, 겹침 금지 등)은 검증하지 않는 휴리스틱이므로
// "추정"으로만 사용한다.
export function elliottWaveHint(pivots) {
  const n = pivots.length;
  if (n < 3) return { wavePosition: "데이터 부족", waveCount: n };
  const posInCycle = ((n - 1) % 8) + 1;
  const label = posInCycle <= 5 ? `추진 ${posInCycle}파 추정` : `조정 ${"ABC"[posInCycle - 6]}파 추정`;
  const lastDir = pivots[n - 1].price > pivots[n - 2].price ? "상승" : "하락";
  return { wavePosition: label, lastSwingDirection: lastDir, waveCount: n };
}

// 메인 신호 생성: 종가 시계열을 받아 결정론적 매매 신호를 반환
export function generateSignal(prices, opts = {}, volumes = null) {
  const {
    shortPeriod = 8, longPeriod = 21, rsiPeriod = 14, zigzagPct = 0.05,
    atrPeriod = 14, atrMultiplier = 2, riskReward = 1.5,
    volumePeriod = 20, volumeMultiplier = 1.2,
    scoreThreshold = 3, trendFilterPeriod = null,
    erPeriod = 14, erTrendThreshold = 0.3, trailMultiplier = 1.5,
  } = opts;
  const minNeeded = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  if (prices.length < minNeeded) {
    throw new Error(`신호 계산에 최소 ${minNeeded}개 데이터 포인트가 필요합니다 (현재 ${prices.length}개)`);
  }

  const emaShort = ema(prices, shortPeriod);
  const emaLong = ema(prices, longPeriod);
  const rsiSeries = rsi(prices, rsiPeriod);
  const atrSeries = atr(prices, atrPeriod);
  const erSeries = efficiencyRatio(prices, erPeriod);
  const pivots = zigzag(prices, zigzagPct);
  const wave = elliottWaveHint(pivots);

  const last = prices.length - 1;
  const curPrice = prices[last];
  const curEmaShort = emaShort[last];
  const curEmaLong = emaLong[last];
  const curRsi = rsiSeries[last];
  const prevEmaShort = emaShort[last - 1];
  const prevEmaLong = emaLong[last - 1];

  const trendUp = curEmaShort > curEmaLong;
  const goldenCross = prevEmaShort <= prevEmaLong && curEmaShort > curEmaLong;
  const deadCross = prevEmaShort >= prevEmaLong && curEmaShort < curEmaLong;

  // 가장 최근 스윙 구간으로 피보나치 레벨 계산
  const recentPivots = pivots.slice(-2);
  let fib = null;
  if (recentPivots.length === 2) {
    const [a, b] = recentPivots;
    fib = fibonacciLevels(Math.min(a.price, b.price), Math.max(a.price, b.price));
  }

  let nearFibSupport = false, nearFibResistance = false;
  if (fib) {
    const tolerance = (fib.high - fib.low) * 0.03;
    for (const r of [0.382, 0.5, 0.618]) {
      const lvl = fib.retracement[r];
      if (Math.abs(curPrice - lvl) <= tolerance) {
        if (trendUp) nearFibSupport = true; else nearFibResistance = true;
      }
    }
  }

  // 시장 국면 판별: ER이 높으면(추세장) 추세추종 모드 - 목표가를 고정하지 않고
  // 트레일링 스탑으로 수익을 최대한 끌고 간다. ER이 낮으면(횡보장) 단타 모드 -
  // 기존처럼 손익비를 고정한 빠른 익절/손절을 사용한다. 단일 종목/단일 전략에
  // 고정하지 않고 같은 자산이라도 구간에 따라 모드를 전환하기 위한 장치다.
  const curEr = erSeries[last];
  const regime = curEr != null && curEr >= erTrendThreshold ? "추세" : "횡보";

  const reasons = [];
  let score = 0; // -3..+3

  if (trendUp) { score += 1; reasons.push(`EMA${shortPeriod} > EMA${longPeriod} (상승 추세)`); }
  else { score -= 1; reasons.push(`EMA${shortPeriod} < EMA${longPeriod} (하락 추세)`); }

  if (goldenCross) { score += 1; reasons.push("골든크로스 발생"); }
  if (deadCross) { score -= 1; reasons.push("데드크로스 발생"); }

  if (curRsi != null) {
    if (curRsi < 30) { score += 1; reasons.push(`RSI ${curRsi.toFixed(1)} (과매도)`); }
    else if (curRsi > 70) { score -= 1; reasons.push(`RSI ${curRsi.toFixed(1)} (과매수)`); }
  }

  if (nearFibSupport) { score += 1; reasons.push("피보나치 지지선 근접"); }
  if (nearFibResistance) { score -= 1; reasons.push("피보나치 저항선 근접"); }

  const volConfirmed = volumeConfirmed(volumes, volumePeriod, volumeMultiplier);
  if (volConfirmed === true) {
    if (score > 0) { score += 1; reasons.push("거래량 동반 (신호 확인)"); }
    else if (score < 0) { score -= 1; reasons.push("거래량 동반 (신호 확인)"); }
  } else if (volConfirmed === false && score !== 0) {
    score = score > 0 ? score - 1 : score + 1;
    reasons.push("거래량 부족 (신호 신뢰도 하향)");
  }

  reasons.push(curEr != null ? `효율성비율 ${curEr.toFixed(2)} (${regime})` : `효율성비율 산출불가 (${regime})`);

  // 장기 추세 필터: 큰 흐름과 반대되는 신호는 걸러내 승률을 높인다 (역추세 매매 차단).
  let longTrendUp = null;
  if (trendFilterPeriod) {
    const emaLongTerm = ema(prices, trendFilterPeriod);
    const curLongTerm = emaLongTerm[last];
    if (curLongTerm != null) {
      longTrendUp = curPrice > curLongTerm;
      if (longTrendUp && score < 0) { score += 1; reasons.push(`장기추세(EMA${trendFilterPeriod}) 상승 중 - 매도 신호 약화`); }
      if (!longTrendUp && score > 0) { score -= 1; reasons.push(`장기추세(EMA${trendFilterPeriod}) 하락 중 - 매수 신호 약화`); }
    }
  }

  let position = "관망";
  if (score >= scoreThreshold) position = "매수";
  else if (score <= -scoreThreshold) position = "매도";

  // ATR 기반 동적 손절/목표가: 변동성이 클수록 손절폭도 넓어진다 (정액 스윙 고저점 대신).
  const curAtr = atrSeries[last];
  const riskDistance = curAtr != null ? curAtr * atrMultiplier : curPrice * 0.02;
  const stopLoss = position === "매수" ? curPrice - riskDistance
    : position === "매도" ? curPrice + riskDistance
    : null;
  // 추세 모드에서는 목표가를 정해두지 않고(null) 백테스트에서 트레일링 스탑으로 관리한다.
  const target = regime === "추세" ? null
    : position === "매수"
    ? [curPrice + riskDistance * riskReward, curPrice + riskDistance * riskReward * 1.5]
    : position === "매도"
    ? [curPrice - riskDistance * riskReward, curPrice - riskDistance * riskReward * 1.5]
    : null;

  return {
    position,
    confidence: Math.min(10, Math.abs(score) * 2 + 1),
    entry: curPrice,
    stopLoss,
    target,
    reasons,
    indicators: {
      price: curPrice,
      emaShort: curEmaShort,
      emaLong: curEmaLong,
      rsi: curRsi,
      atr: curAtr,
      efficiencyRatio: curEr,
      regime,
      trend: trendUp ? "상승" : "하락",
      goldenCross,
      deadCross,
      fibonacci: fib,
      elliott: wave,
      volumeConfirmed: volConfirmed,
      longTrendUp,
    },
  };
}

// 워크포워드 백테스트: 매수 신호 진입, (매도 신호 | 손절가 터치 | 목표가 터치) 시 청산.
// 손절/목표가를 실제로 체결에 반영해야 ATR 기반 리스크관리 효과를 검증할 수 있다.
//
// 국면별 청산 방식 분리 (단타 vs 추세매매):
//  - 진입 시점의 시장 국면(regime)이 "횡보"면 기존처럼 ATR 기반 고정 손절/목표가로
//    빠르게 익절/손절하는 단타 방식을 그대로 쓴다.
//  - "추세"면 목표가를 두지 않고, 진입 후 갈아탄 최고가(롱) 대비 ATR*trailMultiplier
//    만큼 따라오는 트레일링 스탑만 사용해 추세가 꺾이기 전까지 수익을 최대한 끌고 간다.
//    같은 엔진/같은 자산이라도 구간별 국면에 따라 자동으로 전략을 바꾸는 자율 전환 장치.
//  - makerFeePct/takerFeePct: 거래소 메이커/테이커 수수료(예: 0.015% / 0.036%). 진입은
//    신호 발생 즉시 체결되는 시장가(테이커)로 가정한다. 청산은 take_profit만 목표가에
//    걸어둔 리밋 주문(메이커)으로, 그 외(손절/트레일링/신호전환/만기청산)는 즉시 체결이
//    필요한 시장가(테이커)로 가정해 거래별로 다른 수수료를 차감한다. 레버리지는 수익률에
//    단순 배율로 곱해지므로(강제청산 위험은 별도 고려 필요) leverage로 적용한다.
export function backtest(prices, opts = {}, volumes = null) {
  const {
    shortPeriod = 8, longPeriod = 21, rsiPeriod = 14, zigzagPct = 0.05,
    useStopLoss = true, useTarget = true,
    atrPeriod = 14, atrMultiplier, riskReward, scoreThreshold, trendFilterPeriod,
    volumePeriod, volumeMultiplier, erPeriod, erTrendThreshold,
    trailMultiplier = 1.5, makerFeePct = 0.015, takerFeePct = 0.036, leverage = 1,
  } = opts;
  const minBars = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  const trades = [];
  let holding = false;
  let entryPrice = null;
  let activeStop = null;
  let activeTarget = null;
  let entryRegime = null;
  let highestSinceEntry = null;
  let lastPosition = "관망";

  const atrSeries = atr(prices, atrPeriod);

  const closeTrade = (exitPrice, exitReason) => {
    const grossPct = (exitPrice - entryPrice) / entryPrice * 100 * leverage;
    const exitFeePct = exitReason === "take_profit" ? makerFeePct : takerFeePct;
    const feePct = takerFeePct + exitFeePct; // 진입(테이커) + 청산(주문유형별)
    const returnPct = grossPct - feePct;
    trades.push({ entryPrice, exitPrice, returnPct, exitReason, regime: entryRegime, feePct });
    holding = false;
    entryPrice = null;
    activeStop = null;
    activeTarget = null;
    entryRegime = null;
    highestSinceEntry = null;
  };

  for (let i = minBars; i < prices.length; i++) {
    if (holding && entryRegime === "추세") {
      highestSinceEntry = Math.max(highestSinceEntry, prices[i]);
      const curAtr = atrSeries[i];
      if (curAtr != null) activeStop = Math.max(activeStop, highestSinceEntry - curAtr * trailMultiplier);
    }
    if (holding && useStopLoss && activeStop != null && prices[i] <= activeStop) {
      closeTrade(activeStop, entryRegime === "추세" ? "trailing_stop" : "stop_loss");
      continue;
    }
    if (holding && useTarget && activeTarget != null && prices[i] >= activeTarget) {
      closeTrade(activeTarget, "take_profit");
      continue;
    }

    const window = prices.slice(0, i + 1);
    const volWindow = volumes ? volumes.slice(0, i + 1) : null;
    let sig;
    try {
      sig = generateSignal(window, {
        shortPeriod, longPeriod, rsiPeriod, zigzagPct,
        atrPeriod, atrMultiplier, riskReward, scoreThreshold, trendFilterPeriod,
        volumePeriod, volumeMultiplier, erPeriod, erTrendThreshold, trailMultiplier,
      }, volWindow);
    } catch {
      continue;
    }

    if (!holding && sig.position === "매수") {
      holding = true;
      entryPrice = prices[i];
      entryRegime = sig.indicators.regime;
      activeStop = sig.stopLoss;
      activeTarget = entryRegime === "추세" ? null : (sig.target ? sig.target[0] : null);
      highestSinceEntry = entryRegime === "추세" ? prices[i] : null;
    } else if (holding && sig.position === "매도") {
      closeTrade(prices[i], "signal_flip");
    }
    lastPosition = sig.position;
  }

  if (holding) {
    closeTrade(prices[prices.length - 1], "open_at_end");
  }

  const wins = trades.filter((t) => t.returnPct > 0).length;
  const totalReturnPct = trades.reduce((acc, t) => acc + t.returnPct, 0);
  const buyHoldReturnPct = (prices[prices.length - 1] - prices[minBars]) / prices[minBars] * 100;

  return {
    trades,
    tradeCount: trades.length,
    winRate: trades.length ? (wins / trades.length) * 100 : 0,
    totalReturnPct,
    buyHoldReturnPct,
    finalPosition: lastPosition,
  };
}
