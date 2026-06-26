// blind_lab 원본 시세 데이터 수집 — edgeX 공개 API에서 거래량 상위 종목 풀을 동적으로 구성해
// 30m / 4h / 1d OHLC를 받아 raw_data/ 에 저장한다.
// 매번 실행할 때마다 그 시점 거래량 상위 종목 + 최신 캔들로 갱신되므로,
// 차트 풀을 다양화/최신화하고 싶을 때 그냥 재실행하면 된다.
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const RAW_DIR = `${ROOT}/raw_data`;

const META = "https://pro.edgex.exchange/api/v1/public/meta/getMetaData";
const TICKER = "https://pro.edgex.exchange/api/v1/public/quote/getTicker";
const KLINE = "https://pro.edgex.exchange/api/v1/public/quote/getKline";

const NON_CRYPTO = new Set(["XAUT","SILVER","CL","COPPER","NATGAS","PAXG","INTC","QQQ","SPY","CRCL","GOOG","NVDA","MSTR","COIN","AMD","TSLA","AAPL","META","AMZN","MSFT","NFLX","HOOD","PLTR","AVGO"]);
const N_SYMBOLS = Number(process.argv[2] || 30); // 거래량 상위 N개 종목 사용

async function pickTopSymbols(n) {
  const metaRes = await fetch(META);
  const meta = await metaRes.json();
  const contracts = meta.data.contractList || meta.data.contracts || [];
  const results = [];
  for (const c of contracts) {
    const symbol = c.contractName ? c.contractName.replace(/USD.*/, "").replace(/-.*/, "") : (c.baseCoinName || c.name);
    if (!symbol || NON_CRYPTO.has(symbol)) continue;
    if (c.enableTrade === false || c.enableDisplay === false) continue;
    try {
      const tRes = await fetch(`${TICKER}?contractId=${c.contractId}`);
      const t = await tRes.json();
      const vol = Number(t.data?.[0]?.value || 0);
      if (vol > 0) results.push({ symbol, contractId: c.contractId, vol });
    } catch (e) { /* 일시 오류 종목은 건너뜀 */ }
  }
  results.sort((a, b) => b.vol - a.vol);
  return results.slice(0, n);
}

async function fetchKlines(contractId, klineType, sinceMs, untilMs) {
  let all = [];
  let offsetData = "";
  while (true) {
    const url = `${KLINE}?contractId=${contractId}&klineType=${klineType}&size=1000&priceType=LAST_PRICE&filterBeginKlineTimeInclusive=${sinceMs}&filterEndKlineTimeExclusive=${untilMs}${offsetData ? `&offsetData=${offsetData}` : ""}`;
    const res = await fetch(url);
    const j = await res.json();
    if (j.code !== "SUCCESS") throw new Error(JSON.stringify(j));
    const list = j.data.dataList;
    all.push(...list);
    if (!j.data.nextPageOffsetData || list.length === 0) break;
    offsetData = j.data.nextPageOffsetData;
    if (all.length > 50000) break;
  }
  const seen = new Set();
  const dedup = all.filter(k => { if (seen.has(k.klineId)) return false; seen.add(k.klineId); return true; });
  dedup.sort((a, b) => Number(a.klineTime) - Number(b.klineTime));
  return dedup;
}

const TFS = [
  { klineType: "MINUTE_30", dirName: "edgex_30m_ohlc", lookbackDays: 60, minBars: 366 },
  { klineType: "HOUR_4", dirName: "edgex_4h_continuous", lookbackDays: 660, minBars: 366 },
  { klineType: "DAY_1", dirName: "edgex_1d_continuous", lookbackDays: 1500, minBars: 366 },
];

const now = Date.now();
console.log(`거래량 상위 ${N_SYMBOLS}개 종목 조회 중...`);
const top = await pickTopSymbols(N_SYMBOLS);
console.log("선정된 종목:", top.map(t => t.symbol).join(", "));

for (const tf of TFS) {
  const dir = `${RAW_DIR}/${tf.dirName}`;
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const sinceMs = now - tf.lookbackDays * 24 * 3600 * 1000;
  for (const { symbol, contractId } of top) {
    try {
      const klines = await fetchKlines(contractId, tf.klineType, sinceMs, now);
      if (klines.length < tf.minBars) { console.log(`[${tf.dirName}] ${symbol} 데이터 부족(${klines.length}봉) — 건너뜀`); continue; }
      const times = klines.map(k => Number(k.klineTime));
      const highs = klines.map(k => Number(k.high));
      const lows = klines.map(k => Number(k.low));
      const closes = klines.map(k => Number(k.close));
      const volumes = klines.map(k => Number(k.size ?? 0));
      fs.writeFileSync(`${dir}/${symbol}.json`, JSON.stringify({ times, highs, lows, closes, volumes }));
      console.log(`[${tf.dirName}] ${symbol} ${klines.length}봉`);
    } catch (e) {
      console.log(`[${tf.dirName}] ${symbol} 에러: ${e.message}`);
    }
  }
}
console.log("완료. 이제 node prepare_cases.mjs 로 케이스를 재생성하세요.");
