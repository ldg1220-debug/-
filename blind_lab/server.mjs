import fs from "fs";
import http from "http";
import path from "path";
import { fileURLToPath } from "url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const CASES = JSON.parse(fs.readFileSync(`${ROOT}/cases.json`, "utf8"));
const DECISIONS_FILE = `${ROOT}/decisions.json`;
if (!fs.existsSync(DECISIONS_FILE)) fs.writeFileSync(DECISIONS_FILE, "[]");

const FEE_PCT_PER_TXN = 0.076; // 거래(진입/정리 1회)당 왕복 테이커 수수료 % (퍼센트 수익률이라 사이즈와 무관)
const EPS = 1e-9;

function loadDecisions() {
  return JSON.parse(fs.readFileSync(DECISIONS_FILE, "utf8"));
}
function saveDecisions(arr) {
  fs.writeFileSync(DECISIONS_FILE, JSON.stringify(arr, null, 2));
}

// 단일 사용자 도구이므로 진행 중인 케이스 상태를 전역 1개만 유지한다.
let session = null;

function pickNextCase() {
  const decisions = loadDecisions();
  const doneOrders = new Set(decisions.map(d => d.order));
  const remaining = CASES.filter(c => !doneOrders.has(c.order)).sort((a, b) => a.order - b.order);
  return { remaining, completed: decisions.length };
}

function visibleSlice(c, uptoIdx) {
  return {
    times: c.times.slice(0, uptoIdx + 1),
    highs: c.highs.slice(0, uptoIdx + 1),
    lows: c.lows.slice(0, uptoIdx + 1),
    closes: c.closes.slice(0, uptoIdx + 1),
    volumes: c.volumes.slice(0, uptoIdx + 1),
  };
}

// 평단가(코스트베이시스): 지금까지 들어간 진입(레그)만으로 계산. 부분정리해도 남은 물량의 평단가는 안 바뀜(표준 평균비용법).
function avgEntry(legs) {
  const totalSize = legs.reduce((a, b) => a + b.size, 0);
  const totalCost = legs.reduce((a, b) => a + b.price * b.size, 0);
  return totalCost / totalSize;
}
function totalLegSize(legs) {
  return legs.reduce((a, b) => a + b.size, 0);
}

function updateMfeMae(sess, idx) {
  const c = sess.case;
  const dirMul = sess.direction === "buy" ? 1 : -1;
  const ae = avgEntry(sess.legs);
  const favorHigh = dirMul * ((c.highs[idx] - ae) / ae) * 100;
  const favorLow = dirMul * ((c.lows[idx] - ae) / ae) * 100;
  sess.mfe = Math.max(sess.mfe, favorHigh, favorLow);
  sess.mae = Math.min(sess.mae, favorHigh, favorLow);
}

// 현 시점 평단가 기준으로 size만큼 정리(부분 또는 전체)하고 session.exits에 청산 조각을 기록
function applyExit(sess, idx, size) {
  const c = sess.case;
  const dirMul = sess.direction === "buy" ? 1 : -1;
  const ae = avgEntry(sess.legs);
  const exitPrice = c.closes[idx];
  const retPct = dirMul * ((exitPrice - ae) / ae) * 100 - FEE_PCT_PER_TXN;
  sess.exits.push({ idx, barOffset: idx - sess.case.tLocal, price: exitPrice, size, retPct });
  sess.openSize -= size;
}

function buildFinalRecord(sess) {
  const c = sess.case;
  const totalExitSize = sess.exits.reduce((a, b) => a + b.size, 0);
  const retPct = sess.exits.reduce((a, b) => a + b.retPct * b.size, 0) / totalExitSize;
  const lastExit = sess.exits[sess.exits.length - 1];
  const record = {
    order: c.order,
    caseId: c.id,
    tf: c.tf,
    symbol: c.symbol,
    action: sess.direction,
    tags: [...new Set(sess.legs.flatMap(l => l.tags))],
    entries: sess.legs.map(l => ({ idx: l.idx, barOffset: l.idx - c.tLocal, price: l.price, size: l.size, tags: l.tags, note: l.note })),
    exits: sess.exits.map(e => ({ idx: e.idx, barOffset: e.barOffset, price: e.price, size: e.size, retPct: e.retPct })),
    totalEntrySize: totalLegSize(sess.legs),
    exitIdx: lastExit.idx,
    exitBarOffset: lastExit.barOffset,
    exitPrice: lastExit.price,
    numLegs: sess.legs.length,
    numExits: sess.exits.length,
    retPct,
    mfe: sess.mfe,
    mae: sess.mae,
    forcedClose: !!sess.forced,
    decidedAt: Date.now(),
  };
  const decisions = loadDecisions();
  decisions.push(record);
  saveDecisions(decisions);
  return record;
}

function finalizeHold(c, exitIdx) {
  const record = {
    order: c.order, caseId: c.id, tf: c.tf, symbol: c.symbol,
    action: "hold", tags: [], entries: [], exits: [], totalEntrySize: 0,
    exitIdx, exitBarOffset: exitIdx - c.tLocal, exitPrice: c.closes[exitIdx],
    numLegs: 0, numExits: 0, retPct: 0, mfe: 0, mae: 0, forcedClose: false, decidedAt: Date.now(),
  };
  const decisions = loadDecisions();
  decisions.push(record);
  saveDecisions(decisions);
  return record;
}

function send(res, status, obj) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", chunk => (body += chunk));
    req.on("end", () => { try { resolve(JSON.parse(body || "{}")); } catch (e) { reject(e); } });
  });
}

function serveStatic(req, res) {
  let filePath = req.url === "/" ? "/public/index.html" : req.url;
  filePath = path.join(ROOT, filePath);
  if (!filePath.startsWith(ROOT)) { res.writeHead(403); res.end(); return; }
  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404); res.end("not found"); return; }
    const ext = path.extname(filePath);
    const type = ext === ".html" ? "text/html" : ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css" : "application/octet-stream";
    res.writeHead(200, { "Content-Type": type + "; charset=utf-8" });
    res.end(data);
  });
}

function positionSummary(sess, atIdx) {
  const c = sess.case;
  const ae = avgEntry(sess.legs);
  const dirMul = sess.direction === "buy" ? 1 : -1;
  const realizedSoFar = sess.exits.length
    ? sess.exits.reduce((a, b) => a + b.retPct * b.size, 0) / sess.exits.reduce((a, b) => a + b.size, 0)
    : null;
  return {
    direction: sess.direction,
    avgEntry: ae,
    numLegs: sess.legs.length,
    openSize: sess.openSize,
    totalEntrySize: totalLegSize(sess.legs),
    numExits: sess.exits.length,
    realizedSoFarPct: realizedSoFar,
    unrealizedPct: dirMul * ((c.closes[atIdx] - ae) / ae) * 100,
    mfe: sess.mfe,
    mae: sess.mae,
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");

  try {
    if (url.pathname === "/api/next") {
      const { remaining, completed } = pickNextCase();
      if (!remaining.length) { send(res, 200, { done: true, total: CASES.length, completed }); return; }
      const c = remaining[0];
      session = { case: c, direction: null, legs: [], exits: [], openSize: 0, mfe: 0, mae: 0, curIdx: c.tLocal, status: "awaiting_entry" };
      send(res, 200, {
        done: false,
        progress: { completed, total: CASES.length },
        case: { id: c.id, tf: c.tf, symbol: c.symbol, order: c.order, tLocal: c.tLocal, warmup: c.warmup, display: c.display, ...visibleSlice(c, c.tLocal) },
      });
      return;
    }

    if (url.pathname === "/api/decide" && req.method === "POST") {
      const payload = await readBody(req);
      if (!session || session.status !== "awaiting_entry") { send(res, 400, { error: "진행 중인 케이스 없음" }); return; }
      const c = session.case;

      if (payload.action === "hold") {
        // 관망 = 포기가 아니라 "다음 캔들까지 지켜보고 다시 판단"
        const nextIdx = session.curIdx + 1;
        const reachedEnd = nextIdx > c.tLocal + c.h;
        if (reachedEnd) {
          const record = finalizeHold(c, session.curIdx);
          session = null;
          send(res, 200, { finalized: true, record });
          return;
        }
        session.curIdx = nextIdx;
        send(res, 200, {
          finalized: false,
          watching: true,
          newBar: { idx: nextIdx, barOffset: nextIdx - c.tLocal, time: c.times[nextIdx], high: c.highs[nextIdx], low: c.lows[nextIdx], close: c.closes[nextIdx], volume: c.volumes[nextIdx] },
          atEnd: nextIdx >= c.tLocal + c.h,
        });
        return;
      }

      if (payload.action !== "buy" && payload.action !== "sell") { send(res, 400, { error: "invalid action" }); return; }
      if (!payload.tags || !payload.tags.length) { send(res, 400, { error: "tags required" }); return; }
      const size = Number(payload.size);
      if (!size || size <= 0) { send(res, 400, { error: "비중(size)은 0보다 커야 함" }); return; }
      const entryIdx = session.curIdx; // 관망하며 지켜보다가 진입한 시점(반드시 tLocal일 필요 없음)
      session.direction = payload.action;
      session.legs.push({ idx: entryIdx, price: c.closes[entryIdx], size, tags: payload.tags, note: payload.note || "" });
      session.openSize = size;
      session.status = "open";

      const nextIdx = entryIdx + 1;
      const reachedEnd = nextIdx > c.tLocal + c.h;
      if (reachedEnd) {
        session.forced = true;
        applyExit(session, entryIdx, session.openSize);
        const record = buildFinalRecord(session);
        session = null;
        send(res, 200, { finalized: true, record, forcedAtEntry: true });
        return;
      }
      updateMfeMae(session, nextIdx);
      session.curIdx = nextIdx;
      send(res, 200, { finalized: false, newBar: barPayload(c, nextIdx), position: positionSummary(session, nextIdx), atEnd: nextIdx >= c.tLocal + c.h });
      return;
    }

    if (url.pathname === "/api/step" && req.method === "POST") {
      const payload = await readBody(req);
      if (!session || session.status !== "open") { send(res, 400, { error: "열린 포지션 없음" }); return; }
      const c = session.case;
      const idx = session.curIdx;

      if (payload.choice === "add") {
        if (!payload.tags || !payload.tags.length) { send(res, 400, { error: "tags required" }); return; }
        const size = Number(payload.size);
        if (!size || size <= 0) { send(res, 400, { error: "비중(size)은 0보다 커야 함" }); return; }
        session.legs.push({ idx, price: c.closes[idx], size, tags: payload.tags, note: payload.note || "" });
        session.openSize += size;
      } else if (payload.choice === "reduce") {
        const size = Number(payload.size);
        if (!size || size <= 0) { send(res, 400, { error: "정리 비중은 0보다 커야 함" }); return; }
        if (size > session.openSize + EPS) { send(res, 400, { error: `보유 비중(${session.openSize})보다 많이 정리할 수 없음` }); return; }
        applyExit(session, idx, Math.min(size, session.openSize));
        if (session.openSize <= EPS) {
          const record = buildFinalRecord(session);
          session = null;
          send(res, 200, { finalized: true, record });
          return;
        }
      } else if (payload.choice === "close") {
        applyExit(session, idx, session.openSize);
        const record = buildFinalRecord(session);
        session = null;
        send(res, 200, { finalized: true, record });
        return;
      } else if (payload.choice !== "hold") {
        send(res, 400, { error: "invalid choice" }); return;
      }

      const nextIdx = idx + 1;
      const reachedEnd = nextIdx > c.tLocal + c.h;
      if (reachedEnd) {
        session.forced = true;
        if (session.openSize > EPS) applyExit(session, idx, session.openSize);
        const record = buildFinalRecord(session);
        session = null;
        send(res, 200, { finalized: true, record, forcedClose: true });
        return;
      }
      updateMfeMae(session, nextIdx);
      session.curIdx = nextIdx;
      send(res, 200, { finalized: false, newBar: barPayload(c, nextIdx), position: positionSummary(session, nextIdx), atEnd: nextIdx >= c.tLocal + c.h });
      return;
    }

    if (url.pathname === "/api/decisions") {
      send(res, 200, loadDecisions());
      return;
    }

    serveStatic(req, res);
  } catch (e) {
    send(res, 500, { error: e.message });
  }
});

function barPayload(c, idx) {
  return { idx, barOffset: idx - c.tLocal, time: c.times[idx], high: c.highs[idx], low: c.lows[idx], close: c.closes[idx], volume: c.volumes[idx] };
}

const PORT = 8731;
server.listen(PORT, () => console.log(`블라인드 라벨링 서버 http://localhost:${PORT}`));
