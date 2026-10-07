/* 스테이블코인 모니터링 — 정적 JSON을 읽어 화면을 그린다. 의존성 없음. */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const GRADE_KO = { sound: "정상", watch: "주의", breach: "경보", unknown: "미측정" };
  const SCALE_BP = 150; // 계기판 눈금 한계

  // ── 포맷 ────────────────────────────────────────────────
  // 백만 단위는 1억 미만이면 소수 한 자리까지 둔다("$55M" → "$55.4M").
  // 백만 미만은 천 단위 쉼표를 찍는다("153281" → "153,281").
  const usd = (n) => {
    if (n == null || !isFinite(n)) return "—";
    const a = Math.abs(n);
    if (a >= 1e12) return (n / 1e12).toFixed(2) + "T";
    if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
    return Math.round(n).toLocaleString("en-US");
  };
  // 기준선처럼 딱 떨어지는 값은 ".0" 을 떼어 읽는다("$50.0M" → "$50M").
  const usdR = (n) => usd(n).replace(/\.0(?=[MBT]$)/, "");
  // 감시목록·소형 통화용. 백만 달러 아래도 읽히게 K 단위를 쓴다.
  const usdC = (n) => {
    if (n == null || !isFinite(n)) return "—";
    if (Math.abs(n) >= 1e6) return usd(n);
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K";
    return n.toFixed(0);
  };
  // 점유율. 0.1% 미만은 유효숫자 두 자리로, 0.001% 미만은 부등호로.
  const shareTxt = (v, amount) => {
    if (v == null || !isFinite(v)) return "—";
    if (v === 0) return amount > 0 ? "<0.001%" : "0%";
    if (v < 0.001) return "<0.001%";
    if (v < 0.1) return Number(v.toPrecision(2)) + "%";
    return v.toFixed(1) + "%";
  };
  // 자기 통화 금액. 원·엔은 만·억 단위로 읽는 편이 직관적이다.
  const CUR_SIGN = { KRW: "₩", JPY: "¥" };
  const localAmt = (n, cur) => {
    if (n == null || !isFinite(n)) return "—";
    const sign = CUR_SIGN[cur] || "";
    const tail = sign ? "" : " " + cur;
    const a = Math.abs(n);
    let body;
    if (a >= 1e8) body = (n / 1e8).toFixed(a >= 1e10 ? 0 : 1) + "억";
    else if (a >= 1e4) body = Math.round(n / 1e4).toLocaleString("ko-KR") + "만";
    else body = Math.round(n).toLocaleString("ko-KR");
    return sign + body + tail;
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

  // ── 핵심 문장 ───────────────────────────────────────────
  // 각 절 제목 바로 아래에 "그래서 지금 어떤가"를 한 문장으로 적는다
  // (영란은행 금융안정보고서·영국 정부 통계 차트 지침의 '서술형 제목' 방식).
  // 숫자는 전부 같은 화면에 그려지는 데이터에서 계산한다.
  function setLead(id, html) {
    const el = document.getElementById("lead-" + id);
    if (!el) return;
    el.innerHTML = html || "";
    el.hidden = !html;
  }
  const bold = (s) => `<b>${esc(s)}</b>`;
  const pctTxt = (v, d = 1) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%";

  // ── 오늘의 주요 신호 ─────────────────────────────────────
  // 상단 상태 카드 아래 한 줄 목록. 데이터 파일마다 따로 도착하므로 출처별로
  // 모아 두었다가 도착할 때마다 다시 그린다. 심각도 순, 최대 5개.
  const SIGNALS = {};
  const SEV_RANK = { breach: 3, watch: 2, info: 1 };
  const SEV_KO = { breach: "경보", watch: "주의", info: "참고" };

  function putSignals(source, items) {
    SIGNALS[source] = items || [];
    const all = Object.values(SIGNALS).flat()
      .sort((x, y) => (SEV_RANK[y.sev] || 0) - (SEV_RANK[x.sev] || 0));
    const row = $("#signals-row"), ul = $("#signals");
    if (!row || !ul) return;
    if (!all.length) { row.hidden = true; return; }
    ul.innerHTML = all.slice(0, 5).map((g) => `<li class="sig sig--${g.sev}">
      <span class="sig-sev"><i aria-hidden="true"></i>${SEV_KO[g.sev]}</span>
      <span class="sig-txt">${g.html}</span>
      ${g.tab ? `<a class="sig-go" href="#${g.tab}" data-tab="${g.tab}"${g.target ? ` data-target="${g.target}"` : ""}${g.filter ? ` data-filter="${g.filter}"` : ""}>보기<span class="sr"> — ${esc(g.label || "")}</span></a>` : ""}
    </li>`).join("");
    row.hidden = false;
  }

  // "n분 전" — 데이터가 얼마나 신선한지 기준시각 옆에 붙인다.
  function ageTxt(iso) {
    const t = new Date(iso).getTime();
    if (!isFinite(t)) return "";
    const m = Math.max(0, Math.round((Date.now() - t) / 60000));
    if (m < 1) return "방금";
    if (m < 60) return `${m}분 전`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}시간 전`;
    return `${Math.floor(h / 24)}일 전`;
  }

  // ── 상단 상태 ───────────────────────────────────────────
  function renderStatus(d) {
    const t = d.totals, c = d.concentration, m = d.meta;

    $("#stamp-time").textContent = fmtTime(m.generated_at);
    const ageH = (Date.now() - new Date(m.generated_at).getTime()) / 3.6e6;
    const stamp = $("#stamp-time");
    stamp.insertAdjacentHTML("beforeend",
      ` <span class="stamp-age${ageH > 6 ? " is-stale" : ""}">${esc(ageTxt(m.generated_at))}</span>`);
    if (ageH > 6) stamp.title = "마지막 수집 후 6시간이 넘었습니다. 수집 작업이 지연·실패했을 수 있습니다.";
    $("#stamp-src").textContent = m.source || "—";
    $("#stamp-count").textContent = `${m.asset_count}종목`;
    $("#foot-time").textContent = fmtTime(m.generated_at);
    if (m.is_sample) $("#sample-band").hidden = false;

    const g = t.system_grade;
    const verdict = $("#verdict");
    if (verdict) verdict.setAttribute("data-grade", g || "unknown");
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

    // 합성 위험점수 (구버전 snapshot 에는 없을 수 있음)
    const riskEl = $("#f-risk");
    if (riskEl) {
      const rs = t.risk_score != null ? t.risk_score : (d.risk && d.risk.score);
      const rg = t.risk_grade || (d.risk && d.risk.grade) || "unknown";
      const rgCls = rg === "breach" ? "breach" : rg === "watch" ? "watch" : "sound";
      riskEl.textContent = rs != null ? Number(rs).toFixed(1) : "—";
      riskEl.className = "fig-v t-" + rgCls;
      const thrR = m.thresholds || {};
      const rsNote = $("#f-risk-s");
      if (rsNote) {
        rsNote.textContent = rs != null
          ? `주의 ${thrR.risk_watch ?? 35} · 경보 ${thrR.risk_breach ?? 60}` +
            (t.price_degraded_count ? ` · 가격품질 저하 ${t.price_degraded_count}종` : "")
          : "0–100 · 페그·상환·집중·알고·가격품질";
      }
      // 미니 바: 숫자 아래에 위험 정도를 한눈에
      let meter = riskEl.parentElement && riskEl.parentElement.querySelector(".risk-meter");
      if (!meter && riskEl.parentElement && rs != null) {
        meter = document.createElement("span");
        meter.className = "risk-meter";
        meter.setAttribute("aria-hidden", "true");
        meter.innerHTML = "<i></i>";
        riskEl.parentElement.appendChild(meter);
      }
      if (meter) {
        meter.className = "risk-meter is-" + rgCls;
        const fill = meter.querySelector("i");
        if (fill) {
          const w = Math.max(0, Math.min(100, Number(rs) || 0));
          requestAnimationFrame(() => { fill.style.width = w + "%"; });
        }
      }
    }

    const hhiV = c.hhi_issuer, thr = m.thresholds.hhi_concentrated;
    $("#f-hhi").textContent = hhiV.toLocaleString("en-US", { maximumFractionDigits: 0 });
    $("#f-hhi").className = "fig-v t-" + (hhiV >= thr ? "watch" : "sound");
    $("#f-hhi-s").textContent = `상위 3종목 ${c.top3_share.toFixed(1)}% · ${thr.toLocaleString()} 초과 시 고집중`;

    // 주요 신호 — 종목 경보·주의와 구조 지표
    const sig = [];
    (d.alerts || []).filter((a) => a.grade === "breach").slice(0, 3).forEach((a) => {
      const why = a.grade_peg === "breach"
        ? `페그 ${signed(a.dev_bp, 1)}bp 이탈`
        : a.grade_redemption === "breach" ? `30일 발행잔액 ${signed(a.chg_30d, 1, "%")}` : "경보 구간";
      sig.push({ sev: "breach", tab: "issuance", target: "gauge", label: a.symbol,
        html: `${bold(a.symbol)} ${esc(why)} <span class="sig-dim">· 발행잔액 $${esc(usd(a.mcap_usd))}</span>` });
    });
    // alerts 는 상위 12건만 실려 오므로 건수는 totals 에서 읽는다.
    const watchN = d.totals.watch_count || 0;
    if (watchN) {
      sig.push({ sev: "watch", tab: "issuance", target: "h-table", filter: "alert", label: "주의 종목",
        html: `${bold("주의 " + watchN + "종")} <span class="sig-dim">— 페그 편차 또는 30일 상환·가격 품질이 관측선을 넘음</span>` });
    }
    if (c.hhi_issuer >= m.thresholds.hhi_concentrated) {
      const top = (d.assets || [])[0];
      sig.push({ sev: "info", tab: "issuance", target: "h-struct", label: "발행 집중도",
        html: `발행 집중도 ${bold("HHI " + c.hhi_issuer.toLocaleString("en-US", { maximumFractionDigits: 0 }))} 고집중`
          + (top ? ` <span class="sig-dim">· ${esc(top.symbol)} 한 종목이 ${top.share.toFixed(1)}%</span>` : "") });
    }
    putSignals("snapshot", sig);

    // 발행 구조 핵심 문장
    const top1 = (d.assets || [])[0];
    if (top1) {
      setLead("struct",
        `${bold(top1.symbol)} 한 종목이 ${bold(top1.share.toFixed(1) + "%")}, 상위 3종목이 ${bold(c.top3_share.toFixed(1) + "%")}를 차지합니다`
        + ` (HHI ${c.hhi_issuer.toLocaleString("en-US", { maximumFractionDigits: 0 })}`
        + (c.hhi_issuer >= m.thresholds.hhi_concentrated ? ", 고집중)." : ")."));
    }

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

  // ── 종목 로고 ───────────────────────────────────────────
  // icon_url 은 ETL 이 응답 필드로 슬러그를 만들 수 있었을 때만 채워진다.
  // 비어 있으면(예전 수집분이거나 필드가 없는 경우) 아이콘 자리를 아예 만들지
  // 않고 심볼 텍스트만 남긴다.
  //
  // 외부 CDN 은 언제든 죽을 수 있다. onerror 로 img 만 감추면 감싼 원이 자리를
  // 지키고 있어서 행 높이도 열 정렬도 흔들리지 않는다. 깨진 이미지 아이콘이
  // 보이는 일은 없다.
  const hasIcons = (rows) => rows.some((a) => a && a.icon_url);
  const iconCell = (a) => `<span class="cicon">${a.icon_url
    ? `<img src="${esc(a.icon_url)}" alt="" width="24" height="24" loading="lazy"
        decoding="async" referrerpolicy="no-referrer" onerror="this.hidden=true">`
    : ""}</span>`;

  function renderGauge(d) {
    const list = $("#gauge-list");
    const rows = d.assets
      .filter((a) => a.peg_currency === "USD" && !isYieldBearing(a))
      .slice(0, 16);
    GAUGE_ROWS = rows;
    const pos = (bp) => 50 + (clamp(bp, -SCALE_BP, SCALE_BP) / SCALE_BP) * 50;

    // 아이콘이 하나라도 있으면 모든 행에 자리를 만들어 종목 칸을 넓힌다.
    // 하나도 없으면 예전 폭 그대로 간다.
    const icons = hasIcons(rows);
    list.classList.toggle("has-icons", icons);
    const ghead = document.querySelector("#gauge .ghead");
    if (ghead) ghead.classList.toggle("has-icons", icons);

    list.innerHTML = rows.map((a, i) => {
      const g = a.grade_peg;
      const has = a.dev_bp != null;
      const p = has ? pos(a.dev_bp) : 50;
      const barL = Math.min(50, p), barW = Math.abs(p - 50);
      const ticks = [-100, -50, 50, 100]
        .map((b) => `<i class="gtick" style="left:${pos(b)}%"></i>`).join("");
      return `<li class="grow grow-asset">
        <span class="gsym-cell">${icons ? iconCell(a) : ""}<button type="button" class="gsym sym-btn"
          data-i="${i}" aria-expanded="false"
          aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button></span>
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

    // 핵심 문장 — 허용 구간 안 종목 수와 가장 크게 벗어난 종목
    const measured = rows.filter((a) => a.dev_bp != null);
    if (measured.length) {
      const inBand = measured.filter((a) => Math.abs(a.dev_bp) < d.meta.thresholds.peg_watch_bp).length;
      const worst = measured.reduce((x, y) => (Math.abs(y.dev_bp) > Math.abs(x.dev_bp) ? y : x));
      setLead("gauge",
        `발행잔액 상위 USD 페그 ${measured.length}종 중 ${bold(inBand + "종")}이 허용 구간(±${d.meta.thresholds.peg_watch_bp}bp) 안에 있습니다.`
        + (Math.abs(worst.dev_bp) >= d.meta.thresholds.peg_watch_bp
          ? ` 가장 크게 벗어난 종목은 ${bold(worst.symbol)}(${esc(signed(worst.dev_bp, 1))}bp)입니다.` : ""));
    }

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

  // ── 감시목록 (원화·엔화 스테이블코인) ──────────────────
  // ETL(etl/watchlist.json)이 시총 하한과 무관하게 골라 준 종목. 편차는 자기
  // 통화 기준(dev_bp_local)이고 등급 허용폭은 meta 의 peg_watch_bp/peg_breach_bp.
  let WATCH_ROWS = [];

  const chainMix = (a) => {
    const ch = a.chains || [];
    const tot = ch.reduce((s, c) => s + (c.amount || 0), 0);
    if (!tot) return "";
    const top = ch.slice(0, 4).map((c) => `${c.chain} ${Math.round(c.amount / tot * 100)}%`);
    const rest = (a.chain_count || ch.length) - top.length;
    return top.join(" · ") + (rest > 0 ? ` 외 ${rest}` : "");
  };

  function renderWatchlist(d) {
    const w = d.watchlist;
    const wrap = $("#watch-wrap");
    const tb = $("#tbl-watch");
    if (!w || !w.rows || !w.rows.length) {
      if (wrap) wrap.hidden = true;
      if (tb) tb.hidden = true;
      WATCH_ROWS = [];
      return;
    }
    const m = w.meta || {};
    const rows = w.rows;
    WATCH_ROWS = rows;
    const pos = (bp) => 50 + (clamp(bp, -SCALE_BP, SCALE_BP) / SCALE_BP) * 50;
    const icons = hasIcons(rows);
    const list = $("#watch-gauge");
    list.classList.toggle("has-icons", icons);
    const wb = m.peg_watch_bp ?? 75;
    const band = `<i class="gband gband-fx" style="left:${pos(-wb)}%;width:${pos(wb) - pos(-wb)}%"></i>`;

    list.innerHTML = rows.map((a, i) => {
      const ok = a.status === "ok";
      const dev = ok ? a.dev_bp_local : null;
      const has = dev != null;
      const thin = a.price_reliability === "low";
      const g = ok ? (a.grade_peg_local || "unknown") : "unknown";
      const p = has ? pos(dev) : 50;
      const barL = Math.min(50, p), barW = Math.abs(p - 50);
      const ticks = [-100, -50, 50, 100].map((b) => `<i class="gtick" style="left:${pos(b)}%"></i>`).join("");
      const label = has
        ? `${a.symbol} 자기 통화 기준 페그 편차 ${signed(dev, 1)}bp${thin ? " (저유동 — 등급 미부여)" : ""}`
        : `${a.symbol} ${ok ? "편차 측정 불가" : "원 데이터에 없음"}`;
      return `<li class="grow grow-asset grow-watch">
        <span class="gsym-cell">${icons ? iconCell(a) : ""}<button type="button" class="gsym sym-btn"
          data-i="${i}" aria-expanded="false"
          aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button><span class="gcur">${esc(a.peg_currency)}</span></span>
        <span class="gstrip" role="img" aria-label="${esc(label)}">
          ${band}${ticks}<i class="gdatum"></i>
          ${has ? `<i class="gbar is-${g}" style="left:${barL}%;width:${barW}%"></i>
          <i class="gmark is-${g}${thin ? " hollow" : ""}" style="left:${p}%"></i>` : ""}
        </span>
        <span class="gval t-${g}${has ? "" : " na"}"${thin ? ' title="유통액이 작아 가격 신뢰도가 낮습니다 — 등급 미부여"' : ""}>${
          has ? signed(dev, 1) + (thin ? "*" : "") : ok ? "—" : "미수집"}</span>
      </li>`;
    }).join("");

    const fx = m.fx_rates || {};
    const fxTxt = Object.keys(fx).filter((c) => rows.some((r) => r.peg_currency === c))
      .map((c) => `USD/${c} ${Number(fx[c]).toLocaleString("en-US", { maximumFractionDigits: 2 })}`).join(" · ");
    $("#watch-basis").textContent =
      `가격: DefiLlama · 환율: Frankfurter 기준환율${m.fx_date ? " (" + m.fx_date + ")" : ""}${fxTxt ? " " + fxTxt : ""}`
      + ` · 주의 ±${wb}bp · 경보 ±${m.peg_breach_bp ?? 150}bp`;

    const notes = [];
    rows.forEach((a) => {
      if (a.status !== "ok") notes.push(`<li><strong>${esc(a.symbol)}</strong> — 원 데이터(DefiLlama)에서 찾지 못했습니다. ${esc(a.status_note || "")}</li>`);
      if (a.data_caveat) notes.push(`<li><strong>${esc(a.symbol)}</strong> — ${esc(a.data_caveat)}</li>`);
      else if (a.status_note && a.status === "ok") notes.push(`<li><strong>${esc(a.symbol)}</strong> — ${esc(a.status_note)}</li>`);
    });
    if (rows.some((a) => a.price_reliability === "low"))
      notes.push(`<li>* 유통액 $${usdR(m.min_reliable_mcap_usd ?? 1e6)} 미만 — 가격이 시장에서 매겨진 값인지 믿기 어려워 등급을 매기지 않습니다.</li>`);
    const nEl = $("#watch-notes");
    nEl.innerHTML = notes.join("");
    nEl.hidden = !notes.length;
    wrap.hidden = false;

    // 종목별 현황 표 맨 아래 묶음
    tb.innerHTML = `<tr class="tgroup"><th colspan="10" scope="rowgroup">감시목록 — 발행잔액 하한 미만이어도 항상 표시 · 편차는 자기 통화 기준</th></tr>`
      + rows.map((a, i) => {
        const ok = a.status === "ok";
        const g = ok ? (a.grade_peg_local || "unknown") : "unknown";
        const thin = a.price_reliability === "low";
        return `<tr>
        <td><span class="tsym-cell">${icons ? iconCell(a) : ""}<button type="button" class="tsym sym-btn"
            data-i="${i}" aria-expanded="false"
            aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button><span class="tname">${esc(a.name || a.label || "")}</span></span></td>
        <td class="td-ex">${krLogos(a.symbol)}</td>
        <td>${ok ? esc(a.mechanism_ko) : "—"}</td>
        <td>${esc(a.peg_currency)}</td>
        <td class="num" title="${ok ? esc(localAmt(a.circulating, a.peg_currency)) : ""}">${ok ? "$" + usdC(a.mcap_usd) : "—"}</td>
        <td class="num">${ok ? shareTxt(a.share, a.mcap_usd) : "—"}</td>
        <td class="num t-${g}"${thin ? ' title="유통액이 작아 가격 신뢰도가 낮습니다 — 등급 미부여"' : ""}>${
          ok && a.dev_bp_local != null ? signed(a.dev_bp_local, 1) + (thin ? "*" : "") : "—"}</td>
        <td class="num">${ok ? signed(a.chg_7d, 1, "%") : "—"}</td>
        <td class="num t-${ok ? a.grade_redemption : "unknown"}">${ok ? signed(a.chg_30d, 1, "%") : "—"}</td>
        <td><span class="pill is-${a.grade} t-${a.grade}">${ok ? GRADE_KO[a.grade] : "미수집"}</span></td>
      </tr>`;
      }).join("");
    tb.hidden = false;
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
    // 감시목록 행에만 있는 값: 자기 통화 가격·편차와 체인별 분포
    const isW = a.watch_key != null;
    const setOpt = (id, text) => {
      const show = isW && text;
      $("#" + id).textContent = text || "—";
      $("#" + id).hidden = !show;
      $("#" + id + "-k").hidden = !show;
    };
    if (isW) {
      $("#ap-mcap").textContent = "$" + usdC(a.mcap_usd) + " (" + localAmt(a.circulating, a.peg_currency) + ")";
      $("#ap-share").textContent = shareTxt(a.share, a.mcap_usd);
    }
    setOpt("ap-peg", isW && a.price_local != null
      ? `${(CUR_SIGN[a.peg_currency] || "")}${a.price_local.toFixed(4)} (${signed(a.dev_bp_local, 1)}bp)`
      : "");
    setOpt("ap-chain", isW ? chainMix(a) : "");
    {
      const kr = krText(a.symbol);
      $("#ap-kr").textContent = kr || "—";
      $("#ap-kr").hidden = $("#ap-kr-k").hidden = !kr;
    }
    const noteTxt = [a.issuer_note, isW ? a.data_caveat : ""].filter(Boolean).join(" ");
    const note = $("#ap-note");
    note.textContent = noteTxt;
    note.hidden = !noteTxt;

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
    bindAssetPop($("#watch-gauge"), (t) => WATCH_ROWS[Number(t.dataset.i)], (t) => t.closest("li"));
    bindAssetPop($("#tbl-watch"), (t) => WATCH_ROWS[Number(t.dataset.i)], null);
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

  const CHART_SYNC = {}; // 커서를 함께 움직이는 차트 묶음(이름 → 요소 목록)

  // 툴팁은 화면 전체에서 한 번에 하나만 남긴다(같은 sync 묶음은 예외).
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
      const keep = new Set(inPlot ? [inPlot] : []);
      if (inPlot) Object.values(CHART_SYNC).forEach((g) => { if (g.includes(inPlot)) g.forEach((x) => keep.add(x)); });
      document.querySelectorAll(".chart-plot").forEach((p) => {
        if (!keep.has(p)) hideChartTip(p);
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
      el.innerHTML = `<p class="chart-empty">${esc((opts && opts.empty) || "표시할 시계열이 없습니다.")}</p>`;
      return;
    }
    el._chart = { pts, opts, w: 0 };
    if (chartRO && !el._chartObserved) { el._chartObserved = true; chartRO.observe(el); }
    drawChart(el);
  }

  // 그라데이션 채움은 한 페이지에 여러 차트가 있어도 서로 물리지 않게
  // id 를 하나씩 새로 딴다.
  let gradSeq = 0;

  function paintChart(el, pts, opts, W) {
    // 세로는 폭에 비례해 잡는다(AX_BASE 주석 참고). 다만 발행 현황 탭의 차트는
    // 카드 전폭을 혼자 쓰기 때문에 비례만 따르면 1400px 화면에서 1000px 가 넘게
    // 솟는다. hMin/hMax 를 주는 차트만 그 범위로 눌러 4:1 안팎을 유지한다.
    let H = Math.round(W * (opts.height || 170) / AX_BASE);
    // 좁은 화면에서 비례만 따르면 2단 차트가 110px 안팎으로 납작해진다.
    H = Math.max(H, opts.hMin || 160);
    if (opts.hMax) H = Math.min(H, opts.hMax);
    const mt = 12, mb = 26;
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

    // x축 눈금. 기간이 짧으면(약 5개월 미만) "26년 10월"만 세 번 겹쳐 찍히던
    // 문제가 있어 월/일로 바꾸고, 같은 글자나 서로 겹치는 눈금은 뺀다.
    const shortSpan = (x1 - x0) < 150 * 86400;
    const tick = (t) => shortSpan
      ? (() => { const d = new Date(t * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; })()
      : new Date(t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "short" });
    const cand = [
      { p: pts[0], a: "start" },
      { p: pts[Math.floor(pts.length / 2)], a: "middle" },
      { p: pts[pts.length - 1], a: "end" },
    ].map((c) => ({ ...c, s: tick(c.p.t), x: X(c.p.t) }));
    const lw = (s) => s.length * AX_SIZE * 0.9; // 한글 섞인 라벨 폭 어림값
    const keep = [cand[0], cand[2]];
    const mid = cand[1];
    if (mid.s !== cand[0].s && mid.s !== cand[2].s &&
        mid.x - lw(mid.s) / 2 > cand[0].x + lw(cand[0].s) + 6 &&
        mid.x + lw(mid.s) / 2 < cand[2].x - lw(cand[2].s) - 6) keep.splice(1, 0, mid);
    if (cand[2].s === cand[0].s || cand[2].x - lw(cand[2].s) < cand[0].x + lw(cand[0].s) + 6) {
      keep.splice(keep.indexOf(cand[0]), 1);
    }
    const xt = keep.map((c) =>
      `<text x="${c.x.toFixed(1)}" y="${H - 8}" text-anchor="${c.a}" class="ax">${esc(c.s)}</text>`).join("");

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

    // 위쪽이 진하고 아래로 갈수록 옅어지는 세로 그라데이션. 켜는 차트만 켠다
    // (발행 현황 탭의 두 차트). 나머지 차트는 예전처럼 균일한 옅은 채움이다.
    let defs = "", areaFill = `fill="${opts.color}" fill-opacity=".1"`;
    if (opts.fillGradient) {
      const gid = "chfill" + (++gradSeq);
      defs = `<defs><linearGradient id="${gid}" gradientUnits="userSpaceOnUse"
          x1="0" y1="${mt}" x2="0" y2="${(H - mb).toFixed(1)}">
        <stop offset="0" stop-color="${opts.color}" stop-opacity=".42"/>
        <stop offset=".55" stop-color="${opts.color}" stop-opacity=".16"/>
        <stop offset="1" stop-color="${opts.color}" stop-opacity=".03"/>
      </linearGradient></defs>`;
      areaFill = `fill="url(#${gid})"`;
    }

    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
        aria-label="${opts.label}. 최근값 ${esc(lastLab)}">
      <style>.ax{font-family:var(--mono);font-size:${AX_SIZE}px;font-weight:600;fill:var(--ink)}
        .ax-last{letter-spacing:-.02em}
        .ch-cross,.ch-dot{opacity:0;pointer-events:none}</style>
      ${defs}${grid}${zeroLine}
      <path d="${area}" ${areaFill}/>
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

    // 같은 sync 그룹의 차트끼리는 한쪽에 커서를 올리면 다른 쪽도 같은 날짜를 짚는다.
    const showIdx = (best, fromSync) => {
      const r = svg.getBoundingClientRect();
      if (!r.width) return;
      const p = pts[best], px = X(p.t), py = Y(p.v);

      cLine.setAttribute("x1", px.toFixed(1));
      cLine.setAttribute("x2", px.toFixed(1));
      cLine.style.opacity = 1;
      cDot.setAttribute("cx", px.toFixed(1));
      cDot.setAttribute("cy", py.toFixed(1));
      cDot.style.opacity = 1;

      const rows = opts.tipRows ? opts.tipRows(p) : null;
      tip.innerHTML = `<span class="ch-tip-d">${fullDate(p.t)}</span>` + (rows
        ? rows.map(([k, v, cls]) => `<span class="ch-tip-r${cls ? " " + cls : ""}"><span class="ch-tip-k">${esc(k)}</span><span class="ch-tip-v">${esc(v)}</span></span>`).join("")
        : `<span class="ch-tip-v">${opts.fmt(p.v)}</span>`);
      tip.hidden = false;

      // 컨테이너 기준 좌표로 옮기고, 좌우 끝에서 잘리지 않게 민다.
      const box = el.getBoundingClientRect();
      const offX = r.left - box.left, offY = r.top - box.top;
      const half = tip.offsetWidth / 2;
      tip.style.left = clamp((px / W) * r.width + offX, half + 2, box.width - half - 2) + "px";
      tip.style.top = ((py / H) * r.height + offY) + "px";
      if (opts.sync && !fromSync) {
        (CHART_SYNC[opts.sync] || []).forEach((other) => {
          if (other !== el && other._showT) other._showT(p.t);
        });
      }
    };
    const show = (clientX) => {
      const r = svg.getBoundingClientRect();
      if (!r.width) return;
      const vx = ((clientX - r.left) / r.width) * W;
      let best = 0, bd = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const dx = Math.abs(X(pts[i].t) - vx);
        if (dx < bd) { bd = dx; best = i; }
      }
      showIdx(best, false);
    };
    // 다른 차트에서 넘어온 날짜(t)와 가장 가까운 점을 짚는다.
    el._showT = (t) => {
      let best = 0, bd = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const d = Math.abs(pts[i].t - t);
        if (d < bd) { bd = d; best = i; }
      }
      showIdx(best, true);
    };
    if (opts.sync) {
      const g = (CHART_SYNC[opts.sync] = CHART_SYNC[opts.sync] || []);
      if (!g.includes(el)) g.push(el);
    }
    const hideGroup = () => {
      hideChartTip(el);
      if (opts.sync) (CHART_SYNC[opts.sync] || []).forEach((o) => hideChartTip(o));
    };

    svg.addEventListener("pointermove", (e) => show(e.clientX));
    svg.addEventListener("pointerdown", (e) => show(e.clientX));
    // 터치는 손을 떼는 순간 pointerleave 가 따라오므로 마우스일 때만 닫는다.
    // 터치 기기에서는 차트 밖을 탭할 때 bindTipDismiss 가 닫는다.
    svg.addEventListener("pointerleave", (e) => {
      if (e.pointerType !== "touch") hideGroup();
    });
    bindTipDismiss();
  }

  // ── 발행잔액·순증감률 차트 (전체 시장 / 종목별) ──────────
  // metric 은 세그먼트 토글이 고르는 지표다. 두 차트를 동시에 보여주던 것을
  // 한 번에 하나만 보여주는 방식으로 바꿨다 — 드롭다운·툴팁·CSV 는 그대로다.
  const TREND = { opts: [], key: "__all__", metric: "total", days: 365 };
  // 표시 기간으로 자른 시계열. 기간은 마지막 관측일 기준으로 센다.
  const inRange = (pts) => {
    if (!TREND.days || !pts || !pts.length) return pts || [];
    const from = pts[pts.length - 1].t - TREND.days * 86400;
    return pts.filter((p) => p.t >= from);
  };
  const isoDate = (t) => new Date(t * 1000).toISOString().slice(0, 10);
  const TREND_COLOR = "var(--accent)";

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

  // 카드 머리의 큰 숫자. 지금 고른 종목·지표의 최신값을 그대로 보여준다.
  function renderTrendKpi(o) {
    const flow = TREND.metric === "flow";
    const tag = o.key === "__all__" ? "TOTAL" : (o.short || "").toUpperCase();
    const pts = flow ? o.flow : o.total;
    const last = pts && pts.length ? pts[pts.length - 1] : null;

    $("#tk-label").textContent = `${tag} ${flow ? "30일 순증감률" : "발행잔액"}`;
    $("#tk-value").textContent = last
      ? (flow ? signed(last.v, 2, "%") : "$" + usd(last.v))
      : "—";
    // 차트는 DefiLlama 일별 집계라 상단 '총 발행잔액'(현재 시점 종목 합계)과
    // 수치가 조금 다를 수 있다. 같은 이름의 두 숫자를 혼동하지 않게 밝혀 둔다.
    $("#tk-sub").textContent = last
      ? `최근 관측 ${fullDate(last.t)} · ${pts.length}일 시계열` + (flow ? "" : " · 일별 집계 기준")
      : "시계열 없음";
  }

  function renderTrend() {
    const o = currentTrend();
    if (!o) return;
    const whole = o.key === "__all__";
    $("#cap-total").textContent = whole ? "총 발행잔액" : `${o.short} 발행잔액`;
    $("#cap-flow").textContent = whole ? "30일 순증감률" : `${o.short} 30일 순증감률`;

    // 고르지 않은 쪽은 숨긴다. 숨은 동안에는 폭이 0이라 그려지지 않고,
    // 다시 보이는 순간 ResizeObserver 가 같은 자리에서 그려 준다.
    const flow = TREND.metric === "flow";
    $("#fig-total").hidden = flow;
    $("#fig-flow").hidden = !flow;
    $("#seg-total").setAttribute("aria-pressed", String(!flow));
    $("#seg-flow").setAttribute("aria-pressed", String(flow));
    renderTrendKpi(o);

    const height = chartHeight();
    const box = { height, hMin: 200, hMax: Math.round(height * 1.5) };
    document.querySelectorAll("#trend-range .range-btn").forEach((btn) =>
      btn.setAttribute("aria-pressed", String(Number(btn.dataset.days) === TREND.days)));
    lineChart($("#chart-total"), inRange(o.total), {
      ...box, color: TREND_COLOR, label: `${o.short} 발행잔액 추이`, interactive: true,
      fillGradient: true, fmt: (v) => "$" + usd(v),
    });
    lineChart($("#chart-flow"), inRange(o.flow), {
      ...box, color: TREND_COLOR, label: `${o.short} 30일 순증감률`, zero: true, interactive: true,
      fillGradient: true, fmt: (v) => v.toFixed(1) + "%",
    });
  }

  // 내려받기 이름은 CSV·PNG 가 같은 규칙을 쓴다.
  function trendFileName(ext) {
    const o = currentTrend();
    const who = !o || o.key === "__all__" ? "market" : o.short;
    const what = ext === "png" ? (TREND.metric === "flow" ? "_net30d" : "_supply") : "";
    return `stablecoin-monitor_${who}${what}_${isoDate(Math.floor(Date.now() / 1000))}.${ext}`;
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // 서버를 거치지 않고 브라우저에서 바로 CSV를 만들어 내려받는다.
  // 보이는 차트가 하나뿐이어도 CSV 는 예전처럼 두 열을 다 담는다.
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
    saveBlob(new Blob(["﻿" + lines.join("\r\n") + "\r\n"],
      { type: "text/csv;charset=utf-8" }), trendFileName("csv"));
  }

  // 지금 보이는 차트를 PNG 로 굽는다.
  //
  // SVG 를 문서에서 떼어내면 var(--...) 가 풀리지 않는다 — 선 색과 글꼴이
  // 통째로 날아가므로, 직렬화한 뒤 계산된 값으로 바꿔 넣는다. blob: 대신
  // data: URL 을 쓰는 이유는 캔버스 오염(tainted canvas) 없이 확실히
  // toBlob() 까지 가기 위해서다.
  function downloadTrendPng() {
    exportChartPng(TREND.metric === "flow" ? $("#chart-flow") : $("#chart-total"), trendFileName("png"));
  }
  function exportChartPng(host, filename) {
    const svg = host && host.querySelector("svg");
    if (!svg) return;
    const vb = svg.viewBox.baseVal;
    const W = Math.round(vb.width || host.clientWidth || 520);
    const H = Math.round(vb.height || 170);
    if (!W || !H) return;

    const clone = svg.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", W);
    clone.setAttribute("height", H);
    const cs = getComputedStyle(document.documentElement);
    const markup = new XMLSerializer().serializeToString(clone)
      .replace(/var\(\s*(--[\w-]+)\s*\)/g, (m, name) => (cs.getPropertyValue(name) || "#000").trim());

    const scale = 2; // 보고서에 붙여도 흐리지 않을 정도
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = W * scale;
      cv.height = H * scale;
      const ctx = cv.getContext("2d");
      ctx.fillStyle = (cs.getPropertyValue("--card") || "#fff").trim();
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height);
      cv.toBlob((b) => { if (b) saveBlob(b, filename); });
    };
    img.onerror = () => console.warn("PNG 변환 실패 — 차트를 이미지로 굽지 못했습니다.");
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(markup);
  }

  // 상단 TOTAL 카드에 30일 변화율과 90일 미니 추이선을 붙인다
  // (rwa.xyz·DefiLlama 의 KPI 카드 방식: 값 + 기간 변화 + 작은 추이).
  // 상승·하락에 초록·빨강을 쓰지 않는다 — 발행 증가가 곧 '좋음'은 아니므로
  // 방향은 화살표 모양으로만 알린다.
  function renderTotalTrend(d, hist) {
    const pts = (hist && hist.total_circulating) || [];
    if (pts.length < 31) return;
    const last = pts[pts.length - 1];
    const ago = pts.filter((p) => p.t <= last.t - 30 * 86400).pop();
    if (!ago) return;
    const chg = (last.v / ago.v - 1) * 100;
    const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "■";
    const t = d.totals;
    $("#f-total-s").innerHTML =
      `<span class="delta">${arrow} ${esc(pctTxt(chg, 2))}</span> 30일 · 1일 ${t.net_1d_usd >= 0 ? "+" : "−"}$${esc(usd(Math.abs(t.net_1d_usd)))}`;

    const sp = pts.slice(-90), W = 120, H = 26;
    const ys = sp.map((p) => p.v), lo = Math.min(...ys), hi = Math.max(...ys);
    const path = sp.map((p, i) => `${i ? "L" : "M"}${(i / (sp.length - 1) * W).toFixed(1)} ${(H - 2 - (p.v - lo) / ((hi - lo) || 1) * (H - 4)).toFixed(1)}`).join(" ");
    const fig = $("#f-total").parentElement;
    let svg = fig.querySelector(".spark");
    if (!svg) {
      fig.insertAdjacentHTML("beforeend", `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"></svg>`);
      svg = fig.querySelector(".spark");
    }
    svg.setAttribute("aria-label", `최근 90일 총 발행잔액 추이, 30일 변화 ${pctTxt(chg, 2)}`);
    svg.innerHTML = `<path d="${path}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;

    setLead("trend",
      `최근 30일 발행잔액은 $${esc(usd(ago.v))}에서 $${esc(usd(last.v))}로 ${bold(pctTxt(chg, 2))} `
      + (chg >= 0 ? "늘었습니다 — 상환보다 발행이 많았습니다." : "줄었습니다 — 발행보다 상환이 많았습니다."));
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
    $("#png-dl").addEventListener("click", downloadTrendPng);

    // 표시 기간 (1M·3M·6M·1Y·전체)
    document.querySelectorAll("#trend-range .range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        TREND.days = Number(btn.dataset.days) || 0;
        renderTrend();
      });
    });

    // 세그먼트 토글 — 발행잔액 ↔ 30일 순증감률
    document.querySelectorAll("#trend-seg .seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        const m = b.dataset.metric;
        if (m === TREND.metric) return;
        TREND.metric = m;
        renderTrend();
      });
    });
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
    el.innerHTML = items.map((i) => `<div class="bar-r${i.algo ? " algo" : ""}${i.pinned ? " pinned" : ""}">
      <span class="bar-l">${esc(i.label)}${i.pinned ? '<span class="bar-tag">감시</span>' : ""}</span>
      <span class="bar-n">${shareTxt(i.share, i.amount)}<span style="color:var(--dim)"> · $${usdC(i.amount)}</span></span>
      <span class="bar-t"><span class="bar-f" style="width:${(i.share / m * 100).toFixed(1)}%"></span></span>
    </div>`).join("");
  }

  // ── 표 ──────────────────────────────────────────────────
  let TABLE_ROWS = []; // 개요 패널이 참조할 행 데이터 (GAUGE_ROWS 와 같은 역할)

  // 정렬. 머리글을 누르면 그 열로 정렬하고, 다시 누르면 방향을 뒤집는다.
  // 값이 없는 칸(—)은 방향과 관계없이 항상 맨 아래로 보낸다.
  const SORT_COLS = [
    { key: "symbol", type: "text" },
    { key: "kr", type: "kr", hint: "국내 거래지원 거래소 수 순" },
    { key: "mechanism_ko", type: "text" },
    { key: "peg_currency", type: "text" },
    { key: "mcap_usd", type: "num" },
    { key: "share", type: "num" },
    { key: "dev_bp", type: "abs", hint: "편차 크기(절댓값) 순" },
    { key: "chg_7d", type: "num" },
    { key: "chg_30d", type: "num" },
    { key: "grade", type: "grade" },
  ];
  const GRADE_RANK = { breach: 3, watch: 2, sound: 1, unknown: 0 };
  const SORT = { col: 4, dir: "descending", src: [] };

  const sortVal = (a, c) => {
    const v = a[c.key];
    if (c.type === "grade") return GRADE_RANK[v] ?? -1;
    if (c.type === "kr") return krCount(a.symbol);
    if (c.type === "abs") return v == null ? null : Math.abs(v);
    if (c.type === "num") return v == null || !isFinite(v) ? null : v;
    return v == null ? null : String(v);
  };

  function sortedRows() {
    const c = SORT_COLS[SORT.col];
    const sign = SORT.dir === "ascending" ? 1 : -1;
    return SORT.src
      .map((a, i) => ({ a, i }))
      .sort((x, y) => {
        const vx = sortVal(x.a, c), vy = sortVal(y.a, c);
        if (vx == null && vy == null) return x.i - y.i;
        if (vx == null) return 1;
        if (vy == null) return -1;
        const d = c.type === "text" ? vx.localeCompare(vy, "ko") : vx - vy;
        return d ? d * sign : x.i - y.i;
      })
      .map((r) => r.a);
  }

  function initTableSort() {
    const ths = Array.from(document.querySelectorAll("#tbl thead th"));
    ths.forEach((th, i) => {
      const c = SORT_COLS[i];
      if (!c || th.querySelector(".th-sort")) return;
      const label = th.textContent.trim();
      th.innerHTML = `<button type="button" class="th-sort" title="${esc(c.hint || label + " 기준 정렬")}">${esc(label)}</button>`;
      th.querySelector("button").addEventListener("click", () => {
        if (SORT.col === i) SORT.dir = SORT.dir === "descending" ? "ascending" : "descending";
        else { SORT.col = i; SORT.dir = c.type === "text" ? "ascending" : "descending"; }
        paintTable();
      });
    });
  }

  function paintSortState() {
    document.querySelectorAll("#tbl thead th").forEach((th, i) => {
      if (i === SORT.col) th.setAttribute("aria-sort", SORT.dir);
      else th.removeAttribute("aria-sort");
    });
  }

  function renderTable(d) {
    SORT.src = d.assets || [];
    initTableTools();
    // 표에는 하한 이상 종목 중 발행잔액 상위 60종만 실려 온다. 문장의 건수도
    // 표 안에서 세어야 아래 '경보·주의' 거르기 결과와 맞는다.
    const nB = SORT.src.filter((a) => a.grade === "breach").length;
    const nW = SORT.src.filter((a) => a.grade === "watch").length;
    setLead("table",
      `발행잔액 상위 ${bold(SORT.src.length + "종")} 가운데 경보 ${bold(String(nB))} · 주의 ${bold(String(nW))}종입니다.`);
    initTableSort();
    paintTable();
  }

  // 검색·거르기·더 보기 (DefiLlama·rwa.xyz 의 표 도구줄 방식)
  const TBL = { q: "", f: "all", limit: 25, all: false };
  const PAGE = 25;
  const matchRow = (a) => {
    if (TBL.f === "alert" && !(a.grade === "watch" || a.grade === "breach")) return false;
    if (TBL.f === "kr" && !(krCount(a.symbol) > 0)) return false;
    if (TBL.f !== "all" && TBL.f !== "alert" && TBL.f !== "kr" && a.mechanism !== TBL.f) return false;
    if (TBL.q) {
      const q = TBL.q.toLowerCase();
      return String(a.symbol || "").toLowerCase().includes(q) || String(a.name || "").toLowerCase().includes(q)
        || String(a.issuer || "").toLowerCase().includes(q);
    }
    return true;
  };

  function initTableTools() {
    const q = $("#tbl-q");
    if (!q || q._bound) return;
    q._bound = true;
    let t = 0;
    q.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { TBL.q = q.value.trim(); TBL.all = false; paintTable(); }, 120);
    });
    document.querySelectorAll(".tbl-tools .chip").forEach((c) => {
      c.addEventListener("click", () => {
        TBL.f = c.dataset.f; TBL.all = false;
        document.querySelectorAll(".tbl-tools .chip").forEach((x) =>
          x.setAttribute("aria-pressed", String(x === c)));
        paintTable();
      });
    });
    $("#tbl-more").addEventListener("click", () => { TBL.all = !TBL.all; paintTable(); });
  }

  function paintTable() {
    popClose(); // 다시 그리면 열려 있던 개요의 기준 행이 사라진다
    paintSortState();
    const matched = sortedRows().filter(matchRow);
    const rows = TBL.all ? matched : matched.slice(0, PAGE);
    const total = SORT.src.length;
    $("#tbl-count").textContent = matched.length === total
      ? `${total}종` : `${matched.length}종 / 전체 ${total}종`;
    const more = $("#tbl-more-wrap");
    if (more) {
      more.hidden = matched.length <= PAGE;
      $("#tbl-more").textContent = TBL.all ? "처음 25종만 보기" : `나머지 ${matched.length - PAGE}종 더 보기`;
    }
    TABLE_ROWS = rows;
    const icons = hasIcons(rows);
    if (!rows.length) {
      $("#tbl tbody").innerHTML = `<tr class="empty-row"><td colspan="10">조건에 맞는 종목이 없습니다.</td></tr>`;
      return;
    }
    $("#tbl tbody").innerHTML = rows.map((a, i) => `<tr>
      <td><span class="tsym-cell">${icons ? iconCell(a) : ""}<button type="button" class="tsym sym-btn"
          data-i="${i}" aria-expanded="false"
          aria-label="${esc(a.symbol)} 발행사 개요 열기">${esc(a.symbol)}</button><span class="tname">${esc(a.name || "")}</span></span></td>
      <td class="td-ex">${krLogos(a.symbol)}</td>
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

  function renderThresholds(t, wm) {
    const items = [
      ["페그 편차", `주의 ±${t.peg_watch_bp}bp · 경보 ±${t.peg_breach_bp}bp`],
      ...(wm ? [["감시목록 페그 편차(엔·원 기준)",
        `주의 ±${wm.peg_watch_bp}bp · 경보 ±${wm.peg_breach_bp}bp (환율 시차 허용 ${wm.fx_lag_tolerance_bp}bp 포함) · 유통액 $${usdR(wm.min_reliable_mcap_usd)} 미만은 등급 미부여`]] : []),
      ["30일 순증감률", `주의 ${t.redemption_watch}% · 경보 ${t.redemption_breach}%`],
      ["발행사 집중도", `HHI ${t.hhi_concentrated.toLocaleString()} 초과 시 고집중`],
      ["알고리즘형 비중", `${t.algo_share_watch}% 초과 시 주의`],
      ["관측 하한", `발행잔액 $${usdR(t.min_mcap_usd)} 이상`],
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
    {
      const topSeize = (f.issuers || []).slice().sort((x, y) => (y.seize || 0) - (x.seize || 0))[0];
      setLead("freeze",
        `최근 ${days}일(${esc(f.meta.chain || "이더리움")}) 동결 ${bold(t.freeze.toLocaleString() + "건")}, 잔액 소각 ${bold(t.seize.toLocaleString() + "건")}이 있었습니다.`
        + (topSeize && topSeize.seize && t.seize
          ? ` 소각의 ${Math.round(topSeize.seize / t.seize * 100)}%는 ${esc(topSeize.issuer)}가 했습니다.` : ""));
    }

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
    $("#fz-tbl tbody").innerHTML = !(f.events || []).length
      ? `<tr class="empty-row"><td colspan="5">최근 ${days}일 동안 관측된 조치가 없습니다.</td></tr>`
      : (f.events || []).slice(0, 60).map((e) => `<tr>
      <td>${new Date(e.t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" })}</td>
      <td>${e.issuer} <span class="tname">${e.symbol}</span></td>
      <td class="kind-${e.kind}">${KIND_KO[e.kind]}</td>
      <td class="fz-addr">${short(e.addr)}</td>
      <td class="num">${e.units ? usd(e.units) : "—"}</td>
    </tr>`).join("");
  }

  // ── 김치프리미엄 ────────────────────────────────────────
  let PREM_HIST = null; // 실시간 갱신(liveTick)은 이력 없이 부르므로 마지막 이력을 기억해 둔다
  function renderPremium(p, hist) {
    if (!p || !p.assets || !p.assets.length) return;
    const redraw = !!hist; // 실시간 갱신 때는 차트를 다시 그리지 않는다(커서 위치 유지)
    if (hist) PREM_HIST = hist; else hist = PREM_HIST;
    $("#premium-sec").hidden = false;
    $("#premium-empty").hidden = true;

    const thr = p.meta.thresholds || {};
    const sw = thr.stable_watch_pct != null ? thr.stable_watch_pct : 0.5;
    const sb = thr.stable_breach_pct != null ? thr.stable_breach_pct : 1.5;
    const cw = thr.watch_pct != null ? thr.watch_pct : 3.0;
    const cb = thr.breach_pct != null ? thr.breach_pct : 7.0;

    const gradeOf = (prem, stable) => {
      if (prem == null) return "unknown";
      const w = stable ? sw : cw, b = stable ? sb : cb;
      if (prem <= (thr.inverted_pct != null ? thr.inverted_pct : -1)) return "watch";
      if (prem >= b) return "breach";
      if (prem >= w) return "watch";
      return "sound";
    };

    // 스테이블 평균 (신규 필드, 없으면 basket으로 폴백)
    const stableAvg = p.stable_avg_pct != null ? p.stable_avg_pct : null;
    const stableGrade = p.stable_grade || gradeOf(stableAvg, true);
    const pmStable = $("#pm-stable");
    if (pmStable) {
      pmStable.textContent = stableAvg != null ? signed(stableAvg, 2, "%") : "—";
      pmStable.className = "fig-v t-" + (stableAvg != null ? stableGrade : "unknown");
    }
    if (stableAvg != null) {
      const inv = thr.inverted_pct != null ? thr.inverted_pct : -1;
      const line = stableAvg <= inv ? `역프리미엄 주의선(${inv}%)`
        : stableGrade === "breach" ? `경보선(${sb}%)` : stableGrade === "watch" ? `주의선(${sw}%)` : null;
      setLead("premium",
        `국내 거래소의 USDT·USDC가 해외 기준($1)보다 평균 ${bold(signed(stableAvg, 2, "%"))} `
        + (stableAvg >= 0 ? "비싸게" : "싸게") + " 거래됩니다"
        + (line ? (stableAvg <= inv ? ` — ${line} 아래입니다.` : ` — ${line}을 넘었습니다.`) : "."));
      putSignals("premium", line ? [{
        sev: stableGrade, tab: "premium", label: "김치프리미엄",
        html: `스테이블코인 김치프리미엄 ${bold(signed(stableAvg, 2, "%"))} <span class="sig-dim">· ${esc(line)} ${stableAvg <= inv ? "하회" : "초과"}</span>`,
      }] : []);
    }

    const cryptoAvg = p.crypto_avg_pct != null ? p.crypto_avg_pct : p.basket_avg_pct;
    const cryptoGrade = p.crypto_grade || p.basket_grade || gradeOf(cryptoAvg, false);
    $("#pm-avg").textContent = cryptoAvg != null ? signed(cryptoAvg, 2, "%") : "—";
    $("#pm-avg").className = "fig-v t-" + (cryptoAvg != null ? cryptoGrade : "unknown");
    $("#pm-fx").textContent = `USD/KRW ${p.meta.fx_usdkrw.toLocaleString()}`;

    const byAsset = {};
    p.assets.forEach((a) => (byAsset[a.asset] = a));

    ["usdt", "usdc"].forEach((k) => {
      const a = byAsset[k.toUpperCase()];
      const el = $("#pm-" + k);
      if (!el) return;
      if (!a || a.premium_pct == null) { el.textContent = "—"; return; }
      el.textContent = signed(a.premium_pct, 2, "%");
      el.className = "fig-v t-" + (a.grade || gradeOf(a.premium_pct, true));
    });

    ["btc", "eth", "xrp"].forEach((k) => {
      const a = byAsset[k.toUpperCase()];
      const el = $("#pm-" + k);
      if (!el) return;
      if (!a || a.premium_pct == null) { el.textContent = "—"; return; }
      el.textContent = signed(a.premium_pct, 2, "%");
      el.className = "fig-v t-" + (a.grade || gradeOf(a.premium_pct, false));
    });

    // 두 차트 모두 커서를 올리면 그날의 USDT·BTC 프리미엄과 둘 사이 스프레드를 함께 보여 준다.
    // 스프레드(BTC − USDT, %p)는 원화로 산 USDT 를 기준으로 잰 BTC 프리미엄에 가깝다
    // (정확히는 (1+BTC)/(1+USDT)−1 이지만 몇 % 범위에서는 차이가 0.1%p 미만).
    const toT = (d) => Math.floor(new Date(d + "T00:00:00Z").getTime() / 1000);
    const btcPts = (hist && (hist.points_btc || hist.points)) || [];
    const usdtPts = (hist && hist.points_usdt) || [];
    const byDay = {};
    btcPts.forEach((d) => { (byDay[d.date] = byDay[d.date] || {}).btc = d.premium_pct; });
    usdtPts.forEach((d) => { (byDay[d.date] = byDay[d.date] || {}).usdt = d.premium_pct; });
    const pct2 = (v) => (v == null ? "—" : signed(v, 2, "%"));
    const tipRows = (first) => (p) => {
      const day = byDay[isoDate(p.t)] || {};
      const spread = day.btc != null && day.usdt != null ? day.btc - day.usdt : null;
      const rows = [["USDT 프리미엄", pct2(day.usdt), first === "usdt" ? "is-main" : ""],
                    ["BTC 프리미엄", pct2(day.btc), first === "btc" ? "is-main" : ""]];
      if (first === "btc") rows.reverse();
      const sp = ["스프레드(BTC−USDT)", spread == null ? "—" : signed(spread, 2, "%p"), first === "spread" ? "is-main" : "is-spread"];
      return first === "spread" ? [sp, ...rows] : [...rows, sp];
    };
    // 기간이 같은 두 시계열의 평균 스프레드 — 핵심 문장 보강용
    const both = Object.values(byDay).filter((d) => d.btc != null && d.usdt != null);
    const lastDay = btcPts.length ? byDay[btcPts[btcPts.length - 1].date] : null;
    // 지금 값은 위 카드와 같은 실시간 수치로 계산한다(없으면 이력의 마지막 날).
    const liveB = byAsset.BTC && byAsset.BTC.premium_pct, liveU = byAsset.USDT && byAsset.USDT.premium_pct;
    const spreadNow = liveB != null && liveU != null ? liveB - liveU
      : lastDay && lastDay.btc != null && lastDay.usdt != null ? lastDay.btc - lastDay.usdt : null;
    const spreadAvg = both.length ? both.reduce((a, d) => a + (d.btc - d.usdt), 0) / both.length : null;
    const spEl = $("#pm-spread");
    if (spEl) {
      spEl.textContent = spreadNow == null ? "—" : signed(spreadNow, 2, "%p");
      $("#pm-spread-s").textContent = spreadAvg == null ? "BTC − USDT" : `BTC − USDT · ${both.length}일 평균 ${signed(spreadAvg, 2, "%p")}`;
    }

    PREM_VIEW.byDay = byDay; PREM_VIEW.btcPts = btcPts; PREM_VIEW.usdtPts = usdtPts; PREM_VIEW.tipRows = tipRows;
    if (redraw) { bindPremTools(); drawPremCharts(); }
  }

  // 김치프리미엄 카드 — 발행 현황 '발행잔액·순증감률' 카드와 같은 구성:
  // 지표 토글(USDT·BTC·스프레드) · 기간(1M·3M·6M, 이력이 180일) · CSV · PNG · 커서 툴팁.
  const PREM_VIEW = { days: 180, metric: "usdt", byDay: {}, btcPts: [], usdtPts: [], tipRows: null, bound: false };
  const PREM_METRIC = {
    usdt: { label: "USDT 프리미엄", host: "#chart-premium-usdt", fig: "#pm-fig-usdt", color: "var(--accent)", unit: "%", d: 2 },
    btc: { label: "BTC 프리미엄", host: "#chart-premium", fig: "#pm-fig-btc", color: "var(--petrol)", unit: "%", d: 2 },
    spread: { label: "프리미엄 스프레드 (BTC − USDT)", host: "#chart-premium-spread", fig: "#pm-fig-spread", color: "var(--watch)", unit: "%p", d: 2 },
  };
  function premSeries(metric) {
    const v = PREM_VIEW;
    if (metric === "usdt") return v.usdtPts.map((d) => ({ date: d.date, v: d.premium_pct }));
    if (metric === "btc") return v.btcPts.map((d) => ({ date: d.date, v: d.premium_pct }));
    return Object.keys(v.byDay).sort()
      .filter((d) => v.byDay[d].btc != null && v.byDay[d].usdt != null)
      .map((d) => ({ date: d, v: Math.round((v.byDay[d].btc - v.byDay[d].usdt) * 1000) / 1000 }));
  }
  function drawPremCharts() {
    const v = PREM_VIEW;
    const toT = (d) => Math.floor(new Date(d + "T00:00:00Z").getTime() / 1000);
    const cut = (arr) => {
      if (!v.days || !arr.length) return arr;
      const last = toT(arr[arr.length - 1].date);
      return arr.filter((d) => toT(d.date) > last - v.days * 86400);
    };
    document.querySelectorAll("#pm-range .range-btn").forEach((btn) =>
      btn.setAttribute("aria-pressed", String((Number(btn.dataset.days) || 0) === v.days)));
    document.querySelectorAll("#pm-seg .seg-btn").forEach((btn) =>
      btn.setAttribute("aria-pressed", String(btn.dataset.metric === v.metric)));
    Object.entries(PREM_METRIC).forEach(([k, m]) => { const f = $(m.fig); if (f) f.hidden = k !== v.metric; });

    // 상단 값: 고른 지표의 최근 관측값(일별 종가 기준)
    const m = PREM_METRIC[v.metric];
    const full = premSeries(v.metric);
    const last = full[full.length - 1];
    $("#pm-tk-label").textContent = m.label;
    $("#pm-tk-value").textContent = last ? signed(last.v, m.d, m.unit) : "—";
    const shown = cut(full);
    const avg = shown.length ? shown.reduce((a, p) => a + p.v, 0) / shown.length : null;
    $("#pm-tk-sub").textContent = last
      ? `최근 관측 ${last.date} · ${shown.length}일 평균 ${signed(avg, m.d, m.unit)} · 일별 종가 기준` : "시계열 없음";

    const height = chartHeight();
    const box = { height, hMin: 200, hMax: Math.round(height * 1.5) };
    Object.entries(PREM_METRIC).forEach(([k, mm]) => {
      const host = $(mm.host);
      const pts = cut(premSeries(k)).map((p) => ({ t: toT(p.date), v: p.v }));
      if (!host || pts.length < 2) return;
      lineChart(host, pts, {
        ...box, color: mm.color, label: mm.label + " 추이", zero: true, interactive: true, fillGradient: true,
        fmt: (x) => x.toFixed(mm.d) + (mm.unit === "%p" ? "%p" : "%"),
        tipRows: v.tipRows(k),
      });
    });
  }
  function downloadPremCsv() {
    const days = Object.keys(PREM_VIEW.byDay).sort();
    if (!days.length) return;
    const n = (x) => (x == null ? "" : x);
    const lines = ["날짜,USDT 프리미엄(%),BTC 프리미엄(%),스프레드 BTC-USDT(%p)"];
    days.forEach((d) => {
      const r = PREM_VIEW.byDay[d];
      const sp = r.btc != null && r.usdt != null ? Math.round((r.btc - r.usdt) * 1000) / 1000 : null;
      lines.push(`${d},${n(r.usdt)},${n(r.btc)},${n(sp)}`);
    });
    // BOM 을 붙여야 엑셀에서 한글 머리글이 깨지지 않는다.
    saveBlob(new Blob(["\ufeff" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }),
      `stablecoin-monitor_kimchi-premium_${isoDate(Math.floor(Date.now() / 1000))}.csv`);
  }
  function bindPremTools() {
    if (PREM_VIEW.bound) return;
    PREM_VIEW.bound = true;
    document.querySelectorAll("#pm-range .range-btn").forEach((btn) => {
      btn.addEventListener("click", () => { PREM_VIEW.days = Number(btn.dataset.days) || 0; drawPremCharts(); });
    });
    document.querySelectorAll("#pm-seg .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.metric === PREM_VIEW.metric) return;
        PREM_VIEW.metric = btn.dataset.metric; drawPremCharts();
      });
    });
    const dl = $("#pm-csv-dl");
    if (dl) dl.addEventListener("click", downloadPremCsv);
    const png = $("#pm-png-dl");
    if (png) png.addEventListener("click", () => exportChartPng($(PREM_METRIC[PREM_VIEW.metric].host),
      `stablecoin-monitor_kimchi-premium_${PREM_VIEW.metric}_${isoDate(Math.floor(Date.now() / 1000))}.png`));
  }

  // ── 원화마켓 스테이블코인 거래대금 ─────────────────────────
  // 조 단위는 "1.6조", 억 단위는 "1,634억" — 국내 독자가 읽는 단위로.
  const krwK = (v) => v == null ? "—" : v >= 1e12 ? (v / 1e12).toFixed(2) + "조" : Math.round(v / 1e8).toLocaleString("ko-KR") + "억";
  function renderKrwVolume(k, fxKrw) {
    if (!k || !k.daily || !k.daily.length) return;
    const sec = $("#krv-sec");
    if (!sec) return;
    sec.hidden = false;
    const s = k.summary || {};
    const exName = {};
    (k.meta.exchanges || []).forEach((e) => { exName[e.id] = e.name; });
    $("#kv-last").textContent = "₩" + krwK(s.last_full_krw);
    $("#kv-last-s").textContent = s.last_full_date ? `${s.last_full_date} 하루` : "";
    $("#kv-avg").textContent = "₩" + krwK(s.avg30_krw);
    $("#kv-avg-s").textContent = s.chg30_pct != null ? `직전 30일 대비 ${signed(s.chg30_pct, 1, "%")}` : "직전 30일 비교 불가";
    const sh = Object.entries(s.share30_pct || {}).sort((a, b) => b[1] - a[1]);
    $("#kv-share").textContent = sh.map(([id, v]) => `${exName[id] || id} ${v.toFixed(0)}%`).join(" · ") || "—";
    const us = (s.asset_share30_pct || {}).USDT;
    $("#kv-usdt").textContent = us != null ? us.toFixed(1) + "%" : "—";

    const full = k.daily.filter((d) => !d.partial);
    const toT = (d) => Math.floor(new Date(d + "T00:00:00Z").getTime() / 1000);
    const byDate = {};
    full.forEach((d) => { byDate[d.date] = d; });
    lineChart($("#chart-krv"), full.map((d) => ({ t: toT(d.date), v: d.total_krw })), {
      color: "var(--accent)", label: "원화마켓 스테이블코인 일별 거래대금", interactive: true, fillGradient: true,
      fmt: (v) => krwK(v),
      tipRows: (p) => {
        const d = byDate[isoDate(p.t)];
        if (!d) return [["합계", krwK(p.v), "is-main"]];
        const rows = [["합계", "₩" + krwK(d.total_krw), "is-main"]];
        Object.keys(exName).forEach((id) => {
          const b = d.by[id];
          if (!b) return;
          rows.push([exName[id], `USDT ${krwK(b.USDT || 0)} · USDC ${krwK(b.USDC || 0)}`, ""]);
        });
        return rows;
      },
    });

    const fails = Object.entries(k.meta.status || {}).flatMap(([ex, st]) =>
      Object.entries(st).filter(([, v]) => v !== "ok").map(([a]) => `${exName[ex] || ex} ${a}`));
    $("#kv-foot").textContent = `출처: ${k.meta.source} · 갱신 ${fmtTime(k.meta.generated_at)}`
      + (fails.length ? ` · 이번 수집 실패: ${fails.join(", ")}(해당 거래소가 빠진 날은 표시하지 않음)` : "")
      + ". 디지털엑스(옛 코빗)·고팍스는 거래대금이 작아 뺐습니다. 진행 중인 오늘은 그래프에서 제외합니다.";

    if (s.avg30_krw) {
      const usdAmt = s.avg30_krw / (fxKrw || 1350); // 문장용 어림(기준환율) — 정확한 값은 위 수치
      setLead("krv", `최근 30일 국내 원화마켓에서 USDT·USDC 가 하루 평균 ${bold("₩" + krwK(s.avg30_krw))}(약 $${usdR(usdAmt)}) 거래됐습니다`
        + (s.chg30_pct != null ? ` — 직전 30일보다 ${bold(signed(s.chg30_pct, 1, "%"))}.` : "."));
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
    setLead("flow", !t.event_count
      ? `최근 ${f.meta.lookback_days}일 동안 집계 범위 안에서 관측된 이체가 ${bold("없습니다")}. 좁은 대리지표라 실제 흐름이 없다는 뜻은 아닙니다.`
      : `최근 ${f.meta.lookback_days}일 ${t.net_outflow_usd >= 0 ? "국내 → 해외 순유출" : "해외 → 국내 순유입"} ${bold("$" + usd(Math.abs(t.net_outflow_usd)))} (${t.event_count.toLocaleString()}건).`);

    const none = !t.event_count;
    const emptyMsg = `최근 ${f.meta.lookback_days}일 동안 집계 범위(지정 지갑 사이 직접 이체) 안에서 관측된 이체가 없습니다.`;
    const pts = (f.daily || []).map((d) => ({ t: Math.floor(new Date(d.date).getTime() / 1000), v: d.net_outflow_usd }));
    lineChart($("#chart-flow2"), none ? [] : pts, {
      color: "var(--breach)", label: "일별 순유출입", zero: true, empty: emptyMsg,
      fmt: (v) => (v >= 0 ? "+$" : "−$") + usdC(Math.abs(v)),
    });

    const flowMax = Math.max(1, ...(f.by_asset || []).map((r) => Math.max(r.inflow, r.outflow)),
                             ...(f.by_exchange || []).map((r) => Math.max(r.inflow, r.outflow)));
    const flowBars = (el, items) => {
      if (!items.length || items.every((r) => !r.inflow && !r.outflow)) {
        el.innerHTML = '<p class="bars-empty">관측된 이체가 없습니다.</p>';
        return;
      }
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
    $("#fl-tbl tbody").innerHTML = !(f.events || []).length
      ? `<tr class="empty-row"><td colspan="6">${esc(emptyMsg)}</td></tr>`
      : (f.events || []).slice(0, 60).map((e) => `<tr>
      <td>${new Date(e.t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" })}</td>
      <td class="tsym">${e.asset}</td>
      <td class="kind-${e.direction === "outflow" ? "seize" : "freeze"}">${dirKo[e.direction]}</td>
      <td>${e.kr_wallet}</td>
      <td>${e.global_wallet}</td>
      <td class="num">${usd(e.amount)}</td>
    </tr>`).join("");

    // 0건일 때는 빈 차트·표 대신 원인 진단을 앞세운다(2026-10-07 점검 결과).
    const diag = none ? [
      "<b>왜 0건인가 — 2026-10-07 점검</b>: 지정한 업비트 지갑 4개 중 3개(업비트 1·3·콜드)는 USDT·USDC 이체 기록이 아예 없고, 업비트 2 는 약 7년간 495건뿐이며, 빗썸 핫월렛은 금액 0인 이체만 있습니다. 공개 라벨이 붙은 지갑이 실제 스테이블코인 입출금 지갑이 아니라는 뜻입니다.",
      "또 국내 거래소에서 나간 돈은 해외 거래소 핫월렛으로 바로 가지 않고, 이용자 개인별 입금주소를 거쳐 모입니다(점검 표본에서 업비트 2 의 출금 상대 다수가 Binance 핫월렛으로 다시 보냄). 직접 이체만 세는 지금 방식으로는 구조적으로 잡히지 않습니다.",
      "그래서 이 탭 위쪽에 원화마켓 스테이블코인 거래대금을 대신 둡니다. 실제 입출금 지갑 목록을 확보하면 2단계(입금주소 경유) 추적으로 다시 살릴 수 있습니다.",
    ] : [];
    ["#chart-flow2"].forEach((id) => { const fig = $(id) && $(id).closest("figure"); if (fig) fig.hidden = none; });
    document.querySelectorAll("#flow-sec .fz-cols3, #flow-sec .fz-recent").forEach((n) => { n.hidden = none; });
    $("#fl-coverage").innerHTML = diag.map((h) => `<li class="fl-diag">${h}</li>`).join("") + [
      "이더리움 메인넷의 USDT·USDC 이체만 봅니다. 트론·XRP 통로는 포함되지 않습니다.",
      "업비트·빗썸의 태그된 지갑만 봅니다. 코인원은 이용자별 입금주소가 개별 태그되어 있어 단일 지갑으로 묶을 수 없습니다.",
      "해외 비교군은 Binance·OKX·Bybit 각각 잔액이 가장 큰 핫월렛 하나씩입니다. 같은 거래소가 굴리는 다른 지갑들은 빠져 있습니다.",
      "그래서 이 숫자는 실제 순유출의 하한선이지 전체가 아닙니다.",
    ].map((s) => `<li>${s}</li>`).join("");
  }

  // ── 어테스테이션 시차 ────────────────────────────────────
  const GAP_KO = { pending: "확인 대기", unverified: "미검증", none: "보고서 없음", other: "다른 방식" };
  function renderAttestation(a) {
    if (!a || !a.entries || !a.entries.length) return;
    // 옛 형식 데이터(공표 주기 필드 없음)는 월간 기준(주의 45일·경보 75일)으로 본다.
    a.entries.forEach((e) => {
      if (e.watch_days == null) e.watch_days = (a.meta.thresholds && a.meta.thresholds.watch_days) || 45;
      if (e.breach_days == null) e.breach_days = (a.meta.thresholds && a.meta.thresholds.breach_days) || 75;
    });
    $("#attest-sec").hidden = false;
    $("#attest-empty").hidden = true;
    const num = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
    // 표에는 줄인 값(73.32B), 커서를 올리면 보고서 그대로의 정확한 개수
    const cmp = (v) => v == null ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : num(v);
    // 정렬: 등급 나쁜 순 → 경과일 긴 순
    const rows = a.entries.slice().sort((x, y) =>
      (GRADE_RANK[y.grade] || 0) - (GRADE_RANK[x.grade] || 0) || (y.days_since || 0) - (x.days_since || 0));
    $("#attest-tbl tbody").innerHTML = rows.map((e) => {
      const ratio = e.reserves_total != null && e.reported_circulating ? e.reserves_total / e.reported_circulating * 100 : null;
      const why = e.days_since >= e.breach_days ? "경과일 경보" : e.days_since >= e.watch_days ? "경과일 주의"
        : e.grade !== "sound" ? "드리프트" : "";
      const cur = e.reserves_currency === "EUR" ? "€" : "$";
      return `<tr>
      <td><a class="tsym att-link" href="${esc(e.source_url)}" rel="noopener" title="원문 보고서 열기">${esc(e.symbol)}</a><span class="tname">${esc(e.issuer)}</span>${e.note ? `<span class="att-sub att-note">${esc(e.note)}</span>` : ""}</td>
      <td class="att-firm"${e.attestor_note ? ` title="${esc(e.attestor_note)}"` : ""}><span>${esc(e.attestor || "—")}</span>${e.report_type ? `<span class="att-sub">${esc(e.report_type)}</span>` : ""}</td>
      <td>${esc(e.as_of_date)}${e.cadence ? `<span class="att-sub">${esc(e.cadence)} 공표</span>` : ""}</td>
      <td class="num t-${e.days_since >= e.breach_days ? "breach" : e.days_since >= e.watch_days ? "watch" : "sound"}"
        title="주의 ${e.watch_days}일 · 경보 ${e.breach_days}일(공표 주기 ${e.cadence_days || 30}일 기준)">${e.days_since}일</td>
      <td class="num" title="${esc(num(e.reported_circulating) + (e.verify ? " — " + e.verify : ""))}">${cmp(e.reported_circulating)}${e.verified ? "" : (e.reported_circulating == null ? "" : "*")}</td>
      <td class="num" title="${esc(num(e.current_circulating))} (DefiLlama)">${cmp(e.current_circulating)}</td>
      <td class="num t-${e.drift_pct == null ? "unknown" : Math.abs(e.drift_pct) >= 8 ? "breach" : Math.abs(e.drift_pct) >= 3 ? "watch" : "sound"}">${e.drift_pct != null ? signed(e.drift_pct, 1, "%") : "—"}</td>
      <td class="num"${e.reserves_total != null ? ` title="준비금 ${cur}${num(e.reserves_total)}"` : ""}>${ratio != null ? ratio.toFixed(2) + "%" : "—"}</td>
      <td><span class="pill is-${e.grade} t-${e.grade}"${why ? ` title="${why}"` : ""}>${GRADE_KO[e.grade] || e.grade}</span></td>
    </tr>`;
    }).join("");
    $("#attest-note").textContent = "종목명을 누르면 원문 보고서가 열립니다. 발행량은 토큰 개수(EURC 는 유로)이고, 보고 시점 발행량은 보고서 표의 기준일 값을 그대로 옮겼습니다. "
      + "보고서는 특정 시점의 준비금 확인이지 회계감사가 아닙니다. 드리프트는 보고 이후 성장도 포함하므로 그 자체로 부실을 뜻하지 않습니다. " + (a.meta.maintenance_note || "");

    const gaps = a.not_covered || [];
    const gw = $("#attest-gaps-wrap");
    if (gw) {
      gw.hidden = !gaps.length;
      $("#attest-gaps").innerHTML = gaps.map((g) => `<li>
        <span class="att-gap-k gap--${esc(g.kind)}">${esc(GAP_KO[g.kind] || g.kind)}</span>
        <span><b class="tsym">${esc(g.symbol)}</b> <span class="tname">${esc(g.issuer || "")}</span> — ${esc(g.reason || "")}
        ${g.source_url ? ` <a href="${esc(g.source_url)}" rel="noopener">공시 보기</a>` : ""}</span></li>`).join("");
    }

    const fresh = a.entries.filter((e) => e.days_since < e.watch_days).length;
    const stale = a.entries.filter((e) => e.days_since >= e.watch_days);
    const big = a.entries.filter((e) => e.drift_pct != null && Math.abs(e.drift_pct) >= 8);
    setLead("attest",
      (fresh === a.entries.length
        ? `준비금 보고서를 확인한 ${bold(a.entries.length + "종")} 모두 공표 주기 안의 최신 보고서입니다`
        : `준비금 보고서를 확인한 ${bold(a.entries.length + "종")} 가운데 ${bold(fresh + "종")}이 공표 주기 안의 최신 보고서입니다`)
      + (stale.length ? `, ${stale.map((e) => bold(e.symbol)).join("·")}는 다음 보고서가 늦어지고 있습니다` : "")
      + (big.length ? `. 보고 이후 발행량이 8% 이상 달라진 종목은 ${big.map((e) => bold(e.symbol) + `(${signed(e.drift_pct, 1, "%")})`).join("·")}입니다.` : "."));
    putSignals("attest", a.entries.filter((e) => e.days_since >= e.watch_days).map((e) => ({
      sev: e.days_since >= e.breach_days ? "breach" : "watch", tab: "attest", label: "어테스테이션",
      html: `${bold(e.symbol)} 준비금 보고서 ${bold(e.days_since + "일")} 경과 <span class="sig-dim">· ${esc(e.cadence || "월간")} 공표 · 기준일 ${esc(e.as_of_date)}</span>`,
    })));
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
    setLead("flowxrp", t.event_count
      ? `최근 ${f.meta.lookback_days}일 ${t.net_outflow_xrp >= 0 ? "국내 → 해외로" : "해외 → 국내로"} XRP가 ${bold(usd(Math.abs(t.net_outflow_xrp)) + "개")} 더 ${t.net_outflow_xrp >= 0 ? "나갔습니다(순유출)" : "들어왔습니다(순유입)"} — ${t.event_count.toLocaleString()}건.`
      : "");

    const xpts = (f.daily || []).map((d) => ({ t: Math.floor(new Date(d.date).getTime() / 1000), v: d.net_outflow_xrp }));
    lineChart($("#chart-flowxrp"), xpts, {
      color: "var(--breach)", label: "일별 순유출입", zero: true,
      empty: `최근 ${f.meta.lookback_days}일 동안 관측된 결제가 하루치 이하라 추이를 그릴 수 없습니다.`,
      fmt: (v) => (v >= 0 ? "+" : "−") + usdC(Math.abs(v)),
    });

    const dirKo = { outflow: "유출", inflow: "유입" };
    $("#fx-tbl tbody").innerHTML = !(f.events || []).length
      ? `<tr class="empty-row"><td colspan="5">최근 ${f.meta.lookback_days}일 동안 관측된 결제가 없습니다.</td></tr>`
      : (f.events || []).slice(0, 60).map((e) => `<tr>
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

    const bar = document.querySelector(".tabbar");
    const scroller = document.querySelector(".tabbar-scroll");

    // 가로로 더 있는 탭이 있으면 그쪽 가장자리를 흐리게 해 스크롤 가능함을 알린다.
    const paintFade = () => {
      if (!scroller) return;
      const max = scroller.scrollWidth - scroller.clientWidth;
      scroller.classList.toggle("fade-l", scroller.scrollLeft > 2);
      scroller.classList.toggle("fade-r", scroller.scrollLeft < max - 2);
    };
    if (scroller) {
      scroller.addEventListener("scroll", paintFade, { passive: true });
      addEventListener("resize", paintFade);
    }

    // user: 사람이 누른 전환인지(주소창 갱신·스크롤 보정은 그때만 한다)
    const activate = (i, focus, user) => {
      tabs.forEach((t, j) => {
        const on = j === i;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        if (panels[j]) panels[j].hidden = !on;
      });
      if (focus) tabs[i].focus({ preventScroll: true });
      // 고른 탭이 탭바 밖으로 잘려 있으면 보이게 민다(좁은 화면).
      if (scroller) {
        const t = tabs[i], sl = scroller.scrollLeft, w = scroller.clientWidth;
        if (t.offsetLeft < sl + 16 || t.offsetLeft + t.offsetWidth > sl + w - 16) {
          scroller.scrollTo({ left: t.offsetLeft - (w - t.offsetWidth) / 2, behavior: user ? "smooth" : "auto" });
        }
      }
      if (!user) return;
      // 주소에 탭을 남겨 링크로 공유·새로고침해도 같은 탭이 열리게 한다.
      try { history.replaceState(null, "", "#" + tabs[i].id.replace(/^tab-/, "")); } catch (e) {}
      // 긴 탭을 한참 내려 읽다 다른 탭을 누르면 새 탭의 중간(또는 바닥)부터
      // 보이게 된다. 탭바가 위에 붙어 있는 상태라면 새 탭의 첫머리로 올린다.
      // (sticky 요소의 offsetTop 은 붙어 있는 위치를 돌려줄 수 있어서, 탭바 바로
      // 앞 요소의 끝을 탭바의 원래 자리로 쓴다.)
      if (bar && bar.getBoundingClientRect().top <= 0.5) {
        const prev = bar.previousElementSibling;
        if (prev) {
          const home = prev.getBoundingClientRect().bottom + window.scrollY;
          if (window.scrollY > home) window.scrollTo({ top: home, behavior: "auto" });
        }
      }
    };

    // 주소의 #premium 같은 꼬리로 첫 탭을 고른다.
    const fromHash = () => {
      const h = decodeURIComponent((location.hash || "").slice(1));
      const i = tabs.findIndex((t) => t.id === "tab-" + h);
      return i;
    };
    const first = fromHash();
    if (first > 0) activate(first, false, false);
    addEventListener("hashchange", () => {
      const i = fromHash();
      if (i >= 0) activate(i, false, false);
    });
    requestAnimationFrame(paintFade);

    tabs.forEach((t, i) => {
      t.addEventListener("click", () => activate(i, false, true));
      t.addEventListener("keydown", (e) => {
        const n = tabs.length;
        if (e.key === "ArrowRight") { e.preventDefault(); activate((i + 1) % n, true, true); }
        else if (e.key === "ArrowLeft") { e.preventDefault(); activate((i - 1 + n) % n, true, true); }
        else if (e.key === "Home") { e.preventDefault(); activate(0, true, true); }
        else if (e.key === "End") { e.preventDefault(); activate(n - 1, true, true); }
      });
    });
  }

  // ── 국내 거래소 거래지원 ───────────────────────────────
  // data/listings.json(etl/fetch_listings.py)이 있을 때만 '국내' 열·칩·절이 채워진다.
  // 거래소 티커와 DefiLlama 심볼을 대문자로 맞춰 대조한다.
  let LISTINGS = null;
  const EX_NAME = {};
  const krOf = (sym) => (LISTINGS && LISTINGS.assets && LISTINGS.assets[String(sym || "").toUpperCase()]) || null;
  const krCount = (sym) => (LISTINGS ? (krOf(sym) ? krOf(sym).count : 0) : null);
  const mkTxt = (m) => m.join("·");
  function krText(sym) {
    const k = krOf(sym);
    if (!k) return LISTINGS ? "없음(국내 원화마켓 5곳 기준)" : "";
    return Object.entries(k.exchanges).map(([id, v]) =>
      `${EX_NAME[id] || id} ${mkTxt(v.markets)}${v.warning ? "(유의)" : ""}${v.stale ? "*" : ""}`).join(" · ");
  }
  // 거래소 아이콘 한 칸: 공식 아이콘 사본(있으면) 위에, 없거나 못 읽으면 머리글자 배지.
  // 오른쪽 위 작은 글자 = 마켓(원=KRW, B=BTC, T=USDT).
  const MK_SHORT = { KRW: "원", BTC: "B", USDT: "T" };
  const MK_KO = { KRW: "원화", BTC: "BTC", USDT: "USDT" };
  const EX = {}; // id → 거래소 메타(이름·아이콘·머리글자·색)
  function exBadge(e, v, cls = "") {
    const mk = v ? v.markets.map((m) => MK_SHORT[m] || m[0]).join("") : "";
    const tip = v ? `${e.name} · ${v.markets.map((m) => MK_KO[m] || m).join("·")} 마켓${v.warning ? " · 투자유의" : ""}${v.stale ? " · 이번 수집 실패, 직전 값" : ""}` : e.name;
    return `<span class="ex${v && v.warning ? " ex--warn" : ""}${v && v.stale ? " ex--stale" : ""}${cls}" title="${esc(tip)}" role="listitem" aria-label="${esc(tip)}">
      <span class="ex-mono" style="--exc:${esc(e.color || "#475569")}" aria-hidden="true">${esc(e.short || e.name[0])}</span>${
      e.icon ? `<img class="ex-img" src="${esc(e.icon)}" alt="" width="22" height="22" loading="lazy" decoding="async">` : ""}${
      mk ? `<i class="ex-mk" aria-hidden="true">${esc(mk)}</i>` : ""}</span>`;
  }
  function krLogos(sym) {
    if (!LISTINGS) return "";
    const k = krOf(sym);
    if (!k) return `<span class="kr-0" aria-label="국내 거래지원 없음">—</span>`;
    // 거래소 순서를 고정해(빈 자리 유지) 행끼리 같은 거래소가 세로로 맞도록 한다.
    const ids = Object.keys(EX);
    return `<span class="exl" role="list">${ids.map((id) => k.exchanges[id]
      ? exBadge(EX[id], k.exchanges[id]) : '<span class="ex ex--empty" aria-hidden="true"></span>').join("")}</span>`;
  }
  // 아이콘 사본을 못 읽으면 머리글자 배지가 드러나게 이미지만 숨긴다(error 는 버블링하지 않아 캡처로 받는다).
  document.addEventListener("error", (ev) => {
    const t = ev.target;
    if (t && t.classList && t.classList.contains("ex-img")) t.remove();
  }, true);

  function renderListings(L, snap) {
    LISTINGS = L;
    const exs = (L.meta && L.meta.exchanges) || [];
    exs.forEach((e) => { EX_NAME[e.id] = e.name; EX[e.id] = e; });
    const sec = $("#kr-sec");
    const syms = Object.keys(L.assets || {});
    // 표·감시목록·칩을 다시 그려 '국내 거래소' 열을 채운다.
    const chip = document.querySelector('.tbl-tools .chip[data-f="kr"]');
    if (chip) chip.hidden = false;
    const leg = $("#ex-legend");
    if (leg) leg.hidden = false;
    paintTable();
    renderWatchlist(snap);
    if (!sec) return;

    const mcapOf = {};
    (snap.assets || []).concat((snap.watchlist && snap.watchlist.rows) || [])
      .forEach((a) => { mcapOf[String(a.symbol).toUpperCase()] = a; });

    // 거래소별 요약: 아이콘 · 이름 · 스테이블코인 종목 수(원화마켓)
    $("#ex-sum").innerHTML = exs.map((e) => {
      const mine = syms.filter((sym) => L.assets[sym].exchanges[e.id]);
      const krw = mine.filter((sym) => L.assets[sym].exchanges[e.id].markets.includes("KRW")).length;
      return `<li class="ex-sum-i${e.status !== "ok" ? " is-fail" : ""}">
        ${exBadge(e, null, " ex--lg")}
        <span class="ex-sum-t"><b>${esc(e.name)}</b>
          <span class="ex-sum-n">${mine.length}종${mine.length ? ` · 원화마켓 ${krw}종` : ""}${e.status !== "ok" ? " · 이번 수집 실패(직전 값)" : ""}</span></span>
      </li>`;
    }).join("");

    const tracked = (snap.assets || []).length;
    const listedMain = (snap.assets || []).filter((a) => krOf(a.symbol));
    const shareSum = listedMain.reduce((acc, a) => acc + (a.share || 0), 0);
    const all5 = syms.filter((x) => L.assets[x].count === exs.length && exs.length > 1);
    setLead("kr", syms.length
      ? `상위 ${bold(tracked + "종")} 가운데 ${bold(listedMain.length + "종")}이 국내 원화마켓 거래소에서 거래되며, 발행잔액으로는 ${bold(shareSum.toFixed(1) + "%")}입니다.`
        + (all5.length ? ` ${exs.length}곳 모두 지원하는 종목은 ${all5.map((x) => bold((mcapOf[x] || {}).symbol || x)).join("·")}입니다.` : "")
      : "");

    const okEx = exs.filter((e) => e.status === "ok");
    const fails = exs.filter((e) => e.status !== "ok").map((e) => e.name);
    $("#kr-basis").textContent =
      `출처: ${exs.map((e) => e.name).join("·")} 공개 마켓 목록(${okEx.length}/${exs.length}곳 수집) · 기준 ${fmtTime(L.meta.generated_at)}`
      + (fails.length ? ` · 수집 실패: ${fails.join("·")} — 직전 값 표시` : "")
      + ". 거래소 티커와 심볼이 같으면 같은 종목으로 봅니다(동명 티커는 다른 토큰일 수 있음). 시작·종료일은 이 수집이 처음 확인한 날로, 거래소 공지일과 다를 수 있습니다.";

    const ev = (L.events || []).slice(0, 12);
    const evWrap = $("#kr-events-wrap");
    if (ev.length) {
      $("#kr-events").innerHTML = ev.map((e) => `<li class="kr-ev kr-ev--${e.kind}">
        <span class="kr-ev-d">${esc(e.date)}</span>
        <span class="kr-ev-k">${e.kind === "listed" ? "시작" : "종료"}</span>
        <span>${esc(EX_NAME[e.exchange] || e.exchange)} <b class="tsym">${esc(e.symbol)}</b> ${esc(MK_KO[e.market] || e.market)} 마켓</span>
      </li>`).join("");
      evWrap.hidden = false;
    } else evWrap.hidden = true;
    sec.hidden = false;

    const since = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10);
    const recent = (L.events || []).filter((e) => e.date >= since);
    const nL = recent.filter((e) => e.kind === "listed").length, nD = recent.length - nL;
    const sig = [];
    if (nD) sig.push({ sev: "watch", tab: "issuance", target: "kr-sec", label: "국내 거래지원 종료",
      html: `최근 7일 국내 거래지원 종료 ${bold(nD + "건")} — ${recent.filter((e) => e.kind !== "listed").slice(0, 3)
        .map((e) => esc(`${EX_NAME[e.exchange] || e.exchange} ${e.symbol}`)).join(", ")}` });
    if (nL) sig.push({ sev: "info", tab: "issuance", target: "kr-sec", label: "국내 거래지원 시작",
      html: `최근 7일 국내 거래지원 시작 ${bold(nL + "건")} — ${recent.filter((e) => e.kind === "listed").slice(0, 3)
        .map((e) => esc(`${EX_NAME[e.exchange] || e.exchange} ${e.symbol}`)).join(", ")}` });
    putSignals("listings", sig);
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

    // 주요 신호의 "보기": 해당 탭을 열고 그 절로 내려간다.
    $("#signals").addEventListener("click", (e) => {
      const a = e.target.closest ? e.target.closest(".sig-go") : null;
      if (!a) return;
      e.preventDefault();
      const tab = document.getElementById("tab-" + a.dataset.tab);
      if (tab) tab.click();
      if (a.dataset.filter) {
        const chip = document.querySelector(`.tbl-tools .chip[data-f="${a.dataset.filter}"]`);
        if (chip) chip.click();
      }
      const target = a.dataset.target && document.getElementById(a.dataset.target);
      if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
    });

    renderStatus(snap);
    renderGauge(snap);
    renderTable(snap);
    renderWatchlist(snap);
    initAssetPop(); // 계기판·표를 다 그린 뒤 한 번만 건다

    renderThresholds(snap.meta.thresholds, snap.watchlist && snap.watchlist.meta);
    initTrend(hist);
    renderTotalTrend(snap, hist);

    bars($("#mech-bars"), snap.by_mechanism.map((m) => ({
      label: m.label, share: m.share, amount: m.amount, algo: m.mechanism === "algorithmic",
    })));
    // 상위 6개 통화 + 감시 통화(KRW·JPY)는 규모와 관계없이 항상 보인다.
    const pinned = (snap.watchlist && snap.watchlist.meta && snap.watchlist.meta.pinned_currencies) || [];
    const curTop = snap.by_peg_currency.slice(0, 6);
    pinned.forEach((cur) => {
      if (curTop.some((c) => c.currency === cur)) return;
      const hit = snap.by_peg_currency.find((c) => c.currency === cur);
      curTop.push(hit ? { ...hit, pinned: true } : { currency: cur, share: 0, amount: 0, pinned: true });
    });
    bars($("#cur-bars"), curTop.map((c) => ({
      label: c.currency, share: c.share, amount: c.amount,
      pinned: c.pinned || pinned.includes(c.currency),
    })));
    const uv = snap.unvalued, uvEl = $("#cur-unvalued");
    if (uvEl && uv && uv.count) {
      const tops = (uv.top || []).slice(0, 3).map((u) => `${u.symbol}(${u.peg_currency} ${localAmt(u.circulating, u.peg_currency).replace(" " + u.peg_currency, "")})`);
      uvEl.textContent = `가격·환율이 없어 USD 환산과 위 비중에서 뺀 종목 ${uv.count}종: ${tops.join(", ")}${uv.count > 3 ? " 등" : ""}.`;
      uvEl.hidden = false;
    }
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

    // 원화마켓 스테이블코인 거래대금
    try {
      const r = await fetch("data/krw_volume.json", { cache: "no-cache" });
      if (r.ok) renderKrwVolume(await r.json(), snap.watchlist && snap.watchlist.meta && snap.watchlist.meta.fx_rates && snap.watchlist.meta.fx_rates.KRW);
    } catch (e) {
      console.info("krw_volume.json 없음 — 원화 거래대금 생략");
    }

    // 국내 거래소 거래지원 현황
    try {
      const r = await fetch("data/listings.json", { cache: "no-cache" });
      if (r.ok) renderListings(await r.json(), snap);
    } catch (e) {
      console.info("listings.json 없음 — 국내 거래지원 표시 생략");
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

  // ── 테마 토글 ──────────────────────────────────────────
  function initTheme() {
    const btn = $("#theme-toggle");
    if (!btn) return;
    const apply = (t) => {
      document.documentElement.dataset.theme = t;
      try { localStorage.setItem("scw-theme", t); } catch (e) {}
      btn.setAttribute("aria-label", t === "dark" ? "라이트 모드로 전환" : "다크 모드로 전환");
      btn.textContent = t === "dark" ? "라이트" : "다크";
    };
    const cur = document.documentElement.dataset.theme || "light";
    apply(cur === "dark" ? "dark" : "light");
    btn.addEventListener("click", () => {
      apply(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  initTheme();
  initTabs();
  boot();
})();
