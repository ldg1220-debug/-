// 규칙 기반 매매 신호 엔진
// 입력: 시계열 가격 배열 [{t, price}, ...] (오래된 -> 최신 순)
// 출력: 결정론적 매수/매도/관망 신호 + 근거 지표값 (LLM이 임의로 만들어내지 않도록 고정)

// computeSeries/signalAt이 공유하는 기본값. 한쪽만 고치고 다른 쪽을 빠뜨려
// 결과가 갈라지는 것을 막기 위해 상수로 분리한다.
const DEFAULT_SHORT_PERIOD = 8;
const DEFAULT_LONG_PERIOD = 21;

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

// 리스크 가드(서킷브레이커) — 전략 신호와 완전히 분리된 계좌 보호 레이어.
// backtest()/boxBreakoutBacktest()의 트레이드 루프에 끼워 "거래 가능 여부"만
// 판단한다. 신호의 승패 검증과는 무관하게 어떤 전략을 붙이든 동작한다.
// 캘린더 일(day) 경계가 필요한 "당일 손실 한도"는 이 두 함수가 타임스탬프를
// 받지 않아(인덱스 기반 종가 시계열만 받음) 구현하지 않음 — 대신 연속손절
// 쿨다운과 MDD 하드정지(쿨다운 후 자동 재개)로 꼬리손실을 막는다.
export function createRiskGuard(opts = {}) {
  const {
    riskGuardEnabled = false,
    maxDrawdownLimit = 0.15,      // 고점 대비 이 낙폭(%) 도달 시 정지
    mddCooldownBars = 40,         // MDD 정지 후 이만큼 바가 지나면 자동 재개
    maxConsecutiveLosses = 5,     // 연속 손절 이 횟수 도달 시 쿨다운 진입
    consecutiveCooldownBars = 20, // 연속손절 쿨다운 길이(바 수)
  } = opts;
  let equity = 1, peakEquity = 1;
  let consecutiveLosses = 0, lossCooldownRemaining = 0;
  let mddHalted = false, mddCooldownRemaining = 0;
  const events = [];
  return {
    enabled: riskGuardEnabled,
    onBarTick(index) {
      if (!riskGuardEnabled) return;
      if (lossCooldownRemaining > 0) lossCooldownRemaining--;
      if (mddHalted) {
        mddCooldownRemaining--;
        if (mddCooldownRemaining <= 0) {
          mddHalted = false;
          events.push({ index, type: "mdd_resume" });
        }
      }
    },
    canTrade() {
      if (!riskGuardEnabled) return true;
      return !mddHalted && lossCooldownRemaining <= 0;
    },
    registerTrade(returnPct, index) {
      if (!riskGuardEnabled) return;
      equity *= 1 + returnPct / 100;
      if (equity > peakEquity) peakEquity = equity;
      const dd = peakEquity > 0 ? (peakEquity - equity) / peakEquity : 0;
      if (!mddHalted && dd >= maxDrawdownLimit) {
        mddHalted = true;
        mddCooldownRemaining = mddCooldownBars;
        events.push({ index, type: "mdd_halt", dd });
      }
      if (returnPct < 0) {
        consecutiveLosses++;
        if (consecutiveLosses >= maxConsecutiveLosses && lossCooldownRemaining <= 0) {
          lossCooldownRemaining = consecutiveCooldownBars;
          events.push({ index, type: "consecutive_loss_cooldown", count: consecutiveLosses });
        }
      } else {
        consecutiveLosses = 0;
      }
    },
    getEvents() { return events; },
    getEquity() { return equity; },
  };
}

// 거래량 확인 본체. volumes가 prices보다 짧을 수 있으므로(예: 거래량 데이터 누락)
// last가 volumes 범위를 벗어나면 null을 반환해 인덱싱 오류를 막는다.
function volumeConfirmedAt(volumes, volAvg, last, multiplier) {
  if (!volumes || last >= volumes.length || !volAvg || volAvg[last] == null) return null;
  return volumes[last] > volAvg[last] * multiplier;
}

// 거래량 확인: 현재 거래량이 평균 대비 기준치 이상이면 신호에 신뢰도를 더한다.
export function volumeConfirmed(volumes, period = 20, multiplier = 1.2) {
  if (!volumes || volumes.length <= period) return null;
  const avg = sma(volumes, period);
  return volumeConfirmedAt(volumes, avg, volumes.length - 1, multiplier);
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

// 지그재그 알고리즘 본체. 확정된 피벗(confirmed)과 별개로 "아직 확정 안 된
// 진행 중인 극값"을 매 인덱스마다 기록한다(pendingIdx/pendingVal). 이 진행 중
// 극값은 배열이 어디서 끝나는지(미래 데이터)에 따라 달라지는 비인과적 값이라,
// 시점 i에서의 zigzag(prices.slice(0,i+1)) 결과(=confirmedAt<=i인 확정 피벗 +
// 그 시점의 진행 중인 극값)를 인덱스로 조회해 재현하려면 별도로 추적해야 한다.
function zigzagPending(values, pctThreshold = 0.05) {
  const confirmed = [{ index: 0, price: values[0], confirmedAt: 0 }];
  const pendingIdx = new Array(values.length).fill(0);
  const pendingVal = new Array(values.length).fill(values[0]);
  if (values.length < 2) return { confirmed, pendingIdx, pendingVal };
  let lastExtremeIdx = 0;
  let lastExtremeVal = values[0];
  let direction = null;

  for (let i = 1; i < values.length; i++) {
    const v = values[i];
    if (direction === null) {
      if (Math.abs(v - lastExtremeVal) / lastExtremeVal >= pctThreshold) {
        direction = v > lastExtremeVal ? "up" : "down";
        lastExtremeIdx = i;
        lastExtremeVal = v;
      }
    } else if (direction === "up") {
      if (v >= lastExtremeVal) {
        lastExtremeVal = v;
        lastExtremeIdx = i;
      } else if ((lastExtremeVal - v) / lastExtremeVal >= pctThreshold) {
        confirmed.push({ index: lastExtremeIdx, price: lastExtremeVal, confirmedAt: i });
        direction = "down";
        lastExtremeVal = v;
        lastExtremeIdx = i;
      }
    } else {
      if (v <= lastExtremeVal) {
        lastExtremeVal = v;
        lastExtremeIdx = i;
      } else if ((v - lastExtremeVal) / lastExtremeVal >= pctThreshold) {
        confirmed.push({ index: lastExtremeIdx, price: lastExtremeVal, confirmedAt: i });
        direction = "up";
        lastExtremeVal = v;
        lastExtremeIdx = i;
      }
    }
    pendingIdx[i] = lastExtremeIdx;
    pendingVal[i] = lastExtremeVal;
  }
  return { confirmed, pendingIdx, pendingVal };
}

// 지그재그 스윙 포인트 탐색 (엘리엇 파동 카운트 / 피보나치 기준점 추출용).
// zigzagPending()의 얇은 래퍼 — 두 함수가 같은 피벗 판정 로직을 따로 들고
// 있다가 한쪽만 고쳐 결과가 갈라지는 것을 막기 위해 본체를 공유한다.
export function zigzag(values, pctThreshold = 0.05) {
  if (values.length < 2) return [];
  const { confirmed, pendingIdx, pendingVal } = zigzagPending(values, pctThreshold);
  const last = values.length - 1;
  const pivots = confirmed.map(({ index, price }) => ({ index, price }));
  pivots.push({ index: pendingIdx[last], price: pendingVal[last] });
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

// 타임프레임별 검증된 파라미터 프리셋. 일봉과 시간봉은 변동성/신호 빈도가 달라
// 같은 파라미터를 쓰면 한쪽이 무너진다(실측: 일봉 튠 파라미터를 시간봉에 그대로
// 쓰면 승률이 47.7%로 하락). opts에 명시된 값은 프리셋을 덮어쓴다.
// 아래 값은 edgeX v1 실거래소 데이터(20종목, 일봉 최대 700봉/시간봉 91일)에
// 실제 수수료(taker 0.038%/maker 0.018%)를 반영해 그리드서치로 재검증한 결과다.
// trailMultiplier=1.5는 추세 거래의 60%가 3일 이내 트레일링 스탑에 털리고(승률 13%),
// 살아남은 거래도 청산 후 평균 수십%p의 추가 상승을 놓치는 문제가 실측됐다
// (예: UNI 청산후 +46.2%p, XRP +117.6%p 추가 상승). trailMultiplier=2.5로 넓히면
// 조기 손절 비율이 60.7%->48%로 줄고 총수익도 +327%->+546%로 늘어 채택.
// 일봉보다 짧은 타임프레임(4시간/30분/5분)에서도 추세추종이 통하는지 edgeX
// 실데이터로 검증한 결과(4시간=180일/30분=60일/5분=21일치, 20종목, 실수수료
// 반영): 모두 양의 누적수익을 냈고 기간이 짧을수록 거래빈도가 크게 늘어
// (4h n=209 -> 30m n=445 -> 5m n=1566) 단타용 추세추종 옵션으로 추가.
// 이후 더 넓은 그리드(144개 조합)로 재검증: 4h/5m는 기존 값이 이미 상위권이라
// 유지, 30분봉은 breakoutLookback=30/trailMultiplier=3/trendAtrMultiplier=2.5가
// 더 우수해(exclTop 97.17%->125.24%) 교체. 단, 일봉 결과는 전반부 거래(특히
// XRP 1건 +313.6%p)에 총수익의 절반 이상이 쏠려 있고 전후반 기간을 나누면
// 후반부는 오히려 -24.28%로 단일 구간/단일거래 의존도가 커서 과신은 금물.
// 5분봉도 전반부 +7.96% vs 후반부 +57.87%로 기간별 편차가 크다.
// 일봉은 추세 진입 시 ER상승+신고가/신저가 돌파(breakoutLookback=20)까지
// 요구해 표본이 너무 적었다(30종목 합산 n=94, 후반부 -46.65%로 손실 전환).
// breakoutLookback=5/trendScoreThreshold=1/erTrendThreshold=0.1/trailMultiplier=2로
// 진입 문턱을 낮추자 거래수가 94->198건으로 늘면서 후반부도 -46.65%->+49.90%로
// 손실에서 양전환됐고 견고성(exclTop3, 상위3거래 제외 수익)도 86%->351%로 개선돼 채택.
// 과거엔 ER<erTrendThreshold(비추세 구간)에서도 ATR 고정 손절/목표가로 진입하는
// "횡보" 모드가 있었으나, 같은 평균회귀 컨셉을 더 정교하게 구현한 박스권 전용 엔진
// boxBreakoutBacktest()가 실측으로 압도적으로 우월함이 확인되어(동일 구간·레버리지
// 비교: 횡보 n=5 total=-0.07% vs 박스권 n=112 total=+3.80%) 폐기했다. 이제 이 엔진은
// 추세 추종 전용이며, 비추세 구간에서는 관망한다.
const TIMEFRAME_PRESETS = {
  daily: {
    shortPeriod: 10, longPeriod: 24,
    erTrendThreshold: 0.1, breakoutLookback: 5,
    trendScoreThreshold: 1, trendAtrMultiplier: 1.5, trailMultiplier: 2,
  },
  hourly: {
    shortPeriod: 8, longPeriod: 21, erTrendThreshold: 0.7,
  },
  fourHour: {
    shortPeriod: 8, longPeriod: 21,
    erTrendThreshold: 0.2, breakoutLookback: 20,
    trendScoreThreshold: 2, trendAtrMultiplier: 2, trailMultiplier: 1.5,
  },
  thirtyMin: {
    shortPeriod: 8, longPeriod: 21,
    erTrendThreshold: 0.2, breakoutLookback: 30,
    trendScoreThreshold: 2, trendAtrMultiplier: 2.5, trailMultiplier: 3.5,
  },
  fiveMin: {
    shortPeriod: 8, longPeriod: 21,
    erTrendThreshold: 0.2, breakoutLookback: 20,
    trendScoreThreshold: 2, trendAtrMultiplier: 1.5, trailMultiplier: 1.5,
  },
};

function withTimeframePreset(opts) {
  const preset = TIMEFRAME_PRESETS[opts.timeframe] || {};
  return { ...preset, ...opts };
}

// 지표 시계열 일괄 계산 — ema/rsi/atr/er/zigzag는 모두 과거 값만 참조하는 인과적(causal)
// 계산이므로, 전체 가격 배열에 대해 한 번만 계산해두면 임의의 시점 i에서의 지표값이
// prices.slice(0, i+1)로 매번 새로 계산한 것과 동일하다. backtest가 매 봉마다 전체 구간을
// 잘라 모든 지표를 재계산하면 O(n^2)이 되어 데이터가 늘수록 기하급수적으로 느려지므로
// (실측: 5분봉 데이터 3배 증가 시 그리드서치 1콤보당 소요시간이 9배로 증가), 시계열 전체를
// 한 번만 계산해 인덱스로 조회하는 방식으로 O(n)에 맞춘다.
function computeSeries(prices, volumes, opts) {
  const {
    shortPeriod = DEFAULT_SHORT_PERIOD, longPeriod = DEFAULT_LONG_PERIOD, rsiPeriod = 14, zigzagPct = 0.05,
    atrPeriod = 14, erPeriod = 14, trendFilterPeriod = null, volumePeriod = 20,
  } = opts;
  return {
    emaShort: ema(prices, shortPeriod),
    emaLong: ema(prices, longPeriod),
    rsiSeries: rsi(prices, rsiPeriod),
    atrSeries: atr(prices, atrPeriod),
    erSeries: efficiencyRatio(prices, erPeriod),
    zz: zigzagPending(prices, zigzagPct),
    emaLongTerm: trendFilterPeriod ? ema(prices, trendFilterPeriod) : null,
    volAvg: volumes ? sma(volumes, volumePeriod) : null,
  };
}

// series 기반 신호 계산: prices/series 전체를 그대로 두고 시점 last에서의 신호만 평가한다.
function signalAt(prices, series, last, volumes, opts) {
  const {
    shortPeriod = DEFAULT_SHORT_PERIOD, longPeriod = DEFAULT_LONG_PERIOD,
    volumeMultiplier = 1.2,
    trendFilterPeriod = null,
    trendFilterHard = false,
    macroTrendUp = null,
    erTrendThreshold = 0.3,
    breakoutLookback = 50, trendScoreThreshold = 2, trendAtrMultiplier = 2.5,
  } = opts;
  const { emaShort, emaLong, rsiSeries, atrSeries, erSeries, zz, emaLongTerm, volAvg } = series;
  const pivots = zz.confirmed.filter((p) => p.confirmedAt <= last);
  pivots.push({ index: zz.pendingIdx[last], price: zz.pendingVal[last] });
  const wave = elliottWaveHint(pivots);

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

  // 시장 국면 판별: ER이 높으면(추세장) 추세추종 모드로 진입 후보가 되고, 낮으면
  // (비추세 구간) 관망한다. 평균회귀(박스권) 거래는 별도 엔진 boxBreakoutBacktest()가
  // 전담하므로 여기서는 추세 추종만 다룬다.
  const curEr = erSeries[last];
  const regime = curEr != null && curEr >= erTrendThreshold ? "추세" : "비추세";

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

  const volConfirmed = volumeConfirmedAt(volumes, volAvg, last, volumeMultiplier);
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
    const curLongTerm = emaLongTerm[last];
    if (curLongTerm != null) {
      longTrendUp = curPrice > curLongTerm;
      if (longTrendUp && score < 0) { score += 1; reasons.push(`장기추세(EMA${trendFilterPeriod}) 상승 중 - 매도 신호 약화`); }
      if (!longTrendUp && score > 0) { score -= 1; reasons.push(`장기추세(EMA${trendFilterPeriod}) 하락 중 - 매수 신호 약화`); }
    }
  }

  let position = "관망";
  if (regime === "추세") {
    if (score >= trendScoreThreshold) position = "매수";
    else if (score <= -trendScoreThreshold) position = "매도";
  }

  // 추세추종 진입 보강: EMA 골든/데드크로스만으로는 추세가 이미 꺾이기 시작한
  // 끝물에 들어가 트레일링 스탑에 바로 걸리는 경우가 대부분이었다(실측: 19건 중
  // 17건이 진입 직후 -1~-4% 손절). ER이 "상승 중"이고(추세 강도가 막 붙는 구간)
  // 가격이 최근 N봉 신고가/신저가를 "돌파"한 시점으로 진입을 좁혀 늦은 진입을 줄인다.
  if (regime === "추세" && position !== "관망") {
    const lookbackPrices = prices.slice(Math.max(0, last - breakoutLookback), last);
    const erRising = curEr != null && erSeries[last - 1] != null && curEr > erSeries[last - 1];
    const breakoutConfirmed = position === "매수"
      ? curPrice > Math.max(...lookbackPrices)
      : curPrice < Math.min(...lookbackPrices);
    if (!(erRising && breakoutConfirmed)) {
      reasons.push("추세 진입 보류: ER 상승 또는 신고가/신저가 돌파 미확인");
      position = "관망";
    }
  }

  // 하드 추세 게이트: trendFilterHard=true면 장기추세(EMA trendFilterPeriod)와
  // 반대 방향 진입을 점수 보정이 아니라 완전히 차단한다. 4기간 검증에서 EMA150
  // 기준으로 p3/p4가 양전환됨을 확인(BACKTEST_BASELINE.md "하드 추세게이트" 절 참고).
  if (trendFilterHard && trendFilterPeriod && longTrendUp != null && position !== "관망") {
    if (position === "매수" && !longTrendUp) {
      reasons.push(`하드 추세게이트: 장기추세(EMA${trendFilterPeriod}) 하락 중 - 매수 차단`);
      position = "관망";
    } else if (position === "매도" && longTrendUp) {
      reasons.push(`하드 추세게이트: 장기추세(EMA${trendFilterPeriod}) 상승 중 - 매도 차단`);
      position = "관망";
    }
  }

  // 거시 레짐 자본배분 게이트: 개별 심볼 신호와 무관하게 BTC 등 시장 전체
  // 추세(macroTrendUp[last])와 반대되는 진입을 차단한다. 개별 종목의
  // trendFilterHard와 달리 "이 종목이 오르는가"가 아니라 "시장 전체가
  // 우호적인가"를 묻는 것이라 알트코인이 일시적으로 자기 추세를 보여도
  // 거시 국면이 불리하면 거래를 쉰다(자본배분 레이어).
  if (macroTrendUp && position !== "관망") {
    const macroUp = macroTrendUp[last];
    if (macroUp != null) {
      if (position === "매수" && !macroUp) {
        reasons.push("거시레짐게이트: 시장 전체 하락추세 - 매수 차단");
        position = "관망";
      } else if (position === "매도" && macroUp) {
        reasons.push("거시레짐게이트: 시장 전체 상승추세 - 매도 차단");
        position = "관망";
      }
    }
  }

  // ATR 기반 초기 손절: 변동성이 클수록 손절폭도 넓어진다 (정액 스윙 고저점 대신).
  // trendAtrMultiplier를 넓게 잡아야 진입 직후 흔들림에 바로 털리지 않고
  // 트레일링 스탑까지 갈 기회를 준다. 목표가는 정해두지 않고(null) 백테스트에서
  // 트레일링 스탑으로 관리한다.
  const curAtr = atrSeries[last];
  const riskDistance = curAtr != null ? curAtr * trendAtrMultiplier : curPrice * 0.02;
  const stopLoss = position === "매수" ? curPrice - riskDistance
    : position === "매도" ? curPrice + riskDistance
    : null;
  const target = null;

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

// 메인 신호 생성: 종가 시계열을 받아 결정론적 매매 신호를 반환
export function generateSignal(prices, opts = {}, volumes = null) {
  const resolved = withTimeframePreset(opts);
  const { longPeriod = 21, trendFilterPeriod = null } = resolved;
  const minNeeded = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  if (prices.length < minNeeded) {
    throw new Error(`신호 계산에 최소 ${minNeeded}개 데이터 포인트가 필요합니다 (현재 ${prices.length}개)`);
  }
  const series = computeSeries(prices, volumes, {
    shortPeriod: resolved.shortPeriod ?? 8, longPeriod: resolved.longPeriod ?? 21,
    rsiPeriod: resolved.rsiPeriod ?? 14, zigzagPct: resolved.zigzagPct ?? 0.05,
    atrPeriod: resolved.atrPeriod ?? 14, erPeriod: resolved.erPeriod ?? 14,
    trendFilterPeriod, volumePeriod: resolved.volumePeriod ?? 20,
  });
  return signalAt(prices, series, prices.length - 1, volumes, resolved);
}

// 워크포워드 백테스트(추세 추종 전용): 비추세 구간의 ATR 고정 손절/목표가 평균회귀
// ("횡보" 모드)는 boxBreakoutBacktest()로 대체되어 폐기됐다. 매수 신호 진입, (매도
// 신호 | 트레일링 스탑 터치) 시 청산.
//  - 진입 후 갈아탄 최고가(롱) 대비 ATR*trailMultiplier만큼 따라오는 트레일링
//    스탑만 사용해 추세가 꺾이기 전까지 수익을 최대한 끌고 간다.
//  - takerFeePct: 거래소 테이커 수수료(예: 0.038%). 진입/청산 모두 즉시 체결이
//    필요한 시장가로 가정해 양방향 차감한다. 레버리지는 수익률에
//    단순 배율로 곱해지므로(강제청산 위험은 별도 고려 필요) leverage로 적용한다.
export function backtest(prices, opts = {}, volumes = null) {
  const {
    shortPeriod = 8, longPeriod = 21, rsiPeriod = 14, zigzagPct = 0.05,
    useStopLoss = true,
    atrPeriod = 14, trendFilterPeriod, trendFilterHard = false, macroTrendUp = null,
    volumePeriod, volumeMultiplier, erPeriod, erTrendThreshold,
    trailMultiplier = 1.5, takerFeePct = 0.038, leverage = 1,
    breakoutLookback, trendScoreThreshold, trendAtrMultiplier,
    // 피라미딩(추세 추종 강화): 추세가 더 강해질 때 "이기고 있는 포지션"에만
    // 추가 진입한다. RAVEUSDT 사건의 핵심 원인은 손실 중인 숏을 며칠간
    // 늘려간 것이었으므로, 지는 포지션에 추가하는 불타기(averaging down)는
    // 절대 하지 않고 가격이 진입 방향으로 더 움직였을 때만(ATR 버퍼 이상
    // 유리하게 진행) 추가해 추세 구간에서의 수익을 증폭시킨다.
    pyramidEnabled = false, maxPyramidAdds = 2, pyramidAddFraction = 0.5,
    riskGuardEnabled = false, maxDrawdownLimit, mddCooldownBars,
    maxConsecutiveLosses, consecutiveCooldownBars,
  } = withTimeframePreset(opts);
  const riskGuard = createRiskGuard({
    riskGuardEnabled, maxDrawdownLimit, mddCooldownBars,
    maxConsecutiveLosses, consecutiveCooldownBars,
  });
  const minBars = Math.max(longPeriod, trendFilterPeriod || 0) + 2;
  const trades = [];
  let holding = false;
  let entryPrice = null; // 가중평균 진입가
  let activeStop = null;
  let highestSinceEntry = null;
  let lastPosition = "관망";
  let entryIndex = null;
  let legs = []; // [{ price, weight }] — 피라미딩 시 진입 단가별 비중 추적
  let pyramidAdds = 0;
  let lastAddPrice = null;

  const series = computeSeries(prices, volumes, {
    shortPeriod, longPeriod, rsiPeriod, zigzagPct,
    atrPeriod, erPeriod, trendFilterPeriod, volumePeriod,
  });
  const atrSeries = series.atrSeries;

  const closeTrade = (exitPrice, exitReason, exitIndex) => {
    const totalWeight = legs.reduce((a, l) => a + l.weight, 0);
    // 가중 수익률: 각 진입단가별 비중×수익률을 더한다(평단가 1개로 합치는 방식 대신
    // 레그별로 계산해야 추가매수 시 투입자본이 늘어난 만큼 손익도 정확히 커진다).
    const grossPct = legs.reduce((a, l) => a + l.weight * (exitPrice - l.price) / l.price, 0) * 100 * leverage;
    const feePct = takerFeePct * (1 + totalWeight); // 진입(레그별 1회)+청산 1회, 모두 테이커
    const returnPct = grossPct - feePct;
    trades.push({ entryPrice: legs[0].price, exitPrice, entryIndex, exitIndex, returnPct, exitReason, regime: "추세", feePct, pyramidAdds });
    riskGuard.registerTrade(returnPct, exitIndex);
    holding = false;
    entryPrice = null;
    activeStop = null;
    highestSinceEntry = null;
    entryIndex = null;
    legs = [];
    pyramidAdds = 0;
    lastAddPrice = null;
  };

  for (let i = minBars; i < prices.length; i++) {
    riskGuard.onBarTick(i);
    if (holding) {
      highestSinceEntry = Math.max(highestSinceEntry, prices[i]);
      const curAtr = atrSeries[i];
      if (curAtr != null) activeStop = Math.max(activeStop, highestSinceEntry - curAtr * trailMultiplier);
    }
    if (holding && useStopLoss && activeStop != null && prices[i] <= activeStop) {
      // activeStop(이상적 스탑가)이 아닌 prices[i](실제 종가)로 체결 처리.
      // 캔들 종가가 스탑선을 넘어 더 멀리 찍히는 경우가 잦아 activeStop 체결
      // 가정은 손실을 체계적으로 과소평가함(박스권 백테스트에서 동일 버그 확인됨).
      closeTrade(prices[i], "trailing_stop", i);
      continue;
    }

    let sig;
    try {
      sig = signalAt(prices, series, i, volumes, {
        shortPeriod, longPeriod, trendFilterPeriod, trendFilterHard, macroTrendUp,
        volumeMultiplier, erTrendThreshold,
        breakoutLookback, trendScoreThreshold, trendAtrMultiplier,
      });
    } catch {
      continue;
    }

    if (!holding && sig.position === "매수" && riskGuard.canTrade()) {
      holding = true;
      entryPrice = prices[i];
      entryIndex = i;
      activeStop = sig.stopLoss;
      highestSinceEntry = prices[i];
      legs = [{ price: prices[i], weight: 1 }];
      pyramidAdds = 0;
      lastAddPrice = prices[i];
    } else if (holding && sig.position === "매도") {
      closeTrade(prices[i], "signal_flip", i);
    } else if (holding && pyramidEnabled && sig.position === "매수" && pyramidAdds < maxPyramidAdds) {
      // 불타기(averaging down) 금지: 직전 추가매수 가격보다 ATR*1배 이상
      // "유리한 방향(상승)"으로 더 움직였을 때만, 즉 이기고 있는 포지션에만 추가한다.
      const curAtr = atrSeries[i];
      const favorableMove = curAtr != null && prices[i] >= lastAddPrice + curAtr;
      if (favorableMove) {
        legs.push({ price: prices[i], weight: pyramidAddFraction });
        pyramidAdds += 1;
        lastAddPrice = prices[i];
      }
    }
    lastPosition = sig.position;
  }

  if (holding) {
    closeTrade(prices[prices.length - 1], "open_at_end", prices.length - 1);
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
    riskGuardEvents: riskGuard.getEvents(),
  };
}

// 모멘텀 돌파 추격 엔진("공격적" 버킷용). RAVEUSDT 실거래 분석에서 확인된 최대
// 손실 원인은 (1) 숏 포지션을 며칠에 걸쳐 점점 늘려가며 들고 있다가 (2) 급등에
// 강제청산 캐스케이드로 청산된 것이었다. 이 엔진은 그 두 조건을 구조적으로
// 차단한다: 패배 중인 포지션에는 절대 추가진입(불타기)하지 않고(승리 중일 때만
// 추가하는 backtest()의 pyramid 옵션과 달리 이 엔진은 단일 포지션만 보유),
// maxHoldBars로 보유기간 자체에 상한을 둬 "며칠씩 누적" 구조가 원천적으로
// 불가능하다. 거래량 스파이크 + N봉 신고가/신저가 돌파 + ER 상승을 동시에
// 요구해 가짜 돌파를 거른 뒤, 좁은 하드스탑+트레일링스탑으로 빠르게 잘라낸다.
export function momentumChaseBacktest(prices, opts = {}, volumes = null) {
  const {
    // 아래 기본값은 edgeX 20종목 30분봉(최근 14일 구간) 실데이터 그리드서치로
    // 검증한 값이다(breakoutLookback 3종×volumeSpikeMultiplier 4종×breakoutStopPct
    // 3종×breakoutTrailPct 3종×maxHoldBars 4종, 총 432콤보). 초기값(20/2.5/2%/2.5%/12바)은
    // 평균 -23.0%로 손실이었으나, 돌파 판정 구간을 60바(30시간)로 늘리고 손절/트레일을
    // 3%/4%로 넓히고 보유기간 상한을 48바(24시간)로 늘리자 평균 +14.85%(n=270, 20종목
        // 중 14종목 양전)로 개선돼 채택. 상위 3건을 제외해도 +109.63%p로 소수 대형거래
    // 의존도가 낮아 견고함을 확인. ENA/PUMP처럼 변동성이 극단적인 종목은 손실이 커
    // 단일 종목 노출 한도(포트폴리오 오케스트레이터 레벨)를 두는 것을 권장한다.
    breakoutLookback = 60,
    volumeAvgPeriod = 20,
    volumeSpikeMultiplier = 2.5,
    erPeriod = 14,
    breakoutStopPct = 0.03,   // 진입가 기준 하드 손절
    breakoutTrailPct = 0.04,  // 유리한 쪽 극값 기준 트레일링 스탑
    maxHoldBars = 48,         // 보유기간 상한 — RAVEUSDT식 "며칠씩 누적" 차단
    takerFeePct = 0.038, leverage = 1,
  } = opts;

  const minBars = Math.max(breakoutLookback, volumeAvgPeriod, erPeriod) + 1;
  if (prices.length <= minBars) {
    return { trades: [], tradeCount: 0, winRate: 0, totalReturnPct: 0 };
  }

  const volAvg = volumes ? trailingAvg(volumes, volumeAvgPeriod) : null;
  const erSeries = efficiencyRatio(prices, erPeriod);
  const rollHigh = rollingExtreme(prices, breakoutLookback, (a, b) => a >= b);
  const rollLow = rollingExtreme(prices, breakoutLookback, (a, b) => a <= b);

  const trades = [];
  let holding = false;
  let entryPrice = null, entryIndex = null, side = null;
  let extremeSinceEntry = null;

  const closeTrade = (exitPrice, exitReason, exitIndex) => {
    const dir = side === "short" ? -1 : 1;
    const grossPct = dir * (exitPrice - entryPrice) / entryPrice * 100 * leverage;
    const feePct = takerFeePct * 2;
    trades.push({ entryPrice, exitPrice, entryIndex, exitIndex, returnPct: grossPct - feePct, exitReason, regime: "모멘텀추격", side });
    holding = false;
    entryPrice = null; entryIndex = null; side = null; extremeSinceEntry = null;
  };

  for (let i = minBars; i < prices.length; i++) {
    const price = prices[i];

    if (holding) {
      extremeSinceEntry = side === "long" ? Math.max(extremeSinceEntry, price) : Math.min(extremeSinceEntry, price);
      const trailStop = side === "long" ? extremeSinceEntry * (1 - breakoutTrailPct) : extremeSinceEntry * (1 + breakoutTrailPct);
      const hardStop = side === "long" ? entryPrice * (1 - breakoutStopPct) : entryPrice * (1 + breakoutStopPct);
      const stopHit = side === "long" ? (price <= trailStop || price <= hardStop) : (price >= trailStop || price >= hardStop);
      const timeUp = i - entryIndex >= maxHoldBars;
      if (stopHit) {
        // 봉 종가가 손절선을 훌쩍 넘겨도(고변동성 코인의 30분봉 갭) 그 종가로
        // 청산하면 의도한 손절폭을 한참 초과한 손실이 찍힌다(실측: ENA -25.23%,
        // PUMP -14%대 등, 의도한 3%×레버리지를 크게 벗어남). boxBreakoutBacktest의
        // RANGE 모드처럼 정해둔 손절/트레일 레벨 자체로 청산해 의도한 리스크를 지킨다.
        const bindingStop = side === "long" ? Math.max(trailStop, hardStop) : Math.min(trailStop, hardStop);
        closeTrade(bindingStop, bindingStop === hardStop ? "stop_loss" : "trailing_stop", i);
        continue;
      }
      if (timeUp) { closeTrade(price, "max_hold_expired", i); continue; }
      continue;
    }

    // i-1까지의 롤링 고저/거래량평균을 써야 현재 바가 "돌파했는지"를 판단할 수 있다.
    const high = rollHigh[i - 1], low = rollLow[i - 1];
    const curEr = erSeries[i], prevEr = erSeries[i - 1];
    const erRising = curEr != null && prevEr != null && curEr > prevEr;
    const volSpike = volumes && volAvg && volAvg[i] != null ? volumes[i] >= volAvg[i] * volumeSpikeMultiplier : false;
    if (!volSpike || !erRising || high == null || low == null) continue;

    if (price > high) {
      holding = true; side = "long"; entryPrice = price; entryIndex = i; extremeSinceEntry = price;
    } else if (price < low) {
      holding = true; side = "short"; entryPrice = price; entryIndex = i; extremeSinceEntry = price;
    }
  }

  if (holding) closeTrade(prices[prices.length - 1], "open_at_end", prices.length - 1);

  const wins = trades.filter((t) => t.returnPct > 0).length;
  const totalReturnPct = trades.reduce((acc, t) => acc + t.returnPct, 0);

  return {
    trades,
    tradeCount: trades.length,
    winRate: trades.length ? (wins / trades.length) * 100 : 0,
    totalReturnPct,
    riskGuardEvents: riskGuard.getEvents(),
  };
}

// momentumChaseBacktest()의 1바 단위 실시간 의사결정 버전. 오케스트레이터가 매
// 캔들마다 호출해 "지금 진입/청산/유지 중 뭘 해야 하는지"를 받는 용도다.
// position이 null이면 신규 진입 판정, 있으면 보유 중 포지션의 청산 판정만 한다.
// 백테스트 루프와 완전히 같은 조건식을 쓰므로 두 함수는 항상 같은 결론을 내야
// 하며, 이는 verify_live_signal.mjs류 스크립트로 바백테스트 거래 목록과 1:1
// 대조해 검증한다(라이브 로직이 백테스트와 몰래 갈라지는 걸 막는 안전장치).
export function momentumChaseSignal(prices, volumes = null, position = null, opts = {}) {
  const {
    breakoutLookback = 60, volumeAvgPeriod = 20, volumeSpikeMultiplier = 2.5,
    erPeriod = 14, breakoutStopPct = 0.03, breakoutTrailPct = 0.04, maxHoldBars = 48,
  } = opts;

  const i = prices.length - 1;
  const minBars = Math.max(breakoutLookback, volumeAvgPeriod, erPeriod) + 1;
  if (i < minBars) return { action: "none" };
  const price = prices[i];

  if (position) {
    const { side, entryPrice, entryIndex, extremeSinceEntry: prevExtreme } = position;
    const extremeSinceEntry = side === "long" ? Math.max(prevExtreme, price) : Math.min(prevExtreme, price);
    const trailStop = side === "long" ? extremeSinceEntry * (1 - breakoutTrailPct) : extremeSinceEntry * (1 + breakoutTrailPct);
    const hardStop = side === "long" ? entryPrice * (1 - breakoutStopPct) : entryPrice * (1 + breakoutStopPct);
    const stopHit = side === "long" ? (price <= trailStop || price <= hardStop) : (price >= trailStop || price >= hardStop);
    const timeUp = i - entryIndex >= maxHoldBars;

    if (stopHit) {
      const bindingStop = side === "long" ? Math.max(trailStop, hardStop) : Math.min(trailStop, hardStop);
      return { action: "exit", exitPrice: bindingStop, exitReason: bindingStop === hardStop ? "stop_loss" : "trailing_stop", extremeSinceEntry };
    }
    if (timeUp) return { action: "exit", exitPrice: price, exitReason: "max_hold_expired", extremeSinceEntry };
    return { action: "hold", extremeSinceEntry };
  }

  const volAvg = volumes ? trailingAvg(volumes, volumeAvgPeriod) : null;
  const erSeries = efficiencyRatio(prices, erPeriod);
  const rollHigh = rollingExtreme(prices, breakoutLookback, (a, b) => a >= b);
  const rollLow = rollingExtreme(prices, breakoutLookback, (a, b) => a <= b);

  const high = rollHigh[i - 1], low = rollLow[i - 1];
  const curEr = erSeries[i], prevEr = erSeries[i - 1];
  const erRising = curEr != null && prevEr != null && curEr > prevEr;
  const volSpike = volumes && volAvg && volAvg[i] != null ? volumes[i] >= volAvg[i] * volumeSpikeMultiplier : false;
  if (!volSpike || !erRising || high == null || low == null) return { action: "none" };

  if (price > high) return { action: "enter_long", entryPrice: price };
  if (price < low) return { action: "enter_short", entryPrice: price };
  return { action: "none" };
}

// 다종목 스캐너 — "단타로 하루 수십 차례 거래"를 한 종목의 신호를 억지로 늘려서
// 채우면 품질이 무너진다(실측: scoreThreshold를 3→2로 낮추자 11종목 합산 승률이
// 49.5%→45.1%, 수익이 +19.72%→-39.01%로 붕괴). 대신 같은 품질 기준(scoreThreshold=3)을
// 유지한 채 여러 종목을 동시에 감시해서 그중 신호가 뜬 종목에만 들어가는 방식으로
// 빈도를 확보한다. 실측 기준 종목당 평균 약 0.09건/일(시간봉, 90일)이 나오므로,
// 하루 수십 건을 채우려면 코인 수를 충분히 늘려서 스캔해야 한다(예: 30건/일을
// 노리면 약 300개 종목 스캔 필요).
//
// assets: [{ symbol, prices, volumes }, ...] — 각 종목의 최신 시점까지의 시계열.
// 반환: 관망이 아닌(매수/매도) 종목만 확신도 내림차순으로 정렬해 반환한다.
export function scanMarket(assets, opts = {}) {
  const signals = [];
  for (const { symbol, prices, volumes } of assets) {
    let sig;
    try {
      sig = generateSignal(prices, opts, volumes);
    } catch {
      continue;
    }
    if (sig.position !== "관망") signals.push({ symbol, ...sig });
  }
  return signals.sort((a, b) => b.confidence - a.confidence);
}

// 구간 [i-period, i-1] (현재 바 제외)의 평균을 매 인덱스마다 계산. 거래량 스파이크
// 판정에 현재 바 자체가 평균에 섞여 들어가면 스파이크가 희석되므로 prefix sum으로
// "직전까지의" 평균을 O(n)에 구한다.
function trailingAvg(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    if (i >= 1) {
      out[i] = i >= period ? sum / period : sum / i;
    }
    sum += values[i];
    if (i >= period) sum -= values[i - period];
  }
  return out;
}

// 구간 [i-period+1, i] 의 최대/최소값을 모노토닉 데크로 O(n)에 계산(롤링 최고가/최저가).
function rollingExtreme(values, period, cmp) {
  const out = new Array(values.length).fill(null);
  const deque = []; // [index, value], cmp 기준으로 정렬 유지
  for (let i = 0; i < values.length; i++) {
    while (deque.length && cmp(values[i], deque[deque.length - 1][1])) deque.pop();
    deque.push([i, values[i]]);
    while (deque[0][0] <= i - period) deque.shift();
    if (i >= period - 1) out[i] = deque[0][1];
  }
  return out;
}

// 박스권/돌파 듀얼모드 매매. 평소엔 박스권(RANGE) 모드로 지지선/저항선 사이를
// 왕복하며 작은 수익을 누적하고, 거래량이 평균 대비 급증하며 박스권을 벗어나면
// 돌파(BREAKOUT) 모드로 전환해 추세를 추격, 트레일링 스탑으로 청산 후 다시
// RANGE로 복귀하는 상태머신. (참고: 사용자 제공 의사코드의 RANGE/BREAKOUT_LONG/
// BREAKOUT_SHORT 전환 로직을 그대로 구현 — 진입/청산에만 0.5%/1%대 버퍼를 둬서
// 정확히 경계선에 닿아야만 체결되는 비현실적 조건을 완화.)
export function boxBreakoutBacktest(prices, opts = {}, volumes = null) {
  const {
    boxLookback = 96,        // 박스 상/하단 산정 구간(바 개수). 30분봉 기준 96바=48시간
    // 주의: boxStopBufferPct를 좁히면 백테스트상 승률/수익률이 크게 좋아지는 것처럼 보이지만,
    // 이는 청산을 항상 "정확히 stopPrice"에 체결된다고 가정하는 모델의 낙관적 오류 때문임
    // (실제 종가는 그 값을 평균 0.5~0.6%, 최대 8%+ 넘어서 찍히는 경우가 빈번 — 30분봉+타이트
    // 스탑+레버리지 조합에서 슬리피지/갭이 큼). 종가 기준으로 재계산하면 해당 후보는 4개 기간
    // 중 3개에서 오히려 baseline보다 더 나쁨(BACKTEST_BASELINE.md "A버킷 파라미터 변경
    // 재검토" 참조) — 기각하고 원래 값으로 유지.
    boxRangePct = 0.05,      // 이 진폭(상단-하단)/하단 이하일 때만 "유효한 박스"로 인정
    entryBufferPct = 0.005,  // 하단+0.5%/상단-0.5%에서 박스권 진입
    boxStopBufferPct = 0.01, // 박스 하단-1%/상단+1% 벗어나면 박스권 거래 손절
    volumeAvgPeriod = 20,
    volumeSpikeMultiplier = 3,
    breakoutTrailPct = 0.02,
    breakoutStopPct = 0.015,
    makerFeePct = 0.018, takerFeePct = 0.038, leverage = 1,
    riskGuardEnabled = false, maxDrawdownLimit, mddCooldownBars,
    maxConsecutiveLosses, consecutiveCooldownBars,
  } = opts;
  const riskGuard = createRiskGuard({
    riskGuardEnabled, maxDrawdownLimit, mddCooldownBars,
    maxConsecutiveLosses, consecutiveCooldownBars,
  });

  const minBars = Math.max(boxLookback, volumeAvgPeriod) + 1;
  const boxHigh = rollingExtreme(prices, boxLookback, (a, b) => a >= b);
  const boxLow = rollingExtreme(prices, boxLookback, (a, b) => a <= b);
  const volAvg = volumes ? trailingAvg(volumes, volumeAvgPeriod) : null;

  const trades = [];
  let mode = "RANGE"; // RANGE | BREAKOUT_LONG | BREAKOUT_SHORT
  let holding = false;
  let entryPrice = null, entryIndex = null, side = null; // side: "long" | "short" (RANGE 모드 거래용)
  let stopPrice = null, targetPrice = null; // RANGE 모드
  let extremeSinceEntry = null; // BREAKOUT 모드 트레일링용

  const closeTrade = (exitPrice, exitReason, exitIndex, regimeLabel) => {
    const dir = side === "short" ? -1 : 1;
    const grossPct = dir * (exitPrice - entryPrice) / entryPrice * 100 * leverage;
    const exitFeePct = exitReason === "take_profit" ? makerFeePct : takerFeePct;
    const feePct = takerFeePct + exitFeePct;
    const returnPct = grossPct - feePct;
    trades.push({ entryPrice, exitPrice, entryIndex, exitIndex, returnPct, exitReason, regime: regimeLabel, feePct, side });
    riskGuard.registerTrade(returnPct, exitIndex);
    holding = false;
    entryPrice = null; entryIndex = null; side = null;
    stopPrice = null; targetPrice = null; extremeSinceEntry = null;
  };

  for (let i = minBars; i < prices.length; i++) {
    riskGuard.onBarTick(i);
    const price = prices[i];
    // i-1까지(현재 바 제외)의 박스 경계를 써야 "현재가가 박스를 벗어났는지"를
    // 판단할 수 있다 — i를 포함하면 현재가 자체가 항상 그 범위 안에 들어가
    // 돌파 조건이 영원히 성립하지 않는다.
    const high = boxHigh[i - 1], low = boxLow[i - 1];
    const volSpike = volumes && volAvg && volAvg[i] != null
      ? volumes[i] >= volAvg[i] * volumeSpikeMultiplier
      : false;

    if (mode === "RANGE") {
      if (!holding && volSpike) {
        if (price > high) mode = "BREAKOUT_LONG";
        else if (price < low) mode = "BREAKOUT_SHORT";
        continue; // 이번 바는 모드 전환만, 진입은 다음 바부터
      }

      if (holding) {
        if (side === "long") {
          if (price <= stopPrice) { closeTrade(stopPrice, "stop_loss", i, "박스권"); continue; }
          if (price >= targetPrice) { closeTrade(targetPrice, "take_profit", i, "박스권"); continue; }
        } else {
          if (price >= stopPrice) { closeTrade(stopPrice, "stop_loss", i, "박스권"); continue; }
          if (price <= targetPrice) { closeTrade(targetPrice, "take_profit", i, "박스권"); continue; }
        }
      } else if (riskGuard.canTrade()) {
        const boxValid = (high - low) / low <= boxRangePct;
        if (boxValid && price <= low * (1 + entryBufferPct)) {
          holding = true; side = "long"; entryPrice = price; entryIndex = i;
          targetPrice = high * (1 - entryBufferPct);
          stopPrice = low * (1 - boxStopBufferPct);
        } else if (boxValid && price >= high * (1 - entryBufferPct)) {
          holding = true; side = "short"; entryPrice = price; entryIndex = i;
          targetPrice = low * (1 + entryBufferPct);
          stopPrice = high * (1 + boxStopBufferPct);
        }
      }
    } else {
      // BREAKOUT_LONG / BREAKOUT_SHORT
      const isLong = mode === "BREAKOUT_LONG";
      if (!holding) {
        if (!riskGuard.canTrade()) { mode = "RANGE"; continue; }
        holding = true; side = isLong ? "long" : "short"; entryPrice = price; entryIndex = i;
        extremeSinceEntry = price;
      } else {
        extremeSinceEntry = isLong ? Math.max(extremeSinceEntry, price) : Math.min(extremeSinceEntry, price);
        const trailStop = isLong ? extremeSinceEntry * (1 - breakoutTrailPct) : extremeSinceEntry * (1 + breakoutTrailPct);
        const hardStop = isLong ? entryPrice * (1 - breakoutStopPct) : entryPrice * (1 + breakoutStopPct);
        const hit = isLong ? (price <= trailStop || price <= hardStop) : (price >= trailStop || price >= hardStop);
        if (hit) {
          closeTrade(price, price === hardStop ? "stop_loss" : "trailing_stop", i, "돌파");
          mode = "RANGE";
        }
      }
    }
  }

  if (holding) closeTrade(prices[prices.length - 1], "open_at_end", prices.length - 1, mode === "RANGE" ? "박스권" : "돌파");

  const wins = trades.filter((t) => t.returnPct > 0).length;
  const totalReturnPct = trades.reduce((acc, t) => acc + t.returnPct, 0);

  return {
    trades,
    tradeCount: trades.length,
    winRate: trades.length ? (wins / trades.length) * 100 : 0,
    totalReturnPct,
  };
}
