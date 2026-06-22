// edgeX 거래소 실행 레이어의 인터페이스 정의. 지금은 전부 스텁(미구현)이며,
// 전략/리스크/리밸런싱 로직(signalEngine, fundingGuard, rebalancer, portfolioOrchestrator)을
// 먼저 완성한 뒤 이 파일에 실제 edgeX REST + StarkEx 서명 호출을 채워 넣는다.
//
// account: "main" | "sub"
// container: "perp1" | "perp2" | "spot" | "wallet"

function notImplemented(name) {
  throw new Error(`exchangeClient.${name} 미구현 — API 연동 단계에서 채울 자리`);
}

export function getMarkPrice(_symbol) {
  notImplemented("getMarkPrice");
}

export function getPosition(_account, _container, _symbol) {
  // 기대 반환 형태: { entryPrice, size, side, leverage }
  notImplemented("getPosition");
}

export function getBalance(_account, _container) {
  // 기대 반환 형태: { available, equity, marginUsed }
  notImplemented("getBalance");
}

export function placeOrder(_account, _container, _order) {
  // _order: { symbol, side: "buy"|"sell", size, type: "market"|"limit", price? }
  notImplemented("placeOrder");
}

export function closePosition(_account, _container, _symbol) {
  notImplemented("closePosition");
}

// 계좌/컨테이너 간 자금 이전 (Main<->Sub 포함). rebalancer.planRebalance가 만드는
// {from, to, amount} 액션을 실행하는 단계에서 사용.
export function transfer(_fromAccount, _fromContainer, _toAccount, _toContainer, _asset, _amount) {
  notImplemented("transfer");
}
