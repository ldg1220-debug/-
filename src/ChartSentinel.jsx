import { useState, useRef, useEffect } from "react";
import { scanMarket } from "./signalEngine.js";

// 한글/영문 → 실제 티커 매핑. 크립토는 Yahoo Finance 규칙(-USD)에 맞춤.
const TICKER_MAP = {
  // ── 한국 주식 ──
  "삼성전자": "005930.KS", "삼전": "005930.KS",
  "SK하이닉스": "000660.KS", "하이닉스": "000660.KS",
  "LG에너지솔루션": "373220.KS", "엘지엔솔": "373220.KS",
  "현대차": "005380.KS", "기아": "000270.KS",
  "카카오": "035720.KS", "네이버": "035420.KS",
  "셀트리온": "068270.KS", "포스코홀딩스": "005490.KS",
  "삼성바이오로직스": "207940.KS",

  // ── 미국 주식 ──
  "애플": "AAPL", "테슬라": "TSLA", "엔비디아": "NVDA",
  "마이크로소프트": "MSFT", "MS": "MSFT",
  "구글": "GOOGL", "알파벳": "GOOGL",
  "아마존": "AMZN", "메타": "META", "페이스북": "META",
  "넷플릭스": "NFLX", "AMD": "AMD", "인텔": "INTC",
  "팔란티어": "PLTR", "코인베이스": "COIN",

  // ── 크립토 (Yahoo Finance -USD 형식) ──
  "비트코인": "BTC-USD", "BTC": "BTC-USD",
  "이더리움": "ETH-USD", "ETH": "ETH-USD", "이더": "ETH-USD",
  "리플": "XRP-USD", "XRP": "XRP-USD",
  "솔라나": "SOL-USD", "SOL": "SOL-USD",
  "바이낸스코인": "BNB-USD", "BNB": "BNB-USD",
  "에이다": "ADA-USD", "ADA": "ADA-USD", "카르다노": "ADA-USD",
  "도지코인": "DOGE-USD", "DOGE": "DOGE-USD",
  "아발란체": "AVAX-USD", "AVAX": "AVAX-USD",
  "폴카닷": "DOT-USD", "DOT": "DOT-USD",
  "체인링크": "LINK-USD", "LINK": "LINK-USD",
  "폴리곤": "MATIC-USD", "MATIC": "MATIC-USD",
  "트론": "TRX-USD", "TRX": "TRX-USD",
  "라이트코인": "LTC-USD", "LTC": "LTC-USD",
  "시바이누": "SHIB-USD", "SHIB": "SHIB-USD",
  "수이": "SUI-USD", "SUI": "SUI-USD",
};

// 정확 매칭용 크립토 심볼 셋 (substring 매칭 시 SOLV/ADANI 등 false positive 방지)
const CRYPTO_SYMBOLS = new Set([
  "BTC", "ETH", "XRP", "SOL", "BNB", "ADA", "DOGE", "AVAX",
  "DOT", "LINK", "MATIC", "TRX", "LTC", "SHIB", "SUI",
]);

const isCrypto = (ticker) => {
  if (!ticker) return false;
  const t = ticker.toUpperCase();
  // "BTC-USD" 또는 순수 "BTC" 모두 처리
  const base = t.split("-")[0];
  return CRYPTO_SYMBOLS.has(base);
};

// 크립토 심볼 → CoinGecko 코인 ID (Binance 접근 제한 시 폴백으로 사용)
const COINGECKO_ID_MAP = {
  BTC: "bitcoin", ETH: "ethereum", XRP: "ripple", SOL: "solana",
  BNB: "binancecoin", ADA: "cardano", DOGE: "dogecoin", AVAX: "avalanche-2",
  DOT: "polkadot", LINK: "chainlink", MATIC: "matic-network", TRX: "tron",
  LTC: "litecoin", SHIB: "shiba-inu", SUI: "sui",
};

// CoinGecko market_chart API에서 일별 가격을 가져와 캔버스에 라인 차트로 렌더링하고
// base64 PNG로 반환한다 (Vision 분석에 이미지로 투입하기 위함).
async function fetchCoinGeckoChartImage(resolvedTicker, days = 30) {
  const symbol = resolvedTicker.toUpperCase().split("-")[0];
  const coinId = COINGECKO_ID_MAP[symbol];
  if (!coinId) throw new Error(`CoinGecko 매핑 없음: ${symbol}`);

  const res = await fetch(
    `https://api.coingecko.com/api/v3/coins/${coinId}/market_chart?vs_currency=usd&days=${days}`
  );
  if (!res.ok) throw new Error(`CoinGecko HTTP ${res.status}`);
  const data = await res.json();
  const prices = data.prices || []; // [[timestamp, price], ...]
  if (prices.length < 2) throw new Error("가격 데이터가 부족합니다.");

  const W = 800, H = 400, PAD = 40;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#04070f";
  ctx.fillRect(0, 0, W, H);

  const values = prices.map((p) => p[1]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  const x = (i) => PAD + (i / (prices.length - 1)) * (W - PAD * 2);
  const y = (v) => H - PAD - ((v - min) / range) * (H - PAD * 2);

  // 그리드
  ctx.strokeStyle = "#0d1f3c";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const gy = PAD + (i / 4) * (H - PAD * 2);
    ctx.beginPath();
    ctx.moveTo(PAD, gy);
    ctx.lineTo(W - PAD, gy);
    ctx.stroke();
  }

  // 가격선
  ctx.strokeStyle = "#38d4c8";
  ctx.lineWidth = 2;
  ctx.beginPath();
  prices.forEach((p, i) => {
    const px = x(i), py = y(p[1]);
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  });
  ctx.stroke();

  // 레이블
  ctx.fillStyle = "#566a85";
  ctx.font = "12px monospace";
  ctx.fillText(`${symbol}/USD · ${days}D · CoinGecko`, PAD, 20);
  ctx.fillText(`High: $${max.toLocaleString()}`, PAD, H - 8);
  ctx.fillText(`Low: $${min.toLocaleString()}`, W - PAD - 140, H - 8);

  return canvas.toDataURL("image/png").split(",")[1]; // base64 only
}

// CoinGecko 시가총액 상위 N개 코인의 id/심볼 목록을 가져온다 (페이지당 최대 250개).
async function fetchTopCoinIds(n) {
  const perPage = Math.min(n, 250);
  const pages = Math.ceil(n / perPage);
  const ids = [];
  for (let page = 1; page <= pages; page++) {
    const res = await fetch(
      `https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=${perPage}&page=${page}`
    );
    if (!res.ok) throw new Error(`마켓 목록 조회 실패: HTTP ${res.status}`);
    const data = await res.json();
    ids.push(...data.map((c) => ({ id: c.id, symbol: c.symbol.toUpperCase() })));
  }
  return ids.slice(0, n);
}

// 단일 코인의 시간봉 가격/거래량 시계열을 가져온다. CoinGecko 무료 티어 레이트리밋(429)에
// 걸리면 8초 대기 후 재시도한다 — 수십~수백 개를 순차 조회할 때 필수.
async function fetchCoinSeries(coinId, days = 90) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const res = await fetch(
      `https://api.coingecko.com/api/v3/coins/${coinId}/market_chart?vs_currency=usd&days=${days}`
    );
    if (res.ok) {
      const data = await res.json();
      return { prices: data.prices.map((p) => p[1]), volumes: data.total_volumes.map((v) => v[1]) };
    }
    if (res.status === 429) {
      await new Promise((r) => setTimeout(r, 8000));
      continue;
    }
    throw new Error(`HTTP ${res.status}`);
  }
  throw new Error("레이트리밋 재시도 초과");
}

// 다종목 스캐너: 상위 N개 코인을 순차 조회해 signalEngine.scanMarket으로 신호를 뽑는다.
// onProgress(done, total)로 진행 상황을 보고한다.
async function scanTopCoins(n, days, onProgress) {
  const coins = await fetchTopCoinIds(n);
  const assets = [];
  for (let i = 0; i < coins.length; i++) {
    const { id, symbol } = coins[i];
    try {
      const { prices, volumes } = await fetchCoinSeries(id, days);
      assets.push({ symbol, prices, volumes });
    } catch {
      // 데이터 부족/조회 실패 종목은 스캔 대상에서 제외하고 계속 진행
    }
    onProgress?.(i + 1, coins.length);
    await new Promise((r) => setTimeout(r, 1300)); // 레이트리밋 회피용 호출 간격
  }
  return scanMarket(assets);
}

function buildSystemPrompt(assetType) {
  const base = `당신은 Goldman Sachs & Paradigm Capital 출신의 시니어 퀀트 애널리스트입니다.
차트 이미지와 종목 정보를 바탕으로 심층 분석 리포트를 작성합니다.
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.`;

  if (assetType === "crypto") {
    return `${base}
{
  "ticker": "종목코드",
  "name": "종목명",
  "asset_type": "crypto",
  "current_price": "현재가 (차트에서 읽은 값)",
  "technical": {
    "trend": "상승/하락/횡보",
    "pattern": "발견된 차트 패턴",
    "support": "지지선",
    "resistance": "저항선",
    "rsi": "RSI 값 또는 과매수/과매도",
    "volume": "거래량 분석",
    "summary": "기술적 분석 요약 (3줄)"
  },
  "narrative": {
    "cycle": "현재 크립토 사이클 위치 (초기/중반/후기/하락)",
    "macro_crypto": "비트코인 도미넌스, 공포탐욕지수, 기관 유입 등 크립토 거시 환경",
    "catalyst": "상승/하락 촉매 요인 (2-3가지)",
    "on_chain": "온체인 관점 (보유자 심리, 고래 동향 등)",
    "narrative_score": "내러티브 강도 1-10"
  },
  "strategy": {
    "position": "매수/매도/관망",
    "entry": "진입 가격대",
    "target": ["목표가1", "목표가2"],
    "stop_loss": "손절가",
    "confidence": "확신도 1-10",
    "rationale": "전략 근거 (4줄)"
  },
  "risk": "주요 리스크 요인"
}`;
  }

  return `${base}
{
  "ticker": "종목코드",
  "name": "종목명",
  "asset_type": "stock",
  "current_price": "현재가 (차트에서 읽은 값)",
  "technical": {
    "trend": "상승/하락/횡보",
    "pattern": "발견된 차트 패턴",
    "support": "지지선",
    "resistance": "저항선",
    "rsi": "RSI 값 또는 과매수/과매도",
    "volume": "거래량 분석",
    "summary": "기술적 분석 요약 (3줄)"
  },
  "macro": {
    "interest_rate_impact": "금리 환경이 이 종목에 미치는 영향",
    "sector_outlook": "섹터 전망 및 업종 사이클",
    "earnings_catalyst": "실적 및 이벤트 촉매",
    "institutional_flow": "기관 수급 관점",
    "global_risk": "글로벌 리스크 요인 (달러, 지정학 등)",
    "macro_score": "거시경제 우호도 1-10"
  },
  "strategy": {
    "position": "매수/매도/관망",
    "entry": "진입 가격대",
    "target": ["목표가1", "목표가2"],
    "stop_loss": "손절가",
    "confidence": "확신도 1-10",
    "rationale": "전략 근거 (4줄)"
  },
  "risk": "주요 리스크 요인"
}`;
}

const S = {
  wrap: { minHeight: "100vh", background: "#04070f", color: "#dde6f5", fontFamily: "'SF Mono', 'Fira Code', monospace", padding: "0" },
  header: { borderBottom: "1px solid #0d1f3c", padding: "16px 24px", display: "flex", justifyContent: "space-between", alignItems: "center", background: "rgba(4,7,15,0.95)", position: "sticky", top: 0, zIndex: 10 },
  logo: { fontSize: "15px", fontWeight: 700, color: "#38d4c8", letterSpacing: "2px" },
  tabs: { display: "flex", gap: "4px" },
  tab: (active) => ({ background: active ? "rgba(56,212,200,0.12)" : "none", border: active ? "1px solid rgba(56,212,200,0.3)" : "1px solid transparent", color: active ? "#38d4c8" : "#566a85", padding: "6px 16px", borderRadius: "6px", cursor: "pointer", fontSize: "12px", letterSpacing: "1px", fontFamily: "inherit" }),
  main: { maxWidth: "720px", margin: "0 auto", padding: "24px 16px" },
  card: { background: "rgba(8,14,28,0.8)", border: "1px solid #0d1f3c", borderRadius: "10px", padding: "20px", marginBottom: "16px" },
  label: { fontSize: "11px", color: "#38d4c8", letterSpacing: "1.5px", marginBottom: "8px", display: "block" },
  input: { width: "100%", background: "rgba(4,7,15,0.8)", border: "1px solid #1a2d4a", borderRadius: "6px", padding: "10px 14px", color: "#dde6f5", fontFamily: "inherit", fontSize: "14px", outline: "none", boxSizing: "border-box" },
  uploadBox: (hasImg) => ({ border: hasImg ? "1px solid #1a3a5c" : "2px dashed #1a2d4a", borderRadius: "8px", padding: hasImg ? "0" : "40px 20px", textAlign: "center", cursor: "pointer", color: "#566a85", fontSize: "13px", overflow: "hidden", minHeight: "120px", display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(4,7,15,0.5)", transition: "border 0.2s" }),
  btn: (disabled) => ({ width: "100%", padding: "12px", background: disabled ? "#1a2d4a" : "linear-gradient(135deg, #38d4c8, #4a8cd8)", border: "none", borderRadius: "8px", color: disabled ? "#566a85" : "#04070f", fontWeight: 700, fontSize: "14px", cursor: disabled ? "not-allowed" : "pointer", letterSpacing: "1px", fontFamily: "inherit", marginTop: "4px" }),
  divider: { borderColor: "#0d1f3c", margin: "14px 0" },
  tag: (c) => ({ display: "inline-block", padding: "3px 10px", borderRadius: "4px", fontSize: "11px", fontWeight: 700, letterSpacing: "1px", background: c === "매수" ? "rgba(56,216,152,0.12)" : c === "매도" ? "rgba(255,80,100,0.12)" : "rgba(80,130,200,0.12)", color: c === "매수" ? "#38d898" : c === "매도" ? "#ff5064" : "#4a8cd8", border: `1px solid ${c === "매수" ? "#38d89840" : c === "매도" ? "#ff506440" : "#4a8cd840"}` }),
  section: { marginBottom: "16px" },
  sectionTitle: { fontSize: "11px", color: "#566a85", letterSpacing: "2px", marginBottom: "10px", borderLeft: "2px solid #38d4c8", paddingLeft: "8px" },
  row: { display: "flex", justifyContent: "space-between", marginBottom: "6px", fontSize: "13px" },
  rowKey: { color: "#566a85" },
  rowVal: { color: "#dde6f5", textAlign: "right", maxWidth: "60%", wordBreak: "break-word" },
  score: (v) => ({ color: v >= 7 ? "#38d898" : v >= 4 ? "#f0c040" : "#ff5064", fontWeight: 700 }),
  logCard: { background: "rgba(8,14,28,0.6)", border: "1px solid #0d1f3c", borderRadius: "8px", padding: "14px 16px", marginBottom: "10px", display: "flex", justifyContent: "space-between", alignItems: "center" },
  loader: { display: "flex", flexDirection: "column", alignItems: "center", gap: "16px", padding: "40px 0" },
  dot: (d) => ({ width: "8px", height: "8px", borderRadius: "50%", background: "#38d4c8", animation: `pulse 1.2s ${d}s infinite`, display: "inline-block", margin: "0 3px" }),
  badge: (t) => ({ fontSize: "10px", padding: "2px 8px", borderRadius: "3px", background: t === "crypto" ? "rgba(80,80,220,0.15)" : "rgba(56,212,200,0.1)", color: t === "crypto" ? "#8888ff" : "#38d4c8", border: `1px solid ${t === "crypto" ? "#8888ff40" : "#38d4c840"}`, letterSpacing: "1px" }),
};

function Row({ k, v, highlight }) {
  return (
    <div style={S.row}>
      <span style={S.rowKey}>{k}</span>
      <span style={{ ...S.rowVal, ...(highlight ? { color: "#38d4c8", fontWeight: 600 } : {}) }}>{v}</span>
    </div>
  );
}

function ScoreBar({ value, max = 10 }) {
  const pct = (parseInt(value) || 0) / max * 100;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
      <div style={{ flex: 1, height: "4px", background: "#0d1f3c", borderRadius: "2px" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: pct >= 70 ? "#38d898" : pct >= 40 ? "#f0c040" : "#ff5064", borderRadius: "2px", transition: "width 0.5s" }} />
      </div>
      <span style={S.score(parseInt(value))}>{value}/10</span>
    </div>
  );
}

function ReportView({ r }) {
  const isc = r.asset_type === "crypto";
  return (
    <div style={{ ...S.card, borderColor: isc ? "#3a3a7a" : "#0d2a3c" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "16px" }}>
        <div>
          <div style={{ fontSize: "20px", fontWeight: 700, color: "#dde6f5" }}>{r.name}</div>
          <div style={{ fontSize: "12px", color: "#566a85", marginTop: "2px" }}>{r.ticker} · {r.current_price}</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "6px" }}>
          <span style={S.tag(r.strategy.position)}>{r.strategy.position}</span>
          <span style={S.badge(r.asset_type)}>{isc ? "CRYPTO" : "STOCK"}</span>
        </div>
      </div>

      <hr style={S.divider} />

      <div style={S.section}>
        <div style={S.sectionTitle}>TECHNICAL ANALYSIS</div>
        <Row k="추세" v={r.technical.trend} highlight />
        <Row k="패턴" v={r.technical.pattern} />
        <Row k="지지선" v={r.technical.support} />
        <Row k="저항선" v={r.technical.resistance} />
        <Row k="RSI" v={r.technical.rsi} />
        <Row k="거래량" v={r.technical.volume} />
        <div style={{ marginTop: "10px", fontSize: "13px", color: "#8fa8c8", lineHeight: "1.7", background: "rgba(56,212,200,0.04)", padding: "10px", borderRadius: "6px" }}>
          {r.technical.summary}
        </div>
      </div>

      <hr style={S.divider} />

      {isc ? (
        <div style={S.section}>
          <div style={S.sectionTitle}>CRYPTO NARRATIVE</div>
          <Row k="사이클 위치" v={r.narrative.cycle} highlight />
          <Row k="거시 환경" v={r.narrative.macro_crypto} />
          <Row k="촉매 요인" v={r.narrative.catalyst} />
          <Row k="온체인" v={r.narrative.on_chain} />
          <div style={{ marginTop: "8px", display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ fontSize: "12px", color: "#566a85" }}>내러티브 강도</span>
            <div style={{ flex: 1 }}><ScoreBar value={r.narrative.narrative_score} /></div>
          </div>
        </div>
      ) : (
        <div style={S.section}>
          <div style={S.sectionTitle}>MACRO ANALYSIS</div>
          <Row k="금리 영향" v={r.macro.interest_rate_impact} />
          <Row k="섹터 전망" v={r.macro.sector_outlook} highlight />
          <Row k="실적 촉매" v={r.macro.earnings_catalyst} />
          <Row k="기관 수급" v={r.macro.institutional_flow} />
          <Row k="글로벌 리스크" v={r.macro.global_risk} />
          <div style={{ marginTop: "8px", display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ fontSize: "12px", color: "#566a85" }}>거시 우호도</span>
            <div style={{ flex: 1 }}><ScoreBar value={r.macro.macro_score} /></div>
          </div>
        </div>
      )}

      <hr style={S.divider} />

      <div style={S.section}>
        <div style={S.sectionTitle}>STRATEGY</div>
        <Row k="진입가" v={r.strategy.entry} highlight />
        <Row k="목표가" v={r.strategy.target?.join(" → ")} />
        <Row k="손절가" v={r.strategy.stop_loss} />
        <div style={{ marginTop: "8px", display: "flex", alignItems: "center", gap: "10px" }}>
          <span style={{ fontSize: "12px", color: "#566a85" }}>확신도</span>
          <div style={{ flex: 1 }}><ScoreBar value={r.strategy.confidence} /></div>
        </div>
        <div style={{ marginTop: "10px", fontSize: "13px", color: "#8fa8c8", lineHeight: "1.7", background: "rgba(74,140,216,0.06)", padding: "10px", borderRadius: "6px" }}>
          {r.strategy.rationale}
        </div>
      </div>

      <hr style={S.divider} />

      <div style={{ fontSize: "12px", color: "#566a85" }}>
        <span style={{ color: "#ff5064", marginRight: "6px" }}>⚠</span>{r.risk}
      </div>
    </div>
  );
}

function Loader() {
  return (
    <div style={S.loader}>
      <style>{`@keyframes pulse { 0%,80%,100%{opacity:0.2} 40%{opacity:1} }`}</style>
      <div>
        <span style={S.dot(0)} /><span style={S.dot(0.2)} /><span style={S.dot(0.4)} />
      </div>
      <div style={{ fontSize: "12px", color: "#38d4c8", letterSpacing: "2px" }}>AI ANALYZING CHART...</div>
      <div style={{ fontSize: "11px", color: "#566a85" }}>차트 패턴 · 거시지표 · 내러티브 분석 중</div>
    </div>
  );
}

function parseReport(text) {
  if (!text) return null;
  const stripped = text.replace(/```json|```/g, "").trim();
  try { return JSON.parse(stripped); } catch {}
  const m = stripped.match(/\{[\s\S]*\}/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

export default function ChartSentinel() {
  const [tab, setTab] = useState("analyze");
  const [tickerInput, setTickerInput] = useState("");
  const [image, setImage] = useState(null);
  const [imageB64, setImageB64] = useState(null);
  const [imageMime, setImageMime] = useState("image/jpeg");
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState(null);
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState(null);
  const [fetchingChart, setFetchingChart] = useState(false);
  const [scanN, setScanN] = useState(30);
  const [scanning, setScanning] = useState(false);
  const [scanProgress, setScanProgress] = useState(null);
  const [scanResults, setScanResults] = useState(null);
  const [scanError, setScanError] = useState(null);
  const fileRef = useRef(null);

  const key = tickerInput.trim();
  const resolvedTicker = TICKER_MAP[key] || TICKER_MAP[key.toUpperCase()] || key.toUpperCase();
  const tickerIsCrypto = isCrypto(resolvedTicker);

  const autoFetchChart = async () => {
    setFetchingChart(true);
    setError(null);
    try {
      const b64 = await fetchCoinGeckoChartImage(resolvedTicker);
      setImage(`data:image/png;base64,${b64}`);
      setImageB64(b64);
      setImageMime("image/png");
    } catch (e) {
      setError("차트 자동 불러오기 실패: " + e.message);
    } finally {
      setFetchingChart(false);
    }
  };

  // 이미지 미리보기 URL 메모리 누수 방지
  useEffect(() => {
    return () => { if (image) URL.revokeObjectURL(image); };
  }, [image]);

  const onFile = (f) => {
    if (!f || !f.type.startsWith("image/")) return;
    if (image) URL.revokeObjectURL(image);
    setImage(URL.createObjectURL(f));
    setImageMime(f.type);
    const r = new FileReader();
    r.onload = () => setImageB64(r.result.split(",")[1]);
    r.readAsDataURL(f);
  };

  const onDrop = (e) => {
    e.preventDefault();
    onFile(e.dataTransfer.files[0]);
  };

  const clearImage = () => {
    if (image) URL.revokeObjectURL(image);
    setImage(null);
    setImageB64(null);
  };

  const analyze = async () => {
    if (!tickerInput.trim() && !imageB64) {
      setError("종목명 또는 차트 이미지를 입력하세요.");
      return;
    }
    setLoading(true);
    setError(null);
    setReport(null);

    const resolved = resolvedTicker;
    const assetType = isCrypto(resolved) ? "crypto" : "stock";
    const sys = buildSystemPrompt(assetType);

    const userContent = [];
    if (imageB64) {
      userContent.push({
        type: "image",
        source: { type: "base64", media_type: imageMime, data: imageB64 },
      });
    }
    userContent.push({
      type: "text",
      text: `종목: ${key || resolved} (${resolved})\n${imageB64 ? "첨부된 차트를 분석하여 " : ""}${assetType === "crypto" ? "크립토 내러티브 및 온체인 관점을 포함한" : "거시경제 관점을 포함한"} 심층 분석 리포트를 JSON으로 작성해주세요.`,
    });

    try {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "claude-sonnet-4-20250514",
          max_tokens: 2000,
          system: sys,
          messages: [{ role: "user", content: userContent }],
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.error?.message || `HTTP ${res.status}`);
      }

      const text = data.content?.map((b) => b.text || "").join("") || "";
      const parsed = parseReport(text);
      if (!parsed) {
        console.error("Raw response:", text);
        throw new Error("분석 결과를 파싱할 수 없습니다. 다시 시도해주세요.");
      }

      setReport(parsed);
      setLogs((prev) => [{ ...parsed, analyzedAt: new Date().toLocaleString("ko-KR") }, ...prev].slice(0, 20));
    } catch (e) {
      setError("분석 실패: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  const canSubmit = !loading && (tickerInput.trim() || imageB64);

  const runScan = async () => {
    setScanning(true);
    setScanError(null);
    setScanResults(null);
    setScanProgress({ done: 0, total: scanN });
    try {
      const results = await scanTopCoins(scanN, 90, (done, total) => setScanProgress({ done, total }));
      setScanResults(results);
    } catch (e) {
      setScanError("스캔 실패: " + e.message);
    } finally {
      setScanning(false);
    }
  };

  return (
    <div style={S.wrap}>
      <style>{`* { box-sizing:border-box; } input:focus { border-color:#38d4c8 !important; } button:hover:not(:disabled) { opacity:0.85; }`}</style>
      <div style={S.header}>
        <span style={S.logo}>◈ CHART SENTINEL v5</span>
        <div style={S.tabs}>
          {[["analyze", "분석"], ["scanner", "스캐너"], ["logs", "기록"]].map(([v, l]) => (
            <button key={v} type="button" style={S.tab(tab === v)} onClick={() => setTab(v)}>{l}</button>
          ))}
        </div>
      </div>

      <div style={S.main}>
        {tab === "analyze" ? (
          <>
            <div style={S.card}>
              <label style={S.label}>TICKER / 종목명</label>
              <input
                style={S.input}
                placeholder="예: 삼성전자, NVDA, 비트코인, ETH..."
                value={tickerInput}
                onChange={(e) => setTickerInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && analyze()}
              />
              <div style={{ fontSize: "11px", color: "#566a85", marginTop: "6px" }}>
                한글 입력 가능 · 주식 / 코인 자동 감지
              </div>
            </div>

            <div style={S.card}>
              <label style={S.label}>CHART IMAGE (선택)</label>
              <div
                style={S.uploadBox(!!image)}
                onClick={() => fileRef.current.click()}
                onDrop={onDrop}
                onDragOver={(e) => e.preventDefault()}
              >
                {image ? (
                  <img src={image} style={{ maxWidth: "100%", maxHeight: "320px", borderRadius: "6px" }} alt="chart" />
                ) : (
                  <div>
                    <div style={{ fontSize: "24px", marginBottom: "8px" }}>📊</div>
                    <div>클릭 또는 드래그로 차트 업로드</div>
                    <div style={{ fontSize: "11px", marginTop: "4px", color: "#38d4c8" }}>없어도 분석 가능 (텍스트 기반)</div>
                  </div>
                )}
                <input ref={fileRef} type="file" hidden accept="image/*" onChange={(e) => onFile(e.target.files[0])} />
              </div>
              <div style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
                {image && (
                  <button
                    type="button"
                    onClick={clearImage}
                    style={{ background: "none", border: "1px solid #1a2d4a", color: "#566a85", padding: "4px 12px", borderRadius: "4px", cursor: "pointer", fontSize: "11px", fontFamily: "inherit" }}
                  >
                    이미지 제거
                  </button>
                )}
                {tickerIsCrypto && (
                  <button
                    type="button"
                    onClick={autoFetchChart}
                    disabled={fetchingChart}
                    style={{ background: "none", border: "1px solid #38d4c840", color: "#38d4c8", padding: "4px 12px", borderRadius: "4px", cursor: fetchingChart ? "not-allowed" : "pointer", fontSize: "11px", fontFamily: "inherit", opacity: fetchingChart ? 0.6 : 1 }}
                  >
                    {fetchingChart ? "불러오는 중..." : "◈ 차트 자동 불러오기 (CoinGecko)"}
                  </button>
                )}
              </div>
            </div>

            {error && <div style={{ ...S.card, borderColor: "#ff506440", color: "#ff8090", fontSize: "13px" }}>⚠ {error}</div>}

            <button type="button" style={S.btn(!canSubmit)} onClick={analyze} disabled={!canSubmit}>
              {loading ? "ANALYZING..." : "▶  AI 분석 시작"}
            </button>

            {loading && <Loader />}
            {report && !loading && <ReportView r={report} />}
          </>
        ) : tab === "scanner" ? (
          <>
            <div style={S.card}>
              <label style={S.label}>스캔 대상 코인 수 (시가총액 상위 N)</label>
              <input
                style={S.input}
                type="number"
                min={1}
                max={250}
                value={scanN}
                onChange={(e) => setScanN(Math.max(1, Math.min(250, parseInt(e.target.value) || 1)))}
                disabled={scanning}
              />
              <div style={{ fontSize: "11px", color: "#566a85", marginTop: "6px" }}>
                코인당 약 1.3초 소요 (CoinGecko 레이트리밋 회피) · 90일 시간봉 데이터 기준
              </div>
              <button type="button" style={{ ...S.btn(scanning), marginTop: "10px" }} onClick={runScan} disabled={scanning}>
                {scanning ? `스캔 중... (${scanProgress?.done ?? 0}/${scanProgress?.total ?? scanN})` : "▶  시장 스캔 시작"}
              </button>
            </div>

            {scanError && <div style={{ ...S.card, borderColor: "#ff506440", color: "#ff8090", fontSize: "13px" }}>⚠ {scanError}</div>}

            {scanResults && (
              <div style={{ fontSize: "12px", color: "#566a85", marginBottom: "12px", letterSpacing: "1px" }}>
                신호 발생 종목 {scanResults.length}건 (확신도 내림차순)
              </div>
            )}

            {scanResults && scanResults.length === 0 && (
              <div style={{ ...S.card, textAlign: "center", color: "#566a85", fontSize: "13px", padding: "40px" }}>
                현재 신호 조건을 만족하는 종목이 없습니다
              </div>
            )}

            {scanResults?.map((r, i) => (
              <div key={i} style={S.logCard}>
                <div>
                  <div style={{ fontSize: "14px", fontWeight: 600 }}>
                    {r.symbol} <span style={{ color: "#566a85", fontSize: "12px" }}>{r.indicators?.regime}</span>
                  </div>
                  <div style={{ fontSize: "11px", color: "#566a85", marginTop: "3px" }}>
                    진입 {r.entry?.toFixed?.(4) ?? r.entry} · 손절 {r.stopLoss?.toFixed?.(4) ?? r.stopLoss}
                    {r.target ? ` · 목표 ${r.target.map((t) => t.toFixed(4)).join(" → ")}` : ""}
                  </div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "4px" }}>
                  <span style={S.tag(r.position)}>{r.position}</span>
                  <span style={{ fontSize: "11px", color: r.confidence >= 7 ? "#38d898" : "#f0c040" }}>
                    확신도 {r.confidence}/10
                  </span>
                </div>
              </div>
            ))}
          </>
        ) : (
          <>
            <div style={{ fontSize: "12px", color: "#566a85", marginBottom: "16px", letterSpacing: "1px" }}>
              ANALYSIS HISTORY · {logs.length}건
            </div>
            {logs.length === 0 ? (
              <div style={{ ...S.card, textAlign: "center", color: "#566a85", fontSize: "13px", padding: "40px" }}>
                아직 분석 기록이 없습니다
              </div>
            ) : (
              logs.map((l, i) => (
                <div key={i} style={S.logCard}>
                  <div>
                    <div style={{ fontSize: "14px", fontWeight: 600 }}>
                      {l.name} <span style={{ color: "#566a85", fontSize: "12px" }}>({l.ticker})</span>
                    </div>
                    <div style={{ fontSize: "11px", color: "#566a85", marginTop: "3px" }}>{l.analyzedAt}</div>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "4px" }}>
                    <span style={S.tag(l.strategy?.position)}>{l.strategy?.position}</span>
                    <span style={{ fontSize: "11px", color: parseInt(l.strategy?.confidence) >= 7 ? "#38d898" : "#f0c040" }}>
                      확신도 {l.strategy?.confidence}/10
                    </span>
                  </div>
                </div>
              ))
            )}
          </>
        )}
      </div>
    </div>
  );
}
