/* 스테이블코인 모니터링 — 정적 JSON을 읽어 화면을 그린다. 의존성 없음. */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const GRADE_KO = { sound: "정상", watch: "주의", breach: "경보", unknown: "미측정" };
  const SCALE_BP = 150; // 계기판 눈금 한계

  // ── 포맷 ────────────────────────────────────────────────
  const usd = (n) => {
    if (n == null || !isFinite(n)) return "—";
    const a = Math.abs(n);
    if (a >= 1e12) return (n / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(0) + "M";
    return n.toFixed(0);
  };
  const signed = (n, d = 2, suf = "") =>
    n == null || !isFinite(n) ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(d) + suf;
  const pct = (n, d = 2) => (n == null ? "—" : n.toFixed(d) + "%");
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  const fmtTime = (iso) => {
    const dt = new Date(iso);
    if (isNaN(dt)) return iso || "—";
    return dt.toLocaleString("ko-KR", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Seoul",
    }).replace(/\.\s?/g, ".").replace(/\.$/, "") + " KST";
  };

  // ── 상단 상태 ───────────────────────────────────────────
  function renderStatus(d) {
    const t = d.totals, c = d.concentration, m = d.meta;

    $("#stamp-time").textContent = fmtTime(m.generated_at);
    $("#stamp-src").textContent = m.source || "—";
    $("#stamp-count").textContent = `${m.asset_count}종목`;
    $("#foot-time").textContent = fmtTime(m.generated_at);
    if (m.is_sample) $("#sample-band").hidden = false;

    const g = t.system_grade;
    $("#verdict-dot").className = "dot is-" + g;
    $("#verdict-label").textContent = "시스템 " + (GRADE_KO[g] || g);
    $("#verdict-label").className = "verdict-label t-" + g;

    const notes = {
      breach: `${t.breach_count}개 종목이 경보 구간에 있습니다.`,
      watch: t.watch_count
        ? `${t.watch_count}개 종목이 주의 구간이거나 구조 지표가 관측선을 넘었습니다.`
        : "구조 지표가 관측선을 넘었습니다.",
      sound: "관측 대상 전 종목이 허용 구간 안에 있습니다.",
    };
    $("#verdict-note").textContent = notes[g] || "";

    $("#f-total").textContent = "$" + usd(t.circulating_usd);
    $("#f-total-s").textContent = "1일 순증감 " + (t.net_1d_usd >= 0 ? "+$" : "−$") + usd(Math.abs(t.net_1d_usd));
    $("#f-peg").textContent = `${t.breach_count} / ${t.watch_count}`;
    $("#f-peg").className = "fig-v t-" + (t.breach_count ? "breach" : t.watch_count ? "watch" : "sound");

    const hhiV = c.hhi_issuer, thr = m.thresholds.hhi_concentrated;
    $("#f-hhi").textContent = hhiV.toLocaleString("en-US", { maximumFractionDigits: 0 });
    $("#f-hhi").className = "fig-v t-" + (hhiV >= thr ? "watch" : "sound");
    $("#f-hhi-s").textContent = `상위 3종목 ${c.top3_share.toFixed(1)}% · ${thr.toLocaleString()} 초과 시 고집중`;

    $("#f-algo").textContent = pct(c.algo_share);
    $("#f-algo").className = "fig-v t-" + (c.algo_share >= m.thresholds.algo_share_watch ? "watch" : "sound");
  }

  // ── 시그니처: 페그 편차 계기판 ──────────────────────────
  // 계기판 행에서만 쓰는 클래스는 .grow-asset 로 따로 둔다. .grow 는 동결 조치
  // 탭의 지갑 레인과 공유하므로 여기서 스타일을 얹으면 그쪽까지 번진다.
  let GAUGE_ROWS = []; // 개요 패널이 참조할 행 데이터

  // 이자부(가격 누적형) 상품인지. 판정은 ETL(etl/yield_bearing.json)이 하고
  // 화면은 행에 붙어 온 yield_bearing 플래그만 읽는다. 다만 이 커밋 이전에
  // 만들어진 snapshot.json 에는 그 필드가 없어서, 그런 파일을 열었을 때 USYC 가
  // 다시 +1300bp 로 튀지 않도록 심볼 몇 개만 최소한의 대비책으로 들고 있는다.
  // 목록 관리의 정본은 어디까지나 etl/yield_bearing.json 쪽이다.
  const YB_FALLBACK = new Set(["USYC", "USDY", "OUSG", "USTB", "TBILL"]);
  const isYieldBearing = (a) =>
    a.yield_bearing === true || (a.yield_bearing == null && YB_FALLBACK.has(String(a.symbol || "").toUpperCase()));

  function renderGauge(d) {
    const list = $("#gauge-list");
    const rows = d.assets
      .filter((a) => a.peg_currency === "USD" && !isYieldBearing(a))
      .slice(0, 16);
    GAUGE_ROWS = rows;
    const pos = (bp) => 50 + (clamp(bp, -SCALE_BP, SCALE_BP) / SCALE_BP) * 50;

    list.innerHTML = rows.map((a, i) => {
      const g = a.grade_peg;
      const has = a.dev_bp != null;
      const p = has ? pos(a.dev_bp) : 50;
      const barL = Math.min(50, p), barW = Math.abs(p - 50);
      const ticks = [-100, -50, 50, 100]
        .map((b) => `<i class="gtick" style="left:${pos(b)}%"></i>`).join("");
      return `<li class="grow grow-asset">
        <button type="button" class="gsym sym-btn" data-i="${i}" aria-expanded="false"
          aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button>
        <span class="gstrip" role="img" aria-label="${a.symbol} 페그 편차 ${has ? signed(a.dev_bp, 1) + "bp" : "측정 불가"}">
          <i class="gband"></i>${ticks}<i class="gdatum"></i>
          <i class="gbar is-${g}" style="left:50%;width:0" data-l="${barL}" data-w="${barW}"></i>
          <i class="gmark is-${g}" style="left:50%" data-p="${p}"></i>
        </span>
        <span class="gval t-${g}${has ? "" : " na"}">${has ? signed(a.dev_bp, 1) : "—"}</span>
      </li>`;
    }).join("");

    // 페그선에서 실제 위치로 퍼져나가는 로딩 동작
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const marks = list.querySelectorAll(".gmark");
    const bars = list.querySelectorAll(".gbar");
    const place = (i) => {
      marks[i].style.left = marks[i].dataset.p + "%";
      bars[i].style.left = bars[i].dataset.l + "%";
      bars[i].style.width = bars[i].dataset.w + "%";
    };
    if (reduce) { marks.forEach((_, i) => place(i)); }
    else requestAnimationFrame(() => marks.forEach((_, i) => setTimeout(() => place(i), 60 + i * 45)));

    // [B] 가격 출처와 기준시각
    const basis = d.meta.price_basis
      || "DefiLlama 가격 오라클(다중 소스 집계, 단일 거래소 체결가 아님)";
    $("#gauge-basis").textContent =
      `가격 출처: ${basis} · 기준시각: ${fmtTime(d.meta.generated_at)}`;

    // [C] 계기판에서 빠진 비USD 페그 종목 안내
    const nonUsd = d.assets.filter((a) => a.peg_currency !== "USD");
    const el = $("#gauge-nonusd");
    if (nonUsd.length) {
      const curs = [...new Set(nonUsd.map((a) => a.peg_currency))].slice(0, 3).join("·");
      el.textContent = `${curs} 등 비USD 페그 ${nonUsd.length}종은 제외 — 아래 발행 구조 › 페그 통화별 패널 참고`;
      el.hidden = false;
    } else {
      el.hidden = true;
    }

    // [D] 편차 계산에서 빠진 이자부 상품 안내 — 어떤 종목이 왜 빠졌는지 밝힌다
    const yb = (d.yield_bearing && d.yield_bearing.length)
      ? d.yield_bearing
      : d.assets.filter(isYieldBearing);
    const ybEl = $("#gauge-yield");
    if (ybEl) {
      if (yb.length) {
        const syms = yb.slice(0, 6).map((a) => a.symbol).join("·");
        ybEl.textContent =
          `이번 수집분에서 제외된 이자부 상품 ${yb.length}종: ${syms}`
          + " — 이자가 토큰 가격에 누적되는 구조라 $1이 목표가가 아닙니다. 아래 종목별 현황 표에서 편차가 “—”로 표시됩니다.";
        ybEl.hidden = false;
      } else {
        ybEl.hidden = true;
      }
    }
  }

  // ── 종목 개요 패널 (호버 / 탭 / 키보드) ───────────────────
  // 계기판 행과 "종목별 현황" 표 행이 같은 패널·같은 규칙을 쓴다. 여는 조건과
  // 위치 보정은 전부 여기 한 곳에만 있고, 붙는 자리마다 bindAssetPop 으로
  // "무엇을 트리거로 볼지 / 어떤 데이터를 보여줄지"만 달리 넘긴다.
  //
  // 호버가 되는 기기인지의 판정 기준은 CSS 와 같은 질의문을 쓴다.
  // (style.css 의 @media (hover: hover) and (pointer: fine) 블록과 짝)
  const HOVER_MQ = "(hover: hover) and (pointer: fine)";
  const POP_TRIG = ".sym-btn"; // 개요를 여는 버튼 (계기판·표 공용)
  const canHover = () => matchMedia(HOVER_MQ).matches;

  // 개요는 화면 전체에서 한 번에 하나만 열린다.
  const POP = { trig: null, anchor: null, pinned: false, raf: 0, bound: false };

  function popPlace() {
    const pop = $("#asset-pop");
    if (!pop || !POP.trig) return;
    const r = (POP.anchor || POP.trig).getBoundingClientRect();
    const p = pop.getBoundingClientRect();
    const M = 8; // 화면 가장자리 여백
    // 기본은 행 아래쪽. 아래가 모자라면 위로 뒤집고, 그래도 넘치면 화면 안으로 민다.
    let top = r.bottom + 6;
    if (top + p.height > window.innerHeight - M) top = r.top - p.height - 6;
    top = clamp(top, M, Math.max(M, window.innerHeight - p.height - M));
    let left = r.left + 12;
    left = clamp(left, M, Math.max(M, window.innerWidth - p.width - M));
    pop.style.top = top + "px";
    pop.style.left = left + "px";
  }

  function popClose() {
    const pop = $("#asset-pop");
    if (!POP.trig) return;
    POP.trig.setAttribute("aria-expanded", "false");
    POP.trig.removeAttribute("aria-describedby");
    POP.trig = null; POP.anchor = null; POP.pinned = false;
    if (pop) pop.hidden = true;
  }

  function popOpen(trig, a, anchor, pin) {
    const pop = $("#asset-pop");
    if (!pop || !a) return;
    if (POP.trig && POP.trig !== trig) popClose();
    $("#ap-sym").textContent = a.symbol || "—";
    $("#ap-name").textContent = a.name || "";
    $("#ap-issuer").textContent = a.issuer || "확인 필요";
    $("#ap-country").textContent = a.issuer_country || "확인 필요";
    $("#ap-mcap").textContent = "$" + usd(a.mcap_usd);
    $("#ap-share").textContent = a.share != null ? a.share.toFixed(2) + "%" : "—";
    const note = $("#ap-note");
    note.textContent = a.issuer_note || "";
    note.hidden = !a.issuer_note;

    pop.hidden = false;
    trig.setAttribute("aria-expanded", "true");
    trig.setAttribute("aria-describedby", "asset-pop");
    POP.trig = trig; POP.anchor = anchor || trig; POP.pinned = !!pin;
    popPlace();
  }

  // container 안의 트리거들에 개요를 붙인다. resolve(트리거)는 보여줄 종목
  // 데이터를, anchorOf(트리거)는 패널을 붙일 기준 요소를 돌려준다.
  // container 자체에 위임하므로 안쪽 내용을 다시 그려도 다시 걸 필요가 없다.
  function bindAssetPop(container, resolve, anchorOf) {
    if (!container) return;
    const trigOf = (e) => (e.target.closest ? e.target.closest(POP_TRIG) : null);
    const openFrom = (trig, pin) =>
      popOpen(trig, resolve(trig), anchorOf ? anchorOf(trig) : trig, pin);

    // 데스크톱: 커서를 올리면 뜨고 벗어나면 사라진다.
    container.addEventListener("pointerover", (e) => {
      if (!canHover() || e.pointerType === "touch" || POP.pinned) return;
      const trig = trigOf(e);
      if (trig && trig !== POP.trig) openFrom(trig, false);
    });
    container.addEventListener("pointerout", (e) => {
      if (!canHover() || e.pointerType === "touch" || POP.pinned) return;
      const trig = trigOf(e);
      if (trig && !trig.contains(e.relatedTarget)) popClose();
    });

    // 터치 기기: 탭하면 열리고, 같은 행을 다시 탭하면 닫힌다.
    // 데스크톱에서도 클릭하면 고정되어 커서가 벗어나도 남는다.
    container.addEventListener("click", (e) => {
      const trig = trigOf(e);
      if (!trig) return;
      if (POP.trig === trig && (POP.pinned || !canHover())) popClose();
      else openFrom(trig, true);
    });

    // 키보드: Tab 으로 포커스가 오면 열린다. (닫기는 Escape 와 focusout)
    container.addEventListener("focusin", (e) => {
      const trig = trigOf(e);
      if (trig && trig !== POP.trig && trig.matches(":focus-visible")) openFrom(trig, false);
    });
    container.addEventListener("focusout", (e) => {
      if (POP.pinned) return;
      if (trigOf(e) === POP.trig) popClose();
    });
  }

  function initAssetPop() {
    if (POP.bound) return;
    POP.bound = true;

    const reposition = () => {
      if (!POP.trig || POP.raf) return;
      POP.raf = requestAnimationFrame(() => { POP.raf = 0; popPlace(); });
    };

    // Escape 로 닫는다. 여기서 트리거에 focus() 를 다시 주면 focusin 이 그대로
    // 되받아 개요를 다시 열어버린다. 키보드로 연 경우엔 이미 트리거가 포커스를
    // 갖고 있으니 되돌릴 것도 없다 — 닫기만 한다.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && POP.trig) popClose();
    });

    // 다른 곳을 탭/클릭하면 닫는다.
    document.addEventListener("pointerdown", (e) => {
      const pop = $("#asset-pop");
      if (!POP.trig) return;
      if (e.target.closest && e.target.closest(POP_TRIG)) return;
      if (pop && pop.contains(e.target)) return;
      popClose();
    });

    // 표는 가로로 따로 스크롤되므로 창 스크롤만 봐서는 안 된다(capture).
    addEventListener("scroll", reposition, { passive: true, capture: true });
    addEventListener("resize", reposition);

    bindAssetPop($("#gauge-list"), (t) => GAUGE_ROWS[Number(t.dataset.i)], (t) => t.closest("li"));
    bindAssetPop($("#tbl tbody"), (t) => TABLE_ROWS[Number(t.dataset.i)], null);
  }

  // ── SVG 라인차트 ────────────────────────────────────────
  // 넓은 화면일수록 세로를 키운다. viewBox 비율이 곧 화면 크기가 된다.
  function chartHeight() {
    const w = window.innerWidth;
    if (w >= 1400) return 300;
    if (w >= 1100) return 250;
    if (w >= 760) return 200;
    return 170;
  }

  const fullDate = (t) => new Date(t * 1000).toLocaleDateString("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit",
  });

  // 툴팁은 화면 전체에서 한 번에 하나만 남긴다.
  function hideChartTip(plot) {
    const tip = plot.querySelector(".ch-tip");
    if (!tip || tip.hidden) return;
    tip.hidden = true;
    plot.querySelectorAll(".ch-cross,.ch-dot").forEach((n) => { n.style.opacity = 0; });
  }

  // 차트 밖을 탭/클릭하면 닫는다. 차트를 다시 그려도 중복 등록되지 않게 한 번만 건다.
  let tipDismissBound = false;
  function bindTipDismiss() {
    if (tipDismissBound) return;
    tipDismissBound = true;
    document.addEventListener("pointerdown", (e) => {
      const inPlot = e.target.closest ? e.target.closest(".chart-plot") : null;
      document.querySelectorAll(".chart-plot").forEach((p) => {
        if (p !== inPlot) hideChartTip(p);
      });
    });
  }

  // 차트 타이포·선 굵기는 여기 상수 하나로 모든 차트가 같이 간다.
  // lineChart 를 쓰는 차트(발행잔액·순증감률·프리미엄·코너 흐름 ETH/XRP)는
  // 전부 이 값을 그대로 따르므로, 통일하려면 여기만 고치면 된다.
  const AX_BASE = 520;          // 세로/가로 비율 기준 폭 (아래 REF 참고)
  const AX_SIZE = 12.5;         // 축 라벨 크기 (CSS 픽셀)
  const AX_CHAR = AX_SIZE * 0.62; // 모노스페이스 한 글자 폭 어림값
  const LINE_W = 2.2;           // 데이터 선 굵기
  const LAST_R = 3.6;           // 마지막 데이터 점 반지름

  // viewBox 를 실제 그려지는 폭에 맞춘다. 예전처럼 520 으로 고정해두면 SVG 가
  // 컨테이너 폭에 맞춰 통째로 늘어나기 때문에, 같은 9px·1.6px 를 써도 좁은
  // 2단 차트와 가로 전폭 차트의 글씨·선 굵기가 두 배 가까이 벌어진다. 폭을
  // 실측해 넣으면 viewBox 한 칸이 곧 1 CSS 픽셀이라 여섯 차트가 같은 굵기로
  // 그려진다. 세로 비율은 예전 그대로 유지한다(H = W × 기준높이 / 520).
  function drawChart(el) {
    const st = el._chart;
    if (!st) return;
    let w = Math.round(el.clientWidth);
    if (!w) {
      // 숨은 탭 안이라 폭이 없다. ResizeObserver 가 있으면 탭이 열려 폭이
      // 생기는 순간 다시 불러 주므로 그때 제대로 그린다. (없는 브라우저에서만
      // 기준 폭으로 미리 그려 둔다 — 예전처럼 컨테이너에 맞춰 늘어난다.)
      if (chartRO) return;
      w = AX_BASE;
    }
    if (w === st.w) return;
    st.w = w;
    paintChart(el, st.pts, st.opts, w);
  }

  const chartRO = typeof ResizeObserver === "function"
    ? new ResizeObserver((es) => es.forEach((e) => drawChart(e.target)))
    : null;

  // 숨은 탭 안의 차트는 폭이 0이라 그릴 수 없다. 탭이 열려 폭이 생기는 순간과
  // 창 크기가 바뀌는 순간을 ResizeObserver 가 잡아 같은 자리에서 다시 그린다.
  function lineChart(el, pts, opts) {
    if (!pts || pts.length < 2) {
      el._chart = null;
      el.innerHTML = '<p class="foot">시계열 없음</p>';
      return;
    }
    el._chart = { pts, opts, w: 0 };
    if (chartRO && !el._chartObserved) { el._chartObserved = true; chartRO.observe(el); }
    drawChart(el);
  }

  function paintChart(el, pts, opts, W) {
    const H = Math.round(W * (opts.height || 170) / AX_BASE), mt = 12, mb = 26;
    const xs = pts.map((p) => p.t), ys = pts.map((p) => p.v);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (opts.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
    const padY = (y1 - y0) * 0.12 || 1;
    y0 -= padY; y1 += padY;
    const x0 = Math.min(...xs), x1 = Math.max(...xs);

    // 눈금값과 최신값 라벨을 먼저 만들어 두고, 그 글자 폭만큼 좌우 여백을 잡는다.
    // 축 글씨가 커진 만큼 고정 여백(46/8)으로는 라벨이 잘리기 때문이다.
    const gy = [y0 + (y1 - y0) * 0.08, (y0 + y1) / 2, y1 - (y1 - y0) * 0.08];
    const gLab = gy.map((v) => opts.fmt(v));
    const last = pts[pts.length - 1];
    const lastLab = opts.fmt(last.v);
    const ml = clamp(Math.max(...gLab.map((s) => s.length)) * AX_CHAR + 10, 40, 130);
    const mr = clamp(lastLab.length * AX_CHAR + 14, 14, 140);

    const X = (t) => ml + ((t - x0) / (x1 - x0 || 1)) * (W - ml - mr);
    const Y = (v) => mt + (1 - (v - y0) / (y1 - y0 || 1)) * (H - mt - mb);

    const line = pts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p.v).toFixed(1)).join(" ");
    const base = opts.zero ? Y(0) : H - mb;
    const area = line + ` L${X(x1).toFixed(1)} ${base.toFixed(1)} L${X(x0).toFixed(1)} ${base.toFixed(1)} Z`;

    const grid = gy.map((v, i) => `<line x1="${ml}" x2="${W - mr}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--rule-soft)"/>
      <text x="${(ml - 7).toFixed(1)}" y="${(Y(v) + AX_SIZE * 0.35).toFixed(1)}" text-anchor="end" class="ax">${esc(gLab[i])}</text>`).join("");

    const zeroLine = opts.zero
      ? `<line x1="${ml}" x2="${W - mr}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}" stroke="var(--ink)" stroke-opacity=".5" stroke-width="1.6"/>` : "";

    const tick = (t) => new Date(t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "short" });
    const xt = [pts[0], pts[Math.floor(pts.length / 2)], pts[pts.length - 1]]
      .map((p, i) => `<text x="${X(p.t).toFixed(1)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === 2 ? "end" : "middle"}" class="ax">${tick(p.t)}</text>`).join("");

    // 마지막 점 옆에 최신값을 그대로 붙인다. 축을 훑지 않아도 지금 값이 읽힌다.
    const lx = X(last.t), ly = Y(last.v);
    const lty = clamp(ly + AX_SIZE * 0.35, mt + AX_SIZE * 0.8, H - mb - 2);
    const lastTag =
      `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="${LAST_R}" fill="${opts.color}"/>
       <text x="${(lx + LAST_R + 4).toFixed(1)}" y="${lty.toFixed(1)}" class="ax ax-last"
         style="fill:${opts.color}">${esc(lastLab)}</text>`;

    const cross = opts.interactive
      ? `<line class="ch-cross" x1="0" x2="0" y1="${mt}" y2="${H - mb}" stroke="var(--ink)" stroke-opacity=".45" stroke-dasharray="2 2"/>
         <circle class="ch-dot" cx="0" cy="0" r="3.6" fill="${opts.color}" stroke="var(--card)" stroke-width="1.4"/>` : "";

    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
        aria-label="${opts.label}. 최근값 ${esc(lastLab)}">
      <style>.ax{font-family:var(--mono);font-size:${AX_SIZE}px;font-weight:600;fill:var(--ink)}
        .ax-last{letter-spacing:-.02em}
        .ch-cross,.ch-dot{opacity:0;pointer-events:none}</style>
      ${grid}${zeroLine}
      <path d="${area}" fill="${opts.color}" fill-opacity=".1"/>
      <path d="${line}" fill="none" stroke="${opts.color}" stroke-width="${LINE_W}" stroke-linejoin="round" stroke-linecap="round"/>
      ${lastTag}
      ${xt}${cross}
    </svg>${opts.interactive ? '<div class="ch-tip" hidden></div>' : ""}`;

    if (!opts.interactive) return;

    // ── 크로스헤어 툴팁 ──
    // 데스크톱은 커서 이동으로, 터치 기기는 탭·드래그로 같은 값을 읽는다.
    const svg = el.querySelector("svg");
    const cLine = svg.querySelector(".ch-cross");
    const cDot = svg.querySelector(".ch-dot");
    const tip = el.querySelector(".ch-tip");

    const show = (clientX) => {
      const r = svg.getBoundingClientRect();
      if (!r.width) return;
      const vx = ((clientX - r.left) / r.width) * W;
      let best = 0, bd = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const dx = Math.abs(X(pts[i].t) - vx);
        if (dx < bd) { bd = dx; best = i; }
      }
      const p = pts[best], px = X(p.t), py = Y(p.v);

      cLine.setAttribute("x1", px.toFixed(1));
      cLine.setAttribute("x2", px.toFixed(1));
      cLine.style.opacity = 1;
      cDot.setAttribute("cx", px.toFixed(1));
      cDot.setAttribute("cy", py.toFixed(1));
      cDot.style.opacity = 1;

      tip.innerHTML = `<span class="ch-tip-d">${fullDate(p.t)}</span>` +
        `<span class="ch-tip-v">${opts.fmt(p.v)}</span>`;
      tip.hidden = false;

      // 컨테이너 기준 좌표로 옮기고, 좌우 끝에서 잘리지 않게 민다.
      const box = el.getBoundingClientRect();
      const offX = r.left - box.left, offY = r.top - box.top;
      const half = tip.offsetWidth / 2;
      tip.style.left = clamp((px / W) * r.width + offX, half + 2, box.width - half - 2) + "px";
      tip.style.top = ((py / H) * r.height + offY) + "px";
    };

    svg.addEventListener("pointermove", (e) => show(e.clientX));
    svg.addEventListener("pointerdown", (e) => show(e.clientX));
    // 터치는 손을 떼는 순간 pointerleave 가 따라오므로 마우스일 때만 닫는다.
    // 터치 기기에서는 차트 밖을 탭할 때 bindTipDismiss 가 닫는다.
    svg.addEventListener("pointerleave", (e) => {
      if (e.pointerType !== "touch") hideChartTip(el);
    });
    bindTipDismiss();
  }

  // ── 발행잔액·순증감률 차트 (전체 시장 / 종목별) ──────────
  const TREND = { opts: [], key: "__all__" };
  const isoDate = (t) => new Date(t * 1000).toISOString().slice(0, 10);

  function trendOptions(hist) {
    const all = {
      key: "__all__", short: "전체 시장", label: "전체 시장",
      total: hist.total_circulating || [], flow: hist.net_30d_pct || [],
    };
    const each = (hist.series || [])
      .filter((s) => (s.total_circulating || []).length > 1)
      .map((s) => ({
        key: String(s.id || s.symbol),
        short: s.symbol || "",
        label: s.symbol + (s.name ? ` — ${s.name}` : ""),
        total: s.total_circulating || [],
        flow: s.net_30d_pct || [],
      }));
    return [all, ...each];
  }

  function currentTrend() {
    return TREND.opts.find((o) => o.key === TREND.key) || TREND.opts[0];
  }

  function renderTrend() {
    const o = currentTrend();
    if (!o) return;
    const whole = o.key === "__all__";
    $("#cap-total").textContent = whole ? "총 발행잔액" : `${o.short} 발행잔액`;
    $("#cap-flow").textContent = whole ? "30일 순증감률" : `${o.short} 30일 순증감률`;

    const height = chartHeight();
    lineChart($("#chart-total"), o.total, {
      color: "var(--petrol)", label: `${o.short} 발행잔액 추이`, height, interactive: true,
      fmt: (v) => "$" + usd(v),
    });
    lineChart($("#chart-flow"), o.flow, {
      color: "var(--petrol)", label: `${o.short} 30일 순증감률`, zero: true, height, interactive: true,
      fmt: (v) => v.toFixed(1) + "%",
    });
  }

  // 서버를 거치지 않고 브라우저에서 바로 CSV를 만들어 내려받는다.
  function downloadTrendCsv() {
    const o = currentTrend();
    if (!o || !o.total.length) return;
    const flowBy = new Map(o.flow.map((p) => [p.t, p.v]));
    const lines = ["날짜,발행잔액(USD),30일 순증감률(%)"];
    o.total.forEach((p) => {
      const f = flowBy.get(p.t);
      lines.push(`${isoDate(p.t)},${p.v},${f == null ? "" : f}`);
    });
    // BOM 을 붙여야 엑셀에서 한글 머리글이 깨지지 않는다.
    const blob = new Blob(["﻿" + lines.join("\r\n") + "\r\n"],
      { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `stablecoin-monitor_${o.key === "__all__" ? "market" : o.short}_` +
      `${isoDate(Math.floor(Date.now() / 1000))}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function initTrend(hist) {
    TREND.opts = trendOptions(hist);
    const sel = $("#series-pick"), hint = $("#series-hint");
    sel.innerHTML = TREND.opts
      .map((o) => `<option value="${esc(o.key)}">${esc(o.label)}</option>`).join("");
    if (TREND.opts.length <= 1) {
      sel.disabled = true;
      hint.textContent = "종목별 시계열이 아직 없습니다 — python etl/fetch.py 를 다시 실행하면 채워집니다.";
    } else {
      hint.textContent = `종목별 ${TREND.opts.length - 1}종`;
    }
    sel.addEventListener("change", () => { TREND.key = sel.value; renderTrend(); });
    $("#csv-dl").addEventListener("click", downloadTrendCsv);
    renderTrend();

    // 화면 폭이 바뀌어 차트 높이 구간이 달라질 때만 다시 그린다.
    let timer = 0, lastH = chartHeight();
    addEventListener("resize", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const h = chartHeight();
        if (h !== lastH) { lastH = h; renderTrend(); }
      }, 180);
    });
  }

  // ── 막대 ────────────────────────────────────────────────
  function bars(el, items, max) {
    const m = max || Math.max(...items.map((i) => i.share), 1);
    el.innerHTML = items.map((i) => `<div class="bar-r${i.algo ? " algo" : ""}">
      <span class="bar-l">${i.label}</span>
      <span class="bar-n">${i.share.toFixed(1)}%<span style="color:var(--dim)"> · $${usd(i.amount)}</span></span>
      <span class="bar-t"><span class="bar-f" style="width:${(i.share / m * 100).toFixed(1)}%"></span></span>
    </div>`).join("");
  }

  // ── 표 ──────────────────────────────────────────────────
  let TABLE_ROWS = []; // 개요 패널이 참조할 행 데이터 (GAUGE_ROWS 와 같은 역할)

  function renderTable(d) {
    TABLE_ROWS = d.assets;
    $("#tbl tbody").innerHTML = d.assets.map((a, i) => `<tr>
      <td><button type="button" class="tsym sym-btn" data-i="${i}" aria-expanded="false"
          aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button><span class="tname">${esc(a.name || "")}</span></td>
      <td>${a.mechanism_ko}</td>
      <td>${a.peg_currency}</td>
      <td class="num">$${usd(a.mcap_usd)}</td>
      <td class="num">${a.share.toFixed(2)}%</td>
      <td class="num t-${a.grade_peg}"${a.dev_bp == null && isYieldBearing(a)
        ? ' title="이자부 토큰화 상품 — $1 고정이 목표가 아니라 편차를 계산하지 않습니다"' : ""
      }>${a.dev_bp == null ? "—" : signed(a.dev_bp, 1)}</td>
      <td class="num">${signed(a.chg_7d, 1, "%")}</td>
      <td class="num t-${a.grade_redemption}">${signed(a.chg_30d, 1, "%")}</td>
      <td><span class="pill is-${a.grade} t-${a.grade}">${GRADE_KO[a.grade]}</span></td>
    </tr>`).join("");
  }

  function renderThresholds(t) {
    const items = [
      ["페그 편차", `주의 ±${t.peg_watch_bp}bp · 경보 ±${t.peg_breach_bp}bp`],
      ["30일 순증감률", `주의 ${t.redemption_watch}% · 경보 ${t.redemption_breach}%`],
      ["발행사 집중도", `HHI ${t.hhi_concentrated.toLocaleString()} 초과 시 고집중`],
      ["알고리즘형 비중", `${t.algo_share_watch}% 초과 시 주의`],
      ["관측 하한", `발행잔액 $${usd(t.min_mcap_usd)} 이상`],
    ];
    $("#thr-list").innerHTML = items
      .map(([k, v]) => `<li><span class="thr-k">${k}</span> — <span class="thr-v">${v}</span></li>`).join("");
  }

  // ── 동결 조치 ───────────────────────────────────────────
  const KIND_KO = { freeze: "동결", unfreeze: "해제", seize: "소각" };

  function renderFreeze(f) {
    const sec = $("#freeze-sec");
    if (!f || !f.issuers || !f.issuers.length) return;
    sec.hidden = false;
    $("#freeze-empty").hidden = true;

    const t = f.totals, days = f.meta.lookback_days;
    $("#fz-freeze").textContent = t.freeze.toLocaleString();
    $("#fz-window").textContent = `최근 ${days}일 · 이더리움 메인넷`;
    $("#fz-unfreeze").textContent = t.unfreeze.toLocaleString();
    $("#fz-seize").textContent = t.seize.toLocaleString();
    $("#fz-seize").className = "fig-v" + (t.seize ? " t-breach" : "");
    $("#fz-amount").textContent = t.seized_units ? usd(t.seized_units) : "—";

    if (f.meta.notes && f.meta.notes.length) {
      $("#fz-notes").textContent = "확인 필요 — " + f.meta.notes.join(" / ");
    }

    // 시간축
    const now = Math.floor(Date.now() / 1000), from = now - days * 86400;
    const at = (ts) => clamp((ts - from) / (now - from), 0, 1) * 100;
    const label = (ts) => new Date(ts * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "short" });
    $("#fz-axis").innerHTML =
      `<span style="left:0%">${label(from)}</span>` +
      `<span style="left:50%">${label((from + now) / 2)}</span>` +
      `<span style="left:100%">${label(now)}</span>`;

    // 발행사 레인
    const byIssuer = {};
    (f.events || []).forEach((e) => {
      if (e.kind === "unfreeze") return;
      (byIssuer[e.issuer] = byIssuer[e.issuer] || []).push(e);
    });
    $("#fz-lanes").innerHTML = f.issuers.map((r) => {
      const evs = byIssuer[r.issuer] || [];
      const ticks = evs.map((e) =>
        `<i class="fz-tick${e.kind === "seize" ? " seize" : ""}" style="left:${at(e.t).toFixed(2)}%"></i>`
      ).join("");
      const n = r.freeze + r.seize;
      return `<li class="grow">
        <span class="gsym">${r.symbol}</span>
        <span class="fz-lane" role="img" aria-label="${r.issuer} ${r.symbol} 조치 ${n}건">${ticks}</span>
        <span class="gval">${n.toLocaleString()}</span>
      </li>`;
    }).join("");

    // 최근 조치
    const short = (a) => (a && a.length > 12 ? a.slice(0, 8) + "…" + a.slice(-4) : a || "—");
    $("#fz-tbl tbody").innerHTML = (f.events || []).slice(0, 60).map((e) => `<tr>
      <td>${new Date(e.t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" })}</td>
      <td>${e.issuer} <span class="tname">${e.symbol}</span></td>
      <td class="kind-${e.kind}">${KIND_KO[e.kind]}</td>
      <td class="fz-addr">${short(e.addr)}</td>
      <td class="num">${e.units ? usd(e.units) : "—"}</td>
    </tr>`).join("");
  }

  // ── 김치프리미엄 ────────────────────────────────────────
  function renderPremium(p, hist) {
    if (!p || !p.assets || !p.assets.length) return;
    $("#premium-sec").hidden = false;
    $("#premium-empty").hidden = true;

    const grade = p.basket_grade;
    $("#pm-avg").textContent = signed(p.basket_avg_pct, 2, "%");
    $("#pm-avg").className = "fig-v t-" + grade;
    $("#pm-fx").textContent = `USD/KRW ${p.meta.fx_usdkrw.toLocaleString()}`;

    const byAsset = {};
    p.assets.forEach((a) => (byAsset[a.asset] = a));
    ["btc", "eth", "xrp"].forEach((k) => {
      const a = byAsset[k.toUpperCase()];
      const el = $("#pm-" + k);
      if (!a || a.premium_pct == null) { el.textContent = "—"; return; }
      el.textContent = signed(a.premium_pct, 2, "%");
      el.className = "fig-v t-" + (a.premium_pct >= p.meta.thresholds.breach_pct ? "breach"
        : a.premium_pct >= p.meta.thresholds.watch_pct ? "watch" : "sound");
    });

    if (hist && hist.points && hist.points.length > 1) {
      const pts = hist.points.map((d) => ({ t: Math.floor(new Date(d.date).getTime() / 1000), v: d.premium_pct }));
      lineChart($("#chart-premium"), pts, {
        color: "var(--petrol)", label: "BTC 프리미엄 추이", zero: true,
        fmt: (v) => v.toFixed(1) + "%",
      });
    }
  }

  // ── 온체인 코너 자금흐름 ─────────────────────────────────
  function renderFlow(f) {
    if (!f || !f.totals) return;
    $("#flow-sec").hidden = false;
    $("#flow-empty").hidden = true;

    const t = f.totals;
    $("#fl-net").textContent = (t.net_outflow_usd >= 0 ? "+$" : "−$") + usd(Math.abs(t.net_outflow_usd));
    $("#fl-net").className = "fig-v" + (t.net_outflow_usd > 0 ? " t-watch" : "");
    $("#fl-window").textContent = `최근 ${f.meta.lookback_days}일 · ${f.meta.chain} · ${f.meta.assets.join("+")}`;
    $("#fl-in").textContent = "$" + usd(t.inflow_usd);
    $("#fl-out").textContent = "$" + usd(t.outflow_usd);
    $("#fl-count").textContent = t.event_count.toLocaleString();

    if (f.daily && f.daily.length > 1) {
      const pts = f.daily.map((d) => ({ t: Math.floor(new Date(d.date).getTime() / 1000), v: d.net_outflow_usd }));
      lineChart($("#chart-flow2"), pts, {
        color: "var(--breach)", label: "일별 순유출입", zero: true,
        fmt: (v) => (v >= 0 ? "+" : "−") + usd(Math.abs(v)),
      });
    }

    const flowMax = Math.max(1, ...(f.by_asset || []).map((r) => Math.max(r.inflow, r.outflow)),
                             ...(f.by_exchange || []).map((r) => Math.max(r.inflow, r.outflow)));
    const flowBars = (el, items) => {
      el.innerHTML = items.map((r) => `<div class="bar-r">
        <span class="bar-l">${r.label}</span>
        <span class="bar-n t-${r.net >= 0 ? "watch" : "sound"}">${r.net >= 0 ? "+" : "−"}$${usd(Math.abs(r.net))}
          <span style="color:var(--dim)"> · 유입 $${usd(r.inflow)} · 유출 $${usd(r.outflow)}</span></span>
        <span class="bar-t"><span class="bar-f" style="width:${(Math.max(r.inflow, r.outflow) / flowMax * 100).toFixed(1)}%"></span></span>
      </div>`).join("");
    };
    flowBars($("#fl-asset-bars"), (f.by_asset || []).map((r) => ({
      label: r.asset, inflow: r.inflow, outflow: r.outflow, net: r.net_outflow,
    })));
    flowBars($("#fl-exchange-bars"), (f.by_exchange || []).map((r) => ({
      label: r.exchange, inflow: r.inflow, outflow: r.outflow, net: r.net_outflow,
    })));

    const dirKo = { outflow: "유출", inflow: "유입" };
    $("#fl-tbl tbody").innerHTML = (f.events || []).slice(0, 60).map((e) => `<tr>
      <td>${new Date(e.t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" })}</td>
      <td class="tsym">${e.asset}</td>
      <td class="kind-${e.direction === "outflow" ? "seize" : "freeze"}">${dirKo[e.direction]}</td>
      <td>${e.kr_wallet}</td>
      <td>${e.global_wallet}</td>
      <td class="num">${usd(e.amount)}</td>
    </tr>`).join("");

    $("#fl-coverage").innerHTML = [
      "이더리움 메인넷의 USDT·USDC 이체만 봅니다. 트론·XRP 통로는 포함되지 않습니다.",
      "업비트·빗썸의 태그된 지갑만 봅니다. 코인원은 이용자별 입금주소가 개별 태그되어 있어 단일 지갑으로 묶을 수 없습니다.",
      "해외 비교군은 Binance·OKX·Bybit 각각 잔액이 가장 큰 핫월렛 하나씩입니다. 같은 거래소가 굴리는 다른 지갑들은 빠져 있습니다.",
      "그래서 이 숫자는 실제 순유출의 하한선이지 전체가 아닙니다.",
    ].map((s) => `<li>${s}</li>`).join("");
  }

  // ── 어테스테이션 시차 ────────────────────────────────────
  function renderAttestation(a) {
    if (!a || !a.entries || !a.entries.length) return;
    $("#attest-sec").hidden = false;
    $("#attest-empty").hidden = true;
    $("#attest-tbl tbody").innerHTML = a.entries.map((e) => `<tr>
      <td>${e.issuer} <span class="tname">${e.symbol}</span></td>
      <td>${e.as_of_date}</td>
      <td class="num t-${e.grade}">${e.days_since}일</td>
      <td class="num">${e.reported_circulating != null ? usd(e.reported_circulating) : "—"}</td>
      <td class="num">${e.current_circulating != null ? usd(e.current_circulating) : "—"}</td>
      <td class="num t-${e.grade}">${e.drift_pct != null ? signed(e.drift_pct, 2, "%") : "—"}</td>
      <td><span class="pill is-${e.grade} t-${e.grade}">${GRADE_KO[e.grade] || e.grade}</span></td>
    </tr>`).join("");
    $("#attest-note").textContent = a.meta.maintenance_note || "";
  }

  // ── XRP 코너 자금흐름 ────────────────────────────────────
  function renderFlowXRP(f) {
    if (!f || !f.totals) return;
    $("#flowxrp-sec").hidden = false;
    $("#flowxrp-empty").hidden = true;

    const t = f.totals;
    $("#fx-net").textContent = (t.net_outflow_xrp >= 0 ? "+" : "−") + usd(Math.abs(t.net_outflow_xrp)) + " XRP";
    $("#fx-net").className = "fig-v" + (t.net_outflow_xrp > 0 ? " t-watch" : "");
    $("#fx-window").textContent = `최근 ${f.meta.lookback_days}일 · ${f.meta.chain} · 한국 계정 ${f.meta.kr_account_count}개`;
    $("#fx-in").textContent = usd(t.inflow_xrp) + " XRP";
    $("#fx-out").textContent = usd(t.outflow_xrp) + " XRP";
    $("#fx-count").textContent = t.event_count.toLocaleString();

    if (f.daily && f.daily.length > 1) {
      const pts = f.daily.map((d) => ({ t: Math.floor(new Date(d.date).getTime() / 1000), v: d.net_outflow_xrp }));
      lineChart($("#chart-flowxrp"), pts, {
        color: "var(--breach)", label: "일별 순유출입", zero: true,
        fmt: (v) => (v >= 0 ? "+" : "−") + usd(Math.abs(v)),
      });
    }

    const dirKo = { outflow: "유출", inflow: "유입" };
    $("#fx-tbl tbody").innerHTML = (f.events || []).slice(0, 60).map((e) => `<tr>
      <td>${new Date(e.t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" })}</td>
      <td class="kind-${e.direction === "outflow" ? "seize" : "freeze"}">${dirKo[e.direction]}</td>
      <td>${e.kr_wallet}</td>
      <td>${e.global_wallet}</td>
      <td class="num">${usd(e.amount)}</td>
    </tr>`).join("");

    $("#fx-coverage").innerHTML = [
      "이름이 정확히 'Upbit'/'Bithumb'인 xrpscan 라벨 계정만 봅니다. 'Bithumb Global' 같은 계열사 라벨은 빠져 있습니다.",
      "네이티브 XRP 결제만 집계합니다. RLUSD 등 발행 통화 이체는 빠져 있습니다.",
      "해외 비교군은 Binance·OKX·Bybit로 이름표가 붙은 계정들입니다. 다른 해외 거래소는 빠져 있습니다.",
      "그래서 이 숫자도 실제 순유출의 하한선이지 전체가 아닙니다.",
    ].map((s) => `<li>${s}</li>`).join("");
  }

  // ── 김치프리미엄 실시간 표시 ──────────────────────────────
  // 실제 테스트 결과 Upbit·Binance는 브라우저의 직접 호출(CORS)을 막는다.
  // 그래서 "브라우저가 API를 직접 두드리는" 방식은 작동하지 않는다. 대신
  // etl/live_loop.py 를 로컬(또는 서버)에서 계속 돌려 data/premium.json 을
  // 60초마다 다시 쓰게 하고, 브라우저는 같은 출처(same-origin)인 그 JSON을
  // 60초마다 재요청한다. CORS 문제 자체가 없고, live_loop.py 가 꺼져 있으면
  // 자동으로 "정적" 표시로 돌아간다.
  const LIVE_FRESH_SEC = 90; // live_loop.py 주기(60초)보다 여유를 둔 판정 기준

  async function liveTick() {
    try {
      const r = await fetch("data/premium.json", { cache: "no-cache" });
      if (!r.ok) throw new Error("premium.json " + r.status);
      const p = await r.json();
      renderPremium(p, null); // 수치만 갱신. 시계열 차트는 다시 그리지 않는다.

      const ageSec = (Date.now() - new Date(p.meta.generated_at).getTime()) / 1000;
      const isLive = ageSec < LIVE_FRESH_SEC;
      $("#pm-live-dot")?.classList.toggle("live-on", isLive);
      $("#pm-updated").textContent = isLive
        ? "실시간 갱신 중 · " + new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
        : `정적 값 · 기준시각 ${fmtTime(p.meta.generated_at)} (live_loop.py 미실행)`;
    } catch (e) {
      console.info("프리미엄 재조회 실패:", e.message || e);
    }
  }

  // ── 탭 전환 ─────────────────────────────────────────────
  function initTabs() {
    const tabs = Array.from(document.querySelectorAll(".tab-btn"));
    if (!tabs.length) return;
    const panels = tabs.map((t) => document.getElementById(t.getAttribute("aria-controls")));

    const activate = (i, focus) => {
      tabs.forEach((t, j) => {
        const on = j === i;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        if (panels[j]) panels[j].hidden = !on;
      });
      if (focus) tabs[i].focus();
    };

    tabs.forEach((t, i) => {
      t.addEventListener("click", () => activate(i, false));
      t.addEventListener("keydown", (e) => {
        const n = tabs.length;
        if (e.key === "ArrowRight") { e.preventDefault(); activate((i + 1) % n, true); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); activate((i - 1 + n) % n, true); }
        else if (e.key === "Home") { e.preventDefault(); activate(0, true); }
        else if (e.key === "End") { e.preventDefault(); activate(n - 1, true); }
      });
    });
  }

  // ── 부팅 ────────────────────────────────────────────────
  async function boot() {
    let snap, hist;
    try {
      const [a, b] = await Promise.all([
        fetch("data/snapshot.json", { cache: "no-cache" }),
        fetch("data/history.json", { cache: "no-cache" }),
      ]);
      if (!a.ok) throw new Error("snapshot " + a.status);
      snap = await a.json();
      hist = b.ok ? await b.json() : { total_circulating: [], net_30d_pct: [] };
    } catch (e) {
      $("#verdict-label").textContent = "데이터를 불러오지 못했습니다";
      $("#verdict-note").textContent =
        "data/snapshot.json 이 없습니다. python etl/fetch.py 를 실행한 뒤 새로고침하십시오.";
      console.error(e);
      return;
    }

    renderStatus(snap);
    renderGauge(snap);
    renderTable(snap);
    initAssetPop(); // 계기판·표를 다 그린 뒤 한 번만 건다

    renderThresholds(snap.meta.thresholds);
    initTrend(hist);

    bars($("#mech-bars"), snap.by_mechanism.map((m) => ({
      label: m.label, share: m.share, amount: m.amount, algo: m.mechanism === "algorithmic",
    })));
    bars($("#cur-bars"), snap.by_peg_currency.slice(0, 6).map((c) => ({
      label: c.currency, share: c.share, amount: c.amount,
    })));
    bars($("#chain-bars"), snap.by_chain.slice(0, 8).map((c) => ({
      label: c.chain, share: c.share, amount: c.amount,
    })));

    // 동결 데이터는 Etherscan 키가 있을 때만 생성된다. 없으면 섹션을 숨긴 채 넘어간다.
    try {
      const r = await fetch("data/freeze.json", { cache: "no-cache" });
      if (r.ok) renderFreeze(await r.json());
    } catch (e) {
      console.info("freeze.json 없음 — 동결 섹션 생략");
    }

    // 김치프리미엄은 키 없이 항상 생성된다.
    let premiumShown = false;
    try {
      const [pr, ph] = await Promise.all([
        fetch("data/premium.json", { cache: "no-cache" }),
        fetch("data/premium_history.json", { cache: "no-cache" }),
      ]);
      if (pr.ok) { renderPremium(await pr.json(), ph.ok ? await ph.json() : null); premiumShown = true; }
    } catch (e) {
      console.info("premium.json 없음 — 프리미엄 섹션 생략");
    }

    // 코너 자금흐름도 Etherscan 키가 필요하다.
    try {
      const r = await fetch("data/flow.json", { cache: "no-cache" });
      if (r.ok) renderFlow(await r.json());
    } catch (e) {
      console.info("flow.json 없음 — 자금흐름 섹션 생략");
    }

    // XRP 코너는 키가 필요 없다.
    try {
      const r = await fetch("data/flow_xrp.json", { cache: "no-cache" });
      if (r.ok) renderFlowXRP(await r.json());
    } catch (e) {
      console.info("flow_xrp.json 없음 — XRP 자금흐름 섹션 생략");
    }

    // 어테스테이션 시차는 손으로 갱신되는 데이터다.
    try {
      const r = await fetch("data/attestation.json", { cache: "no-cache" });
      if (r.ok) renderAttestation(await r.json());
    } catch (e) {
      console.info("attestation.json 없음 — 어테스테이션 섹션 생략");
    }

    // 정적 스냅숏을 그린 뒤, 프리미엄만 브라우저가 직접 실시간으로 갱신한다.
    if (premiumShown) {
      if (!document.hidden) liveTick();
      setInterval(() => { if (!document.hidden) liveTick(); }, 60000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) liveTick(); });
    }
  }

  initTabs();
  boot();
})();
