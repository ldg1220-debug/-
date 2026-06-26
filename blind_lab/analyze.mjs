import fs from "fs";

const decisions = JSON.parse(fs.readFileSync("/tmp/blind_lab/decisions.json", "utf8"));
const traded = decisions.filter(d => d.action === "buy" || d.action === "sell");

function stats(arr) {
  const n = arr.length;
  if (!n) return { n: 0, avgRet: null, winRate: null };
  const avgRet = arr.reduce((a, b) => a + b.retPct, 0) / n;
  const winRate = arr.filter(b => b.retPct > 0).length / n * 100;
  return { n, avgRet, winRate };
}

function fmt(s, label) {
  if (s.n < 30) return `${label.padEnd(28)} n=${String(s.n).padStart(3)}  [판단보류: 30건 미만]`;
  return `${label.padEnd(28)} n=${String(s.n).padStart(3)}  평균수익률=${s.avgRet.toFixed(3)}%  승률=${s.winRate.toFixed(1)}%`;
}

console.log("===== 1) 전체 =====");
console.log(fmt(stats(traded), "전체 매수+매도"));
console.log(fmt(stats(traded.filter(d => d.action === "buy")), "매수만"));
console.log(fmt(stats(traded.filter(d => d.action === "sell")), "매도만"));

console.log("\n   (대조 기준선: 랜덤/항상매수 forward 성과는 같은 케이스풀에서 별도 산출 필요 —");
console.log("    cases.json의 모든 t에서 '항상매수' 시뮬을 돌려 비교하세요.)");

console.log("\n===== 2) 그룹별(A/B/C) =====");
const groups = { A: [], B: [], C: [] };
for (const d of traded) {
  const gset = new Set(d.tags.map(t => t[0]));
  for (const g of gset) if (groups[g]) groups[g].push(d);
}
for (const g of ["A", "B", "C"]) console.log(fmt(stats(groups[g]), `그룹 ${g}`));

console.log("\n===== 3) 태그별 =====");
const tagMap = {};
for (const d of traded) for (const t of d.tags) (tagMap[t] = tagMap[t] || []).push(d);
for (const tag of Object.keys(tagMap).sort()) console.log(fmt(stats(tagMap[tag]), `태그 ${tag}`));

console.log("\n===== 4) 타임프레임별 =====");
const tfMap = {};
for (const d of traded) (tfMap[d.tf] = tfMap[d.tf] || []).push(d);
for (const tf of Object.keys(tfMap).sort()) console.log(fmt(stats(tfMap[tf]), `TF ${tf}`));

console.log("\n===== 5) MFE/MAE 분포 =====");
if (traded.length) {
  const mfes = traded.map(d => d.mfe).sort((a, b) => a - b);
  const maes = traded.map(d => d.mae).sort((a, b) => a - b);
  const med = arr => arr[Math.floor(arr.length / 2)];
  console.log(`MFE 중앙값=${med(mfes).toFixed(3)}%  MAE 중앙값=${med(maes).toFixed(3)}%`);
} else {
  console.log("판단보류: 거래 없음");
}

console.log("\n===== 6) IN(1-30) → OUT(31-60) 패턴 유지 여부 =====");
const inSet = traded.filter(d => d.order <= 30);
const outSet = traded.filter(d => d.order > 30);
console.log(fmt(stats(inSet), "IN (order 1-30)"));
console.log(fmt(stats(outSet), "OUT (order 31-60)"));
console.log("\n(IN에서 우세했던 그룹/태그가 OUT에서도 평균수익률 양(+)으로 유지되는지 위 그룹별/태그별 표와 대조해 확인)");
