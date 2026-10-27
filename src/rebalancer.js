// 계좌/레그 매핑
//   Main: perp2 = B(공격적 매매), spot  = C_long(델타뉴트럴 매수)
//   Sub : perp2 = A(박스권 매매), perp1 = C_short(델타뉴트럴 매도)
//
// 리밸런싱 우선순위(폭포식):
//   1) A/B 중 수익난 쪽에서 손실난 쪽으로 내부 이전 (Main↔Sub 계좌 간 이전 포함)
//   2) 그래도 부족하면 델타뉴트럴(C_long+C_short)에서 충당 — 헤지가 깨지지 않도록
//      C_long과 C_short는 항상 같은 금액만큼만 동시에 줄인다(한쪽만 빼면 안 됨)
//   3) A+B가 모두 흑자로 남으면 그 잉여를 C_long/C_short에 동일 비율로 추가(스윕)
export function planRebalance({ pnlA, pnlB, cAvailable }) {
  const actions = [];
  let a = pnlA;
  let b = pnlB;

  if (a < 0 && b > 0) {
    const fill = Math.min(b, -a);
    actions.push({ from: "Main.perp2(B)", to: "Sub.perp2(A)", amount: fill, reason: "내부상계" });
    a += fill;
    b -= fill;
  } else if (b < 0 && a > 0) {
    const fill = Math.min(a, -b);
    actions.push({ from: "Sub.perp2(A)", to: "Main.perp2(B)", amount: fill, reason: "내부상계" });
    b += fill;
    a -= fill;
  }

  const netAB = a + b;

  if (netAB < 0) {
    const deficit = -netAB;
    const drawn = Math.min(deficit, cAvailable);
    if (drawn > 0) {
      const half = drawn / 2;
      actions.push({ from: "Main.spot(C_long)", to: "trading pool", amount: half, reason: "델타뉴트럴 회수(헤지유지를 위해 양쪽 동일 비율)" });
      actions.push({ from: "Sub.perp1(C_short)", to: "trading pool", amount: half, reason: "델타뉴트럴 회수(헤지유지를 위해 양쪽 동일 비율)" });
    }
    if (drawn < deficit) {
      actions.push({ note: `C 잔고 부족 — 미충당 손실 ${(deficit - drawn).toFixed(2)} 발생, 한도/리스크 점검 필요` });
    }
  } else if (netAB > 0) {
    const half = netAB / 2;
    actions.push({ from: "trading pool surplus", to: "Main.spot(C_long)", amount: half, reason: "흑자 스윕(헤지유지를 위해 양쪽 동일 비율)" });
    actions.push({ from: "trading pool surplus", to: "Sub.perp1(C_short)", amount: half, reason: "흑자 스윕(헤지유지를 위해 양쪽 동일 비율)" });
  }

  return { actions, netAB };
}
