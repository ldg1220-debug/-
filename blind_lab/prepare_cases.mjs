// 블라인드 라벨링 케이스 생성 — 실데이터(/tmp/edgex_top20_*)에서
// (종목, 타임프레임, 시점 t) 무작위 샘플을 뽑아 cases.json으로 저장.
// 각 케이스는 t까지 보이는 구간 + (사용자 결정 전엔 숨김) t+1~t+H 구간을 포함한다.
// 화면 표시용 룩백(DISPLAY=200) 앞에 MA(100) 계산용 워밍업(WARMUP=100)을 추가로 포함.
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));

const DISPLAY = 200;   // 화면에 보이는 캔들 수 (t까지)
const WARMUP = 105;    // 100MA 계산을 위한 추가 과거 데이터
const H = 60;          // forward 채점 구간(봉수)
const N_CASES = 60;    // 총 케이스 수 (IN 30 + OUT 30)

const SOURCES = [
  { tf: "30m", dir: `${ROOT}/raw_data/edgex_30m_ohlc` },
  { tf: "4h", dir: `${ROOT}/raw_data/edgex_4h_continuous` },
  { tf: "1d", dir: `${ROOT}/raw_data/edgex_1d_continuous` },
];
const QUOTA_PER_TF = Math.floor(N_CASES / SOURCES.length); // 타임프레임별 균등 쿼터

function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
// 매번 새로운 시드 → 재생성할 때마다 다른 (종목,시점) 조합이 뽑혀 이전에 본 차트와 겹치지 않음
const rand = mulberry32(Date.now() ^ Math.floor(Math.random() * 0xffffffff));

const poolByTf = {};
for (const { tf, dir } of SOURCES) {
  const syms = fs.readdirSync(dir).map(f => f.replace(".json", ""));
  poolByTf[tf] = [];
  for (const sym of syms) {
    const d = JSON.parse(fs.readFileSync(`${dir}/${sym}.json`, "utf8"));
    const n = d.closes.length;
    const minIdx = WARMUP + DISPLAY;
    const maxIdx = n - H - 1;
    if (maxIdx <= minIdx) continue;
    poolByTf[tf].push({ tf, sym, d, minIdx, maxIdx });
  }
}

for (const { tf } of SOURCES) {
  if (!poolByTf[tf].length) { console.error(`${tf} 가용 데이터 없음`); process.exit(1); }
}

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const cases = [];
for (const { tf } of SOURCES) {
  const pool = poolByTf[tf];
  // 같은 종목이 다른 종목들보다 먼저 두 번 뽑히지 않도록, 셔플된 종목 순서를 한 바퀴씩 다 쓰고 나서야 다음 바퀴(반복)를 시작한다.
  let cycle = shuffle(pool);
  let cyclePos = 0;
  let made = 0;
  let guard = 0;
  while (made < QUOTA_PER_TF && guard < QUOTA_PER_TF * 50) {
    guard++;
    if (cyclePos >= cycle.length) { cycle = shuffle(pool); cyclePos = 0; }
    const src = cycle[cyclePos++];
    const t = src.minIdx + Math.floor(rand() * (src.maxIdx - src.minIdx));
    const sliceStart = t - WARMUP - DISPLAY;
    const sliceEnd = t + H; // inclusive index of last revealed bar
    const slice = (arr) => arr.slice(sliceStart, sliceEnd + 1);
    cases.push({
      id: cases.length + 1,
      tf: src.tf,
      symbol: src.sym,
      // 인덱스 매핑: 슬라이스 내에서 t의 로컬 인덱스
      tLocal: WARMUP + DISPLAY,
      warmup: WARMUP,
      display: DISPLAY,
      h: H,
      times: slice(src.d.times),
      highs: slice(src.d.highs),
      lows: slice(src.d.lows),
      closes: slice(src.d.closes),
      volumes: slice(src.d.volumes),
    });
    made++;
  }
}

// IN(처음 30) / OUT(나머지) 라벨은 분석 시 "제출 순서" 기준으로 나누지만,
// 케이스 자체의 노출 순서를 무작위로 섞어 사용자가 구간을 예측 못 하게 한다.
for (let i = cases.length - 1; i > 0; i--) {
  const j = Math.floor(rand() * (i + 1));
  [cases[i], cases[j]] = [cases[j], cases[i]];
}
cases.forEach((c, i) => (c.order = i + 1));

fs.writeFileSync(`${ROOT}/cases.json`, JSON.stringify(cases));
console.log(`케이스 ${cases.length}개 생성 완료 → ${ROOT}/cases.json`);
const byTf = {};
for (const c of cases) byTf[c.tf] = (byTf[c.tf] || 0) + 1;
console.log("타임프레임 분포:", byTf);
