// ==UserScript==
// @name         Steam × PS Plus 会免游戏显示
// @icon         https://store.steampowered.com/favicon.ico
// @namespace    https://github.com/oXnMe/psplus-steam-overlay
// @version      1.1.9
// @description  在 Steam 显示 PS Plus 会免/入库状态。
// @author       oXnMe
// @license      MIT
// @match        https://store.steampowered.com/*
// @connect      raw.githubusercontent.com
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_addStyle
// @run-at       document-idle
// @updateURL    https://raw.githubusercontent.com/oXnMe/psplus-steam-overlay/main/userscript/steam-psplus.user.js
// @downloadURL  https://raw.githubusercontent.com/oXnMe/psplus-steam-overlay/main/userscript/steam-psplus.user.js
// ==/UserScript==

const CONFIG = {
  RAW_URL: "https://raw.githubusercontent.com/oXnMe/psplus-steam-overlay/main/data/psplus-games.json",
  CACHE_KEY: "psplus_data_cache_v1",
};

const PS_LOGO_PATH =
  "M0.69116 21.9548C-0.506476 22.7935 -0.284724 24.2384 2.44769 25.1419C5.18011 26.0455 8.35603 26.2323 11.1505 25.729C11.0707 25.729 11.3102 25.729 11.1505 25.729V22.8774L8.43588 23.8C7.39792 24.1355 6.35997 24.2194 5.32202 23.9677C4.5236 23.7161 4.68328 23.2129 5.64139 22.7935L11.1505 20.7806V17.6774L3.48565 20.4452C2.52754 20.7806 1.56943 21.2839 0.69116 21.9548ZM19.2146 9.37419V17.5097C22.4881 19.1871 25.0431 17.5097 25.0431 13.1484C25.0431 8.70323 23.5261 6.69032 19.1348 5.09677C16.8193 4.25806 14.4241 3.50323 12.0288 3V27.2387L17.6178 29V8.61935C17.6178 7.69677 17.6178 7.02581 18.2565 7.27742C19.1348 7.52903 19.2146 8.45161 19.2146 9.37419ZM29.5941 20.0258C27.2787 19.1871 24.8036 18.8516 22.4083 19.1032C21.0779 19.1906 19.8294 19.5869 18.5759 20.0258V23.2968L23.7656 21.2839C24.8036 20.9484 25.8415 20.8645 26.8795 21.1161C27.6779 21.3677 27.5182 21.871 26.5601 22.2903L18.5759 25.3935V28.5806L29.5941 24.3032C30.3925 23.9677 31.1111 23.5484 31.7499 22.8774C32.3088 22.0387 32.0692 20.8645 29.5941 20.0258Z";

const PS_ICON =
  '<svg viewBox="0 0 32 32" width="22" height="22" role="img" aria-label="PlayStation" fill="currentColor">' +
  '<path d="' + PS_LOGO_PATH + '"/>' +
  "</svg>";

const PS_ICON_MINI =
  '<svg viewBox="0 0 32 32" width="12" height="12" role="img" aria-label="PlayStation" fill="currentColor">' +
  '<path d="' + PS_LOGO_PATH + '"/>' +
  "</svg>";

const EXIT_ICON_PATH =
  "M66.4,68.67H40.29a7.23,7.23,0,0,1,0-14.45H66.4l-8.48-9.47a7.25,7.25,0,0,1,.51-10.16,7.07,7.07,0,0,1,10.05.51L87.76,56.62a7.27,7.27,0,0,1-.06,9.72L68.48,87.78a7.05,7.05,0,0,1-10.05.51,7.25,7.25,0,0,1-.51-10.16l8.48-9.46ZM40.11.14a7.22,7.22,0,0,1,2.83,14.17L39.55,15c-16.33,3.24-25.1,5.09-25.1,27.69V82.25c0,21,9.34,22.76,24.8,25.65l3.63.68a7.21,7.21,0,1,1-2.71,14.17l-3.57-.68C13.78,117.81,0,115.23,0,82.25V42.67C0,8.24,12.84,5.56,36.74.82L40.11.14Z";

const EXIT_ICON =
  '<svg class="psplus-exit" viewBox="0 0 89.6 122.88" role="img" aria-label="已出库" fill="currentColor">' +
  '<path d="' + EXIT_ICON_PATH + '"/>' +
  "</svg>";

const STATUS_DATE_LABEL = {
  "即将会免": "预计领取",
  "领取中": "截止领取",
  "会免过": "截止领取",
  "即将入库": "预计入库",
  "在库": "入库",
  "即将出库": "出库",
  "已出库": "出库",
};

const CSS = `
.psplus-banners {
  margin: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.psplus-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  background: linear-gradient(90deg, #003791, #004db8);
  border-radius: 3px;
  color: #fff;
  text-decoration: none;
  font-size: 13px;
  line-height: 1.4;
  transition: filter .15s ease;
}
.psplus-banner:hover {
  filter: brightness(1.15);
}
.psplus-banner svg {
  display: block;
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
}
.psplus-banner-text {
  flex: 1 1 auto;
  font-weight: 600;
}
.psplus-banner-date {
  flex: 0 0 auto;
  font-size: 12px;
  opacity: 0.9;
  white-space: nowrap;
}
.psplus-badge {
  position: absolute;
  left: 0;
  top: calc(50% + 10px);
  transform: translateY(-50%);
  z-index: 20;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  background: #003791;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  border-radius: 0 3px 3px 0;
  line-height: 1;
  pointer-events: none;
  box-shadow: 2px 0 5px rgba(0,0,0,0.3);
  white-space: nowrap;
}
.psplus-badge svg {
  display: block;
  width: 12px;
  height: 12px;
}
.psplus-badge .psplus-exit {
  width: 9px;
  height: 12px;
}
.search_result_row .psplus-badge {
  top: calc(50% + 5px);
}
[id^="searchSuggestions"] .psplus-badge {
  top: calc(50% + 15px);
}
`;

function getIdsFromUrl(href) {
  const url = href || location.href;
  const app = /\/app\/(\d+)/.exec(url);
  const sub = /\/sub\/(\d+)/.exec(url);
  const bundle = /\/bundle\/(\d+)/.exec(url);
  return {
    appId: app ? app[1] : null,
    subId: sub ? sub[1] : (bundle ? bundle[1] : null),
  };
}

function buildMaps(games) {
  const app = {};
  const sub = {};
  games.forEach(function (g) {
    const id = String(g.steamAppId || "");
    if (!id) return;
    if (id.charAt(0) === "s") {
      if (!sub[id]) sub[id] = [];
      sub[id].push(g);
    } else {
      if (!app[id]) app[id] = [];
      app[id].push(g);
    }
  });
  return { app: app, sub: sub };
}

function loadData() {
  return new Promise(function (resolve) {
    const cached = GM_getValue(CONFIG.CACHE_KEY);
    let cachedObj = null;
    if (cached) {
      try {
        cachedObj = JSON.parse(cached);
      } catch (e) {
        /* fall through to network */
      }
    }

    GM_xmlhttpRequest({
      method: "GET",
      url: CONFIG.RAW_URL,
      timeout: 10000,
      onload: function (res) {
        if (res.status === 200) {
          try {
            const data = JSON.parse(res.responseText);
            const remote = data.updatedAt || "";
            const local = cachedObj ? (cachedObj.data.updatedAt || "") : "";
            if (!cachedObj || remote !== local) {
              GM_setValue(CONFIG.CACHE_KEY, JSON.stringify({ ts: Date.now(), data: data }));
            }
            resolve(data);
          } catch (e) {
            resolve(cachedObj ? cachedObj.data : null);
          }
        } else {
          resolve(cachedObj ? cachedObj.data : null);
        }
      },
      onerror: function () {
        resolve(cachedObj ? cachedObj.data : null);
      },
    });
  });
}

function bannerText(m) {
  if (m.tier === 1) {
    return m.month + " 1档 " + (m.statusDisplay || m.statusName || "");
  }
  return m.month + "入库 " + m.tier + "档 " + (m.statusDisplay || m.statusName || "");
}

function bannerDate(m) {
  if (Number(m.tier) === 1 &&
      (m.statusName === "会免过" || m.statusDisplay === "会免过")) {
    return "";
  }
  if (!m.statusDate) return "";
  if (m.statusDate === "出库时间未知") return m.statusDate;
  const lbl = STATUS_DATE_LABEL[m.statusName];
  return lbl ? lbl + "：" + m.statusDate : m.statusDate;
}

function renderStoreBanners(appMap, subMap) {
  const ids = getIdsFromUrl();
  const key = ids.appId || ("s" + ids.subId);
  const matches = ids.appId ? (appMap[key] || []) : (subMap[key] || []);
  if (!matches.length) return;

  const sortedMatches = matches.slice().sort(function (a, b) {
    return (a.tier || 0) - (b.tier || 0);
  });

  function makeBanner(text, dateText, href) {
    const a = document.createElement("a");
    a.className = "psplus-banner";
    a.href = href || "#";
    a.target = "_blank";
    a.rel = "noopener noreferrer";

    const icon = document.createElement("span");
    icon.innerHTML = PS_ICON;
    a.appendChild(icon);

    const txt = document.createElement("span");
    txt.className = "psplus-banner-text";
    txt.textContent = "PS Plus | " + text;
    a.appendChild(txt);

    if (dateText) {
      const date = document.createElement("span");
      date.className = "psplus-banner-date";
      date.textContent = dateText;
      a.appendChild(date);
    }
    return a;
  }

  function inject() {
    const wrap = document.createElement("div");
    wrap.className = "psplus-banners";

    if (sortedMatches.length === 1) {
      const m = sortedMatches[0];
      wrap.appendChild(makeBanner(bannerText(m), bannerDate(m), m.psstore));
    } else {
      const mergedText = sortedMatches.map(bannerText).join(" / ");
      const parts = [];
      let tier2PlusAdded = false;
      for (let i = 0; i < sortedMatches.length; i++) {
        const m = sortedMatches[i];
        const d = bannerDate(m);
        if (!d) continue;
        if (Number(m.tier) === 1) {
          parts.push(d);
        } else if (!tier2PlusAdded) {
          parts.push(d);
          tier2PlusAdded = true;
        }
      }
      const mergedDate = parts.join(" / ");
      wrap.appendChild(makeBanner(mergedText, mergedDate, sortedMatches[0].psstore));
    }

    const actions =
      document.querySelector("#queue_actions_ctn") ||
      document.querySelector(".queue_actions_ctn") ||
      document.querySelector(".game_area_purchase");
    if (actions && actions.parentNode) {
      actions.parentNode.insertBefore(wrap, actions);
      return;
    }

    const media =
      document.querySelector(".game_media_ctn") ||
      document.querySelector(".game_header_image_ctn") ||
      document.querySelector(".game_header_image") ||
      document.querySelector(".page_content");
    if (!media) return;

    if (media.classList.contains("page_content")) {
      media.insertBefore(wrap, media.firstChild);
    } else if (media.parentNode) {
      media.parentNode.insertBefore(wrap, media.nextSibling);
    } else {
      media.appendChild(wrap);
    }
  }

  function wait(tries) {
    tries = tries || 0;
    if (
      document.querySelector("#queue_actions_ctn") ||
      document.querySelector(".queue_actions_ctn") ||
      document.querySelector(".game_area_purchase") ||
      document.querySelector(".game_media_ctn") ||
      document.querySelector(".game_header_image_ctn") ||
      document.querySelector(".game_header_image") ||
      document.querySelector(".page_content") ||
      tries > 30
    ) {
      inject();
      return;
    }
    setTimeout(function () {
      wait(tries + 1);
    }, 250);
  }
  wait();
}

function appIdFromNode(node) {
  const ds = node.getAttribute && node.getAttribute("data-ds-appid");
  if (ds) return ds.split(",")[0];
  const href = node.href || (node.getAttribute && node.getAttribute("href")) || "";
  const m = /\/app\/(\d+)/.exec(href);
  return m ? m[1] : null;
}

function renderCardBadge(node, appId, matches) {
  const prev = node.getAttribute("data-psplus-appid");
  const hasBadges = !!node.querySelector(".psplus-badge");
  const wantBadges = matches.length > 0;
  if (prev === appId && hasBadges === wantBadges) return;

  node.querySelectorAll(".psplus-badge").forEach(function (b) {
    b.remove();
  });
  node.setAttribute("data-psplus-appid", appId);
  if (!wantBadges) return;

  if (window.getComputedStyle(node).position === "static") {
    node.style.position = "relative";
  }

  const tier2plus = matches.filter(function (m) {
    return Number(m.tier) >= 2;
  });
  tier2plus.sort(function (a, b) {
    return (b.monthSort || 0) - (a.monthSort || 0);
  });
  const latestExited =
    tier2plus.length > 0 &&
    (tier2plus[0].statusName || tier2plus[0].statusDisplay) === "已出库";

  matches
    .slice()
    .sort(function (a, b) {
      return (a.tier || 0) - (b.tier || 0);
    })
    .forEach(function (m) {
      const badge = document.createElement("span");
      badge.className = "psplus-badge";
      let html = PS_ICON_MINI + " PS+ " + m.tier + "档";
      if (Number(m.tier) >= 2 && latestExited) {
        html += " " + EXIT_ICON;
      }
      badge.innerHTML = html;
      node.appendChild(badge);
    });
}

const CARD_SELECTOR = [
  "[data-ds-appid]",
  "a.match",
  '[id^="searchSuggestions"] a[href*="/app/"]',
].join(", ");

function scanCards(appMap) {
  const pageAppId = getIdsFromUrl().appId;
  document
    .querySelectorAll(CARD_SELECTOR)
    .forEach(function (node) {
      const appId = appIdFromNode(node);
      if (!appId || appId === pageAppId) return;
      renderCardBadge(node, appId, appMap[appId] || []);
    });

  if (location.pathname.indexOf("/wishlist/") === 0) {
    document
      .querySelectorAll('a[href*="/app/"]')
      .forEach(function (a) {
        if (!a.querySelector("img")) return;
        const m = /\/app\/(\d+)/.exec(a.getAttribute("href") || "");
        if (!m) return;
        renderCardBadge(a, m[1], appMap[m[1]] || []);
      });
  }
}

function scheduleScan(appMap, scanner) {
  if (scanner._timer) clearTimeout(scanner._timer);
  scanner._timer = setTimeout(function () {
    scanner(appMap);
  }, 200);
}

function observe(appMap, scanner) {
  if (!window.MutationObserver) return;
  const obs = new MutationObserver(function () {
    scheduleScan(appMap, scanner);
  });
  obs.observe(document.body, { childList: true, subtree: true });
}

(function () {
  GM_addStyle(CSS);

  if (typeof GM_registerMenuCommand === "function") {
    GM_registerMenuCommand("PS+ 强制刷新会免数据", function () {
      GM_deleteValue(CONFIG.CACHE_KEY);
      location.reload();
    });
  }

  loadData().then(function (data) {
    if (!data || !data.games) return;

    const maps = buildMaps(data.games);

    if (/^\/(?:app|sub|bundle)\//.test(location.pathname)) {
      renderStoreBanners(maps.app, maps.sub);
    }
    scanCards(maps.app);
    observe(maps.app, scanCards);
  });
})();