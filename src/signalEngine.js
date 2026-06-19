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
    shortPeriod = 12, longPeriod = 26, rsiPeriod = 14, zigzagPct = 0.05,
    atrPeriod = 14, atrMultiplier = 1.5, riskReward = 1,
    volumePeriod = 20, volumeMultiplier = 1.2,
    scoreThreshold = 3, trendFilterPeriod = null,
  } = opts;
  const minNeeded = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  if (prices.length < minNeeded) {
    throw new Error(`신호 계산에 최소 ${minNeeded}개 데이터 포인트가 필요합니다 (현재 ${prices.length}개)`);
  }

  const emaShort = ema(prices, shortPeriod);
  const emaLong = ema(prices, longPeriod);
  const rsiSeries = rsi(prices, rsiPeriod);
  const atrSeries = atr(prices, atrPeriod);
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
  const target = position === "매수"
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
export function backtest(prices, opts = {}, volumes = null) {
  const {
    shortPeriod = 12, longPeriod = 26, rsiPeriod = 14, zigzagPct = 0.05,
    useStopLoss = true, useTarget = true,
    atrMultiplier, riskReward, scoreThreshold, trendFilterPeriod,
    volumePeriod, volumeMultiplier,
  } = opts;
  const minBars = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  const trades = [];
  let holding = false;
  let entryPrice = null;
  let activeStop = null;
  let activeTarget = null;
  let lastPosition = "관망";

  for (let i = minBars; i < prices.length; i++) {
    if (holding && useStopLoss && activeStop != null && prices[i] <= activeStop) {
      trades.push({ entryPrice, exitPrice: activeStop, returnPct: (activeStop - entryPrice) / entryPrice * 100, exitReason: "stop_loss" });
      holding = false;
      entryPrice = null;
      activeStop = null;
      activeTarget = null;
      continue;
    }
    if (holding && useTarget && activeTarget != null && prices[i] >= activeTarget) {
      trades.push({ entryPrice, exitPrice: activeTarget, returnPct: (activeTarget - entryPrice) / entryPrice * 100, exitReason: "take_profit" });
      holding = false;
      entryPrice = null;
      activeStop = null;
      activeTarget = null;
      continue;
    }

    const window = prices.slice(0, i + 1);
    const volWindow = volumes ? volumes.slice(0, i + 1) : null;
    let sig;
    try {
      sig = generateSignal(window, {
        shortPeriod, longPeriod, rsiPeriod, zigzagPct,
        atrMultiplier, riskReward, scoreThreshold, trendFilterPeriod,
        volumePeriod, volumeMultiplier,
      }, volWindow);
    } catch {
      continue;
    }

    if (!holding && sig.position === "매수") {
      holding = true;
      entryPrice = prices[i];
      activeStop = sig.stopLoss;
      activeTarget = sig.target ? sig.target[0] : null;
    } else if (holding && sig.position === "매도") {
      const exitPrice = prices[i];
      trades.push({ entryPrice, exitPrice, returnPct: (exitPrice - entryPrice) / entryPrice * 100, exitReason: "signal_flip" });
      holding = false;
      entryPrice = null;
      activeStop = null;
      activeTarget = null;
    }
    lastPosition = sig.position;
  }

  if (holding) {
    const exitPrice = prices[prices.length - 1];
    trades.push({ entryPrice, exitPrice, returnPct: (exitPrice - entryPrice) / entryPrice * 100, exitReason: "open_at_end" });
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
