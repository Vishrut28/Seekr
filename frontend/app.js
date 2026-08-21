const $ = (s, r=document)=>r.querySelector(s);
const esc = (s)=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (n)=>(n??0).toLocaleString();

/* The Deccan mark: the company's logo, traced from the official artwork
   rather than drawn by eye. It is a filled outline — the inner counter is a
   second subpath under fill-rule evenodd — which keeps the hand-drawn
   variation in stroke weight that a uniform stroke cannot reproduce.
   The viewBox has padding built in, so nothing clips at any size. */
const MARK_PATH = "M250.9 16.0L275.4 17.0L304.1 23.2L328.7 32.4L348.1 42.6L362.4 51.8L381.9 67.2L400.3 85.6L416.7 105.0L431.0 126.5L436.1 138.8L438.2 149.0L437.2 179.8L433.1 192.0L418.7 219.7L406.4 239.1L391.1 259.6L325.6 335.3L312.3 354.8L291.8 394.7L271.4 447.9L264.2 462.2L256.0 474.5L245.8 484.7L231.4 492.9L219.2 496.0L199.7 496.0L184.4 491.9L167.0 483.7L148.5 469.4L134.2 453.0L119.9 431.5L108.6 409.0L93.3 368.1L93.3 364.0L91.2 359.9L86.1 334.3L84.1 329.2L81.0 304.6L78.9 297.4L75.9 260.6L74.8 259.6L74.8 236.0L73.8 235.0L74.8 199.2L75.9 198.2L77.9 179.8L84.1 160.3L93.3 146.0L105.6 132.7L140.3 104.0L159.8 84.6L186.4 50.8L202.8 34.4L213.0 27.3L225.3 21.1L241.7 17.0L249.9 17.0ZM157.7 127.6L178.2 108.1L208.9 69.2L221.2 56.9L227.3 52.8L241.7 46.7L270.3 45.7L290.8 49.8L311.3 56.9L338.9 71.3L350.2 79.5L362.4 89.7L390.1 119.4L402.4 135.7L408.5 150.1L409.5 162.4L407.5 171.6L404.4 180.8L393.1 203.3L369.6 239.1L353.2 259.6L351.2 260.6L308.2 310.8L287.7 339.4L271.4 369.1L271.4 371.1L260.1 394.7L244.7 435.6L237.6 451.0L231.4 460.2L226.3 465.3L219.2 469.4L214.0 470.4L198.7 469.4L180.3 460.2L167.0 447.9L152.6 429.5L139.3 405.9L132.2 386.5L130.1 384.4L130.1 381.4L124.0 366.0L113.7 328.2L113.7 322.0L108.6 297.4L108.6 290.3L107.6 289.3L107.6 282.1L105.6 272.9L105.6 261.6L104.5 260.6L103.5 211.5L104.5 210.5L105.6 190.0L108.6 178.7L116.8 164.4L127.0 153.1L156.7 128.6Z";
const markSvg = (cls, size) =>
  `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 512 512" aria-hidden="true">`
  + `<path d="${MARK_PATH}" fill="currentColor" fill-rule="evenodd"/></svg>`;

const ICON = {
  logo: markSvg('', 15),
  search:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5"/></svg>',
  people:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="3.2"/><path d="M22 20v-2a4 4 0 0 0-3-3.87"/></svg>',
  review:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
  plug:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6M15 2v6"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/></svg>',
  caret:'<svg class="caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
  back:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
  thumbUp:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M7 22V10l5-8a2.5 2.5 0 0 1 2.4 3.2L13.5 9H19a2.5 2.5 0 0 1 2.4 3.1l-1.7 7A2.5 2.5 0 0 1 17.3 22z"/><rect x="2" y="10" width="5" height="12" rx="1"/></svg>',
  thumbDown:'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M17 2v12l-5 8a2.5 2.5 0 0 1-2.4-3.2l.9-3.8H5a2.5 2.5 0 0 1-2.4-3.1l1.7-7A2.5 2.5 0 0 1 6.7 2z"/><rect x="17" y="2" width="5" height="12" rx="1"/></svg>',
  eye:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="2.7"/></svg>',
  eyeOff:'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10.7 6.2A9.9 9.9 0 0 1 12 5.5c6.4 0 10 6.5 10 6.5a18 18 0 0 1-3.2 4M6.3 7.9A17.7 17.7 0 0 0 2 12s3.6 6.5 10 6.5a10 10 0 0 0 4-.8"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="m3 3 18 18"/></svg>',
  bookmark:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
  linkedin:'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5M2.5 9.5h5V21h-5zM10 9.5h4.7v1.6c.7-1.2 2-1.9 3.6-1.9 3 0 4.2 1.9 4.2 5.2V21h-5v-5.6c0-1.5-.5-2.4-1.8-2.4-1 0-1.6.7-1.9 1.4-.1.2-.1.6-.1.9V21h-5z"/></svg>',
  github:'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 0 0-3.2 19.5c.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.3-3.4-1.3-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.3-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.4 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10 10 0 0 0 12 2"/></svg>',
  empty:'<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M16 16l5 5"/></svg>',
};

/* ---------------- auth ---------------- */
const token = ()=>localStorage.getItem("seekr_token") || localStorage.getItem("rip_token") || "";
async function api(path, opts={}){
  const res = await fetch(path, {...opts, headers:{...(opts.headers||{}), "Authorization":"Bearer "+token()}});
  if(res.status===401){ renderGate(); throw new Error("unauthorized"); }
  if(!res.ok){
    let detail = "request failed ("+res.status+")";
    try{ const j = await res.json(); if(j.detail) detail = typeof j.detail==="string"?j.detail:JSON.stringify(j.detail); }catch(e){}
    throw new Error(detail);
  }
  return res.json();
}
function renderGate(){
  $("#root").innerHTML = `<div class="gate"><div class="gatebox">
    <div class="mark">${ICON.logo}</div>
    <h1>Sign in to Seekr</h1>
    <p>Paste the API token (RIP_API_TOKEN). It is stored only in this browser.</p>
    <div class="secretfield">
      <input id="tok" type="password" placeholder="Token" autocomplete="off"
             spellcheck="false" autocapitalize="off" autofocus>
      <button type="button" id="tokeye" class="reveal" aria-pressed="false"
              aria-label="Show token" title="Show token"
              onclick="toggleTokenVisible()">${ICON.eye}</button>
    </div>
    <button class="btn primary" onclick="saveToken()">Continue</button>
  </div></div>`;
  $("#tok").addEventListener("keydown", e=>{ if(e.key==="Enter") saveToken(); });
}
/* The token is masked by default — it is a credential, and people paste it
   with someone watching. The eye is for checking a paste actually landed. */
function toggleTokenVisible(){
  const input = $("#tok"), btn = $("#tokeye");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.innerHTML = show ? ICON.eyeOff : ICON.eye;
  btn.setAttribute("aria-pressed", String(show));
  const label = show ? "Hide token" : "Show token";
  btn.setAttribute("aria-label", label);
  btn.title = label;
  input.focus();
}
function saveToken(){ localStorage.setItem("seekr_token", $("#tok").value.trim()); route(); }
function signOut(){ localStorage.removeItem("seekr_token"); localStorage.removeItem("rip_token"); renderGate(); }

/* ---------------- shell ---------------- */
const NAV = [
  ["#/search", "Search", ICON.search],
  ["#/shortlists", "Shortlists", ICON.bookmark],
  ["#/review", "Review", ICON.review],
  ["#/sources", "Sources", ICON.plug],
];
function shell(active, topbar, body){
  $("#root").innerHTML = `<div class="shell">
    <aside class="rail">
      <a class="brand" href="/ui#/search" onclick="reloadApp(event)"
         title="Reload Seekr" aria-label="Reload Seekr">
        <div class="mark">${ICON.logo}</div>
        <div><b>Seekr</b><span>by Deccan<sup>AI</sup></span></div>
      </a>
      <nav>${NAV.map(([href,label,icon])=>
        `<a href="${href}" class="${active===href?"active":""}">${icon}${label}</a>`).join("")}</nav>
      <div class="rail-foot" id="railstats">
        <button class="themebtn" onclick="toggleTheme()">Toggle theme</button>
        <button class="themebtn" onclick="signOut()">Sign out</button>
      </div>
    </aside>
    <main>
      <div class="backdrop" aria-hidden="true">${markBackdrop()}</div>
      <div class="topbar"${topbar?"":" hidden"}>${topbar||""}</div>
      <div class="page" id="page">${body}</div>
    </main>
  </div>`;
  loadRailStats();
  bindScrollShade();
}
function reloadApp(e){
  if(e) e.preventDefault();
  if(window.matchMedia("(prefers-reduced-motion: reduce)").matches){
    location.reload();
    return;
  }
  document.body.classList.add("is-reloading");
  setTimeout(()=>location.reload(), 240);
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur==="dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("seekr_theme", next);
}
(function(){ const t=localStorage.getItem("seekr_theme"); if(t) document.documentElement.setAttribute("data-theme",t); })();
document.addEventListener("animationend", e=>{
  if(e.animationName==="arrive") document.body.classList.remove("is-arriving");
});

async function loadRailStats(){
  const el = $("#railstats"); if(!el) return;
  try{
    const [sources, countries] = await Promise.all([
      api("/v1/facets?field=source"), api("/v1/facets?field=country&limit=200"),
    ]);
    const people = sources.values.reduce((m,v)=>Math.max(m,v.people),0);
    el.insertAdjacentHTML("afterbegin",
      `<div class="stat"><span>People</span><b data-to="${people}">0</b></div>
       <div class="stat"><span>Sources</span><b data-to="${sources.values.length}">0</b></div>
       <div class="stat"><span>Countries</span><b data-to="${countries.values.length}">0</b></div>`);
    el.querySelectorAll("b[data-to]").forEach(countUp);
  }catch(e){}
}

/* A number that lands on its value rather than appearing fully formed. Short
   enough not to keep anyone waiting to read it, and skipped outright for
   anyone who asked for less motion. */
function countUp(el){
  const target = Number(el.dataset.to || 0);
  if (!target || window.matchMedia("(prefers-reduced-motion: reduce)").matches){
    el.textContent = fmt(target);
    return;
  }
  const started = performance.now(), ms = 620;
  (function step(now){
    const t = Math.min(1, (now - started) / ms);
    const eased = 1 - Math.pow(1 - t, 3);      // ease-out: fast, then settles
    el.textContent = fmt(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(step);
  })(started);
}

/* The topbar earns its shadow only once there is something scrolled under it. */
function bindScrollShade(){
  const bar = $(".topbar"); if(!bar) return;
  const onScroll = () => bar.classList.toggle("scrolled", window.scrollY > 4);
  window.removeEventListener("scroll", window.__seekrShade || (()=>{}));
  window.__seekrShade = onScroll;
  window.addEventListener("scroll", onScroll, {passive:true});
  onScroll();
}

/* ---------------- search ---------------- */
const EXAMPLES = [
  "machine learning at University of Toronto",
  "deep learning, top 20",
  "product designers at Swiggy",
];
const FIELDS = {
  f_country:"country", f_source:"source", f_org:"organization",
  f_curorg:"current_organization", f_edu:"education", f_role:"role",
  f_skill:"skill", f_tech:"technology", f_loc:"location",
  f_pubs:"min_publications", f_cites:"min_citations", f_active:"active_since",
  f_srcs:"min_sources", f_sort:"sort",
};
let PAGE = {mode:"query", q:"", offset:0, rows:[]};
let LIVE = sessionStorage.getItem("seekr_live") === "1";

function searchTopbar(q){
  const filled = !!(q||"").trim();
  return `<div class="searchrow">
    <div class="searchwrap">
      ${ICON.search}
      <input class="search" id="q" placeholder="Search people, skills, organizations…" value="${esc(q||"")}">
      <button type="button" class="qclear" id="qclear" ${filled?"":"hidden"}
              onclick="clearBox()" aria-label="Clear search" title="Clear">×</button>
      <span class="kbd">/</span>
    </div>
    <button class="btn primary" onclick="runQuery()">Search</button>
    <button class="btn livebtn${LIVE?" on":""}" id="livebtn" onclick="toggleLive()"
            title="Also query live sources (GitHub, OpenAlex, the web)"
            aria-pressed="${LIVE?"true":"false"}">
      <span class="livedot" aria-hidden="true"></span> Live
    </button>
  </div>`;
}
function syncClearBtn(){
  const q = $("#q"), b = $("#qclear");
  if(b) b.hidden = !(q && q.value.trim());
}
function clearBox(){
  const q = $("#q"); if(q) q.value = "";
  clearSearchResults();
  syncClearBtn();
  q?.focus();
}
function paintLiveBtn(){
  const b = $("#livebtn"); if(!b) return;
  b.classList.toggle("on", LIVE);
  b.setAttribute("aria-pressed", String(LIVE));
}
function toggleLive(){
  LIVE = !LIVE;
  sessionStorage.setItem("seekr_live", LIVE ? "1" : "0");
  paintLiveBtn();
  if($("#q")?.value.trim()) runQuery(LIVE ? "true" : null);
}
function clearSearchResults(){
  PAGE = {mode:"query", q:"", offset:0, rows:[]};
  sessionStorage.removeItem("seekr_q");
  setWorking(false);
  const box = $("#results"); if(box) box.innerHTML = "";
}


/* Profile links, drawn as the brand people recognise. Only URLs a source
   actually published — never a handle guessed from someone's name. Ordered
   by how much the link tells you about a person: OpenAlex is on all 50k
   records and identifies nobody, so it sits last. */
const BRANDS = [
  ["linkedin.com", "LinkedIn", "#0a66c2",
    '<path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5M2.5 9.5h5V21h-5zM10 9.5h4.7v1.6c.7-1.2 2-1.9 3.6-1.9 3 0 4.2 1.9 4.2 5.2V21h-5v-5.6c0-1.5-.5-2.4-1.8-2.4-1 0-1.6.7-1.9 1.4-.1.2-.1.6-.1.9V21h-5z"/>'],
  ["github.com", "GitHub", "#24292f",
    '<path d="M12 2a10 10 0 0 0-3.2 19.5c.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.3-3.4-1.3-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.3-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.4 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10 10 0 0 0 12 2"/>'],
  ["scholar.google.com", "Google Scholar", "#4285f4",
    '<path d="M12 2 1 8.5l11 6.5 9-5.3v6.8h2V8.5z"/><path d="M5.5 13.2v3.6c0 2 2.9 3.7 6.5 3.7s6.5-1.7 6.5-3.7v-3.6L12 17z"/>'],
  ["orcid.org", "ORCID", "#a6ce39",
    '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M8.2 6.1a1.1 1.1 0 1 1 0 2.2 1.1 1.1 0 0 1 0-2.2m-.9 3.3h1.8v8.3H7.3zm4 0h3.3c2.6 0 4.2 1.9 4.2 4.2s-1.7 4.1-4.2 4.1h-3.3zm1.8 1.6v5.1h1.4c1.7 0 2.5-1.1 2.5-2.5s-.8-2.6-2.5-2.6z"/>'],
  ["twitter.com", "X", "#111827",
    '<path d="M17.5 3h3l-6.6 7.6L21.8 21h-6l-4.7-6.2L5.6 21h-3l7.1-8.1L2.5 3h6.2l4.3 5.7zm-1 16h1.7L7.6 4.7H5.8z"/>'],
  ["x.com", "X", "#111827",
    '<path d="M17.5 3h3l-6.6 7.6L21.8 21h-6l-4.7-6.2L5.6 21h-3l7.1-8.1L2.5 3h6.2l4.3 5.7zm-1 16h1.7L7.6 4.7H5.8z"/>'],
  ["stackoverflow.com", "Stack Overflow", "#f48024",
    '<path d="M17 21v-6h2v8H3v-8h2v6z"/><path d="m6.9 14.7 8.6 1.8.4-2-8.6-1.8zm1.1-4.6 8 3.7.8-1.8-8-3.7zm2.2-4.3 6.8 5.6 1.3-1.5-6.8-5.7zM14.5 1l-1.6 1.2 5.3 7.1 1.6-1.2z"/>'],
  ["huggingface.co", "Hugging Face", "#ff9d00",
    '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20M8.5 9.5a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4m7 0a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4M12 18c-2.4 0-4.4-1.6-4.9-3.7h9.8C16.4 16.4 14.4 18 12 18"/>'],
  ["semanticscholar.org", "Semantic Scholar", "#1857b6",
    '<path d="M3 4h8.5c5 0 9.5 3.4 9.5 8.5S16.9 21 12 21H3l4-4h5c2.8 0 5-1.9 5-4.5S14.8 8 12 8H3z"/>'],
  ["dblp.org", "dblp", "#004a99",
    '<path d="M7 2h6.5c3.6 0 6 2.6 6 6.4V22H13V8.6C13 7 12 6 10.4 6H7zM4.5 10H9v12H4.5z"/>'],
  ["wikipedia.org", "Wikipedia", "#3366cc",
    '<path d="M2 5h5.6v1.4l-1.4.3 3.5 8.6 2.3-5.6-1.2-3-1.2-.3V5h5.2v1.4l-1.3.3 3.4 8.6 3.2-8.6-1.5-.3V5H22v1.4l-1.4.4L16.2 20h-1.5l-3-7.2L8.6 20H7.1L2.9 6.8 2 6.4z"/>'],
  ["wikidata.org", "Wikidata", "#339966",
    '<path d="M2 6h1.6v12H2zm2.9 0h1.6v12H4.9zm2.9 0h3.2v12H7.8zm4.5 0H14v12h-1.7zm3 0h3.2v12h-3.2zM22 6h-1.6v12H22z"/>'],
  ["researchgate.net", "ResearchGate", "#00ccbb",
    '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20m-2.6 6h2.4c1.6 0 2.6 1 2.6 2.4 0 1.1-.6 1.9-1.6 2.2l2 3.4h-1.8l-1.8-3.1h-.6V16H9.4zm1.2 1.3v2.4h1c.8 0 1.3-.4 1.3-1.2s-.5-1.2-1.3-1.2z"/>'],
  ["openalex.org", "OpenAlex", "#7c3aed",
    '<path d="M12 2 2 20h4l6-11 6 11h4z"/>'],
];
const MAX_BRAND_LINKS = 6;

function brandLinks(urls, max){
  const seen = new Set();
  const used = new Set();
  const out = [];
  const cap = max || MAX_BRAND_LINKS;
  for(const [host, label, color, path] of BRANDS){
    const hit = (urls||[]).find(u=>{
      try{ const h = new URL(u).hostname.replace(/^www\./, "");
           return h === host || h.endsWith("." + host); }catch(e){ return false; }
    });
    if(!hit || seen.has(label)) continue;
    seen.add(label);
    used.add(hit);
    out.push(`<a class="plink" href="${esc(hit)}" target="_blank" rel="noopener noreferrer"
      title="${esc(label)}" aria-label="${esc(label)}" style="--plink:${color}"
      onclick="event.stopPropagation()">
      <svg viewBox="0 0 24 24" fill="currentColor" width="10" height="10">${path}</svg></a>`);
    if(out.length >= cap) break;
  }
  for(const u of urls||[]){
    if(out.length >= cap || used.has(u)) continue;
    try{ new URL(u); }catch(e){ continue; }
    used.add(u);
    out.push(`<a class="plink" href="${esc(u)}" target="_blank" rel="noopener noreferrer"
      title="Profile page" aria-label="Profile page" style="--plink:#6b7280"
      onclick="event.stopPropagation()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="10" height="10">
        <circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>
      </svg></a>`);
  }
  return out.length ? `<div class="plinks">${out.join("")}</div>` : "";
}

const SOURCE_HOST = {
  github: "github.com",
  openalex: "openalex.org",
  semanticscholar: "semanticscholar.org",
  orcid: "orcid.org",
  dblp: "dblp.org",
  stackoverflow: "stackoverflow.com",
  huggingface: "huggingface.co",
  wikidata: "wikidata.org",
  wikipedia: "wikipedia.org",
  linkedin: "linkedin.com",
  scholar: "scholar.google.com",
  exa: null,
  web: null,
};

function hostOf(url){
  try{ return new URL(url).hostname.replace(/^www\./, ""); }catch(e){ return ""; }
}

function urlForSource(source, urls, links){
  const named = (links||[]).find(x => (x.source||"").toLowerCase() === (source||"").toLowerCase());
  if(named && named.url) return named.url;
  const host = SOURCE_HOST[(source||"").toLowerCase()];
  if(!host) return null;
  return (urls||[]).find(u => {
    const h = hostOf(u);
    return h === host || h.endsWith("." + host);
  }) || null;
}

function sourcePills(p){
  const urls = p.profile_urls || [];
  const links = p.source_links || [];
  const srcs = [];
  const seen = new Set();
  const add = (name, url)=>{
    const key = (name||"").toLowerCase() || url;
    if(!key || seen.has(key)) return;
    seen.add(key);
    srcs.push({name: name || hostOf(url) || "profile", url});
  };
  for(const x of links) add(x.source, x.url);
  for(const s of [...new Set((p.attributes||[]).flatMap(a=>a.sources||[]))]){
    add(s, urlForSource(s, urls, links));
  }
  for(const u of urls){
    const host = hostOf(u);
    const known = Object.entries(SOURCE_HOST).find(([,h])=>h && (host===h || host.endsWith("."+h)));
    add(known ? known[0] : host, u);
  }
  if(!srcs.length) return '<span class="muted">—</span>';
  return srcs.map(x => sourcePill(x.name, x.url)).join("");
}

function sourcePill(label, url){
  if(url) return `<a class="srcpill" href="${esc(url)}" target="_blank" rel="noopener noreferrer"
    title="${esc(url)}" onclick="event.stopPropagation()">${esc(label)}</a>`;
  return `<span class="srcpill">${esc(label)}</span>`;
}

function suggestionUrl(x){
  if(x.url) return x.url;
  const id = x.external_id;
  if(!id) return null;
  if(x.source==="github") return "https://github.com/"+id;
  if(x.source==="openalex") return String(id).startsWith("http") ? id : "https://openalex.org/"+id;
  if(x.source==="orcid") return String(id).startsWith("http") ? id : "https://orcid.org/"+id;
  if(x.source==="dblp") return "https://dblp.org/pid/"+id;
  if(x.source==="huggingface") return "https://huggingface.co/"+id;
  if(x.source==="stackoverflow") return "https://stackoverflow.com/users/"+id;
  if(x.source==="semanticscholar") return "https://www.semanticscholar.org/author/"+id;
  return null;
}

/* ---------------- recent searches ---------------- */
const RECENT_KEY = "seekr_recent";
const recent = ()=>{ try{ return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; }catch(e){ return []; } };
function rememberQuery(q){
  const list = [q, ...recent().filter(x=>x!==q)].slice(0, 8);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}
function paintRecent(){
  const box = $("#recent"); if(!box) return;
  const list = recent();
  box.hidden = !list.length;
  box.innerHTML = `<em>Recent</em>` + list.map(x=>
    `<button class="chipbtn" onclick="useExample(this)">${esc(x)}</button>`).join("")
    + (list.length ? `<button class="chipbtn muted" onclick="clearRecent()">clear</button>` : "");
}
function clearRecent(){ localStorage.removeItem(RECENT_KEY); paintRecent(); }

/* Trending is drawn from the corpus itself — the roles and skills most people
   in Seekr actually carry, refreshed from /v1/facets on every visit. It
   reflects this graph, not an outside market index. */
async function paintTrending(){
  const box = $("#trending"); if(!box) return;
  try{
    // Roles only. Skills skew to whatever the corpus happens to hold — the
    // top ones today are particle physics topics, which is not a job anyone
    // searches for. Job titles are what people actually look for.
    const roles = await api("/v1/facets?field=role&limit=8");
    const chips = (roles.values||roles.facets||[])
      .map(v=>v.value).filter(Boolean).slice(0,6);
    if(chips.length) box.innerHTML = `<em>Trending</em>` + chips.map(x=>
      `<button class="chipbtn" onclick="useExample(this)">${esc(x)}</button>`).join("");
  }catch(e){ /* keep the static examples if facets are unavailable */ }
}

async function renderSearch(){
  // An empty box must show no results. Do not replay the last query — that
  // left "Rahul" on screen after the input was cleared.
  shell("#/search", searchTopbar(""), `
    <div class="examples" id="trending"><em>Trending</em>${EXAMPLES.map(x=>
      `<button class="chipbtn" onclick="useExample(this)">${esc(x)}</button>`).join("")}</div>
    <div class="examples" id="recent" hidden><em>Recent</em></div>
    <details class="filters" id="filterbox">
      <summary>${ICON.caret} Filters <span class="count" id="fcount" hidden>0</span></summary>
      <div class="fgrid">
        <label>Country<select id="f_country"><option value="">Any</option></select></label>
        <label>Source<select id="f_source"><option value="">Any</option></select></label>
        <label>Organization<input id="f_org" placeholder="Ever affiliated"></label>
        <label>Current employer<input id="f_curorg" placeholder="Present only"></label>
        <label>Studied at<input id="f_edu" placeholder="University"></label>
        <label>Role<input id="f_role" placeholder="e.g. professor"></label>
        <label>Skill<input id="f_skill" placeholder="e.g. nlp"></label>
        <label>Technology<input id="f_tech" placeholder="e.g. rust"></label>
        <label>Location<input id="f_loc" placeholder="City or region"></label>
        <label>Min publications<input id="f_pubs" type="number" min="0" placeholder="0"></label>
        <label>Min citations<input id="f_cites" type="number" min="0" placeholder="0"></label>
        <label>Active since<input id="f_active" placeholder="YYYY"></label>
        <label>Min sources<input id="f_srcs" type="number" min="1" placeholder="1"></label>
        <label>Sort<select id="f_sort">
          <option value="relevance">Best match</option>
          <option value="recent">Recently updated</option>
          <option value="name">Name A–Z</option></select></label>
        <label class="chk"><input type="checkbox" id="f_cv"> Has CV</label>
        <label class="chk"><input type="checkbox" id="f_email"> Has email</label>
      </div>
      <p class="fhint">Text filters match whole words — <code>go</code> finds Go,
        not Cognitive. Add <code>*</code> for a loose match: <code>go*</code>
        also finds Golang.</p>
      <div class="filter-actions">
        <button class="btn primary" onclick="runFilters()">Apply filters</button>
        <button class="btn" onclick="clearFilters()">Clear</button>
      </div>
    </details>
    <div id="results"></div>`);
  bindSearchKeys();
  loadFacets();
  paintRecent();
  paintTrending();
}
function useExample(el){ $("#q").value = el.textContent; runQuery(); }
function bindSearchKeys(){
  const q = $("#q");
  q.addEventListener("keydown", e=>{
    if(e.key==="Enter") runQuery();
    if(e.key==="Escape"){ q.value=""; clearSearchResults(); syncClearBtn(); q.blur(); }
  });
  q.addEventListener("input", ()=>{
    syncClearBtn();
    if(!q.value.trim()) clearSearchResults();
  });
  document.addEventListener("keydown", e=>{
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if((e.key==="/" && !typing) || ((e.metaKey||e.ctrlKey) && e.key==="k")){
      e.preventDefault(); $("#q")?.focus(); $("#q")?.select();
    }
  });
}
async function loadFacets(){
  for(const [id, field] of [["f_country","country"],["f_source","source"]]){
    try{
      const d = await api("/v1/facets?field="+field+"&limit=100");
      const el = $("#"+id); if(!el) continue;
      el.innerHTML = `<option value="">Any</option>` + d.values.map(v=>
        `<option value="${esc(v.value)}">${esc(v.value)} · ${fmt(v.people)}</option>`).join("");
    }catch(e){}
  }
  // Free-text filters now match whole words, so a guessed fragment finds
  // nothing. Offer the values that actually exist, with their people-counts,
  // instead of making people guess at the vocabulary.
  for(const [id, field] of [["f_org","organization"],["f_curorg","organization"],
                            ["f_edu","organization"],["f_role","role"],
                            ["f_skill","skill"],["f_tech","skill"]]){
    const el = $("#"+id); if(!el) continue;
    const listId = id+"_list";
    el.setAttribute("list", listId);
    if(!$("#"+listId)){
      const dl = document.createElement("datalist");
      dl.id = listId;
      el.parentElement.appendChild(dl);
    }
    try{
      const d = await api("/v1/facets?field="+field+"&limit=150");
      $("#"+listId).innerHTML = d.values.map(v=>
        `<option value="${esc(v.value)}">${fmt(v.people)} people</option>`).join("");
    }catch(e){}
  }
}
function filterParams(){
  const p = new URLSearchParams();
  for(const [id, name] of Object.entries(FIELDS)){
    const el = $("#"+id);
    if(el && el.value && el.value !== "relevance") p.set(name, el.value);
  }
  if($("#f_cv")?.checked) p.set("has_cv","true");
  if($("#f_email")?.checked) p.set("has_email","true");
  return p;
}
function syncFilterCount(){
  const n = [...filterParams().keys()].length;
  const badge = $("#fcount"); if(!badge) return;
  badge.textContent = n; badge.hidden = n===0;
}
function clearFilters(){
  Object.keys(FIELDS).forEach(id=>{ const e=$("#"+id); if(e) e.value=""; });
  ["f_cv","f_email"].forEach(id=>{ const e=$("#"+id); if(e) e.checked=false; });
  const s=$("#f_sort"); if(s) s.value="relevance";
  syncFilterCount();
  $("#results").innerHTML = "";
}

// Waiting is drawn with the brand mark rather than a generic spinner.
/* The watermark IS the loading indicator. One mark on the page rather than a
   second, smaller copy appearing next to it while the first sits there
   inert. The fill layer is invisible until work is in flight. */
function markBackdrop(){
  // Two stacked copies of the mark. The upper one is revealed from the
  // bottom with a CSS inset clip — animating an SVG <clipPath> rect is not
  // reliably supported, and silently did nothing.
  return `
  <svg width="465" height="465" viewBox="0 0 512 512" aria-hidden="true">
    <path class="bdbase" d="${MARK_PATH}" fill="currentColor" fill-rule="evenodd"/>
    <path class="bdfill" d="${MARK_PATH}" fill="currentColor" fill-rule="evenodd"/>
  </svg>`;
}

/* Marks the whole page as working, so the watermark animates and any caller
   can show its own message underneath. */
function setWorking(on){ document.body.classList.toggle("is-working", !!on); }
function busy(msg){
  setWorking(true);
  $("#results").innerHTML = `<div class="loading">${esc(msg)}</div>`;
}

async function runQuery(discover, offset){
  const q = $("#q").value.trim();
  if(!q){ clearSearchResults(); return; }
  if(discover == null && LIVE) discover = "true";
  sessionStorage.setItem("seekr_q", q);
  rememberQuery(q);
  paintRecent();
  const paging = typeof offset === "number" && offset > 0;
  if(!paging){ PAGE = {mode:"query", q, offset:0, rows:[]}; busy(discover?"Querying live sources…":"Searching…"); }
  try{
    const data = await api("/v1/query?q="+encodeURIComponent(q)
      + (paging?"&offset="+offset:"") + (discover?"&discover="+discover:""));
    renderResults(data, {discover});
  }catch(e){ if(e.message!=="unauthorized") $("#results").innerHTML = `<div class="banner">${esc(e.message)}</div>`; }
}
async function runFilters(offset){
  syncFilterCount();
  const params = filterParams();
  // The search box holds a question; on this endpoint `q` is a NAME filter.
  // Sending a whole sentence there matches nobody and silently empties the
  // result, so only a short, name-shaped value is passed through.
  const q = $("#q").value.trim();
  if(q && q.split(/\s+/).length <= 3) params.set("q", q);
  const paging = typeof offset === "number" && offset > 0;
  if(!paging){ PAGE = {mode:"filters", q, offset:0, rows:[]}; busy("Filtering…"); }
  if(paging) params.set("offset", offset);
  params.set("limit", 50);
  try{ renderResults(await api("/v1/persons?"+params.toString()), {filtered:true}); }
  catch(e){ if(e.message!=="unauthorized") $("#results").innerHTML = `<div class="banner">${esc(e.message)}</div>`; }
}
function loadMore(){
  const b = $("#more"); if(b){ b.disabled = true; b.textContent = "Loading…"; }
  return PAGE.mode==="filters" ? runFilters(PAGE.offset) : runQuery(null, PAGE.offset);
}

function renderResults(data, opts={}){
  setWorking(false);
  data = data || {};
  const f = data.applied_filters;
  const pills = f ? [
    ...(f.skills||[]).map(s=>`<span class="pill"><b>skill</b>${esc(s)}</span>`),
    ...(f.skill_patterns||[]).map(s=>`<span class="pill"><b>matches</b>${esc(s)}</span>`),
    ...(f.organizations||[]).map(o=>`<span class="pill"><b>org</b>${esc(o)}</span>`),
    ...(f.locations||[]).map(l=>`<span class="pill"><b>place</b>${esc(l)}</span>`),
    ...(f.countries||[]).map(c=>`<span class="pill"><b>country</b>${esc(c)}</span>`),
    ...(f.name_terms||[]).map(n=>`<span class="pill"><b>name</b>${esc(n)}</span>`),
  ].join("") : "";
  const um = data.unmatched_terms||[];
  const nf = data.not_found||[];
  const unmatched = um.length
    ? `<span class="pill warn">not applied: ${um.map(esc).join(", ")}</span>` : "";
  const notFoundPills = nf.map(x=>
    `<span class="pill warn">${esc(x.term)} not found</span>`).join("");

  const rows = (data.results||[]).map(p=>{
    const skills = (p.attributes||[])
      .filter(a=>a.attribute_type==="skill"||a.attribute_type==="research_interest")
      .slice(0,3).map(a=>esc(a.value)).join(", ");
    const primary = p.matched_organization || p.current_organization || "";
    const others = (p.organizations||[]).filter(o=>o!==primary);
    const orgSub = [
      p.matched_organization && p.current_organization && p.current_organization!==primary
        ? "now: "+esc(p.current_organization) : "",
      others.length ? "+"+others.length+" more" : "",
    ].filter(Boolean).join(" · ");
    // fetched live for this query rather than already in the corpus
    const liveTag = p.from_live_search ? '<span class="livetag">new</span>' : "";
    const links = brandLinks(p.profile_urls);
    return `<tr onclick="location.hash='#/person/${p.id}'">
      <td class="nm">${esc(p.canonical_name||"Unnamed")}${liveTag}${links}</td>
      <td class="org">${primary?esc(primary):'<span class="muted">—</span>'}
        ${orgSub?`<div class="sub2">${orgSub}</div>`:""}</td>
      <td class="org">${esc(p.location||"")||'<span class="muted">—</span>'}</td>
      <td class="sk">${skills||'<span class="muted">—</span>'}</td>
      <td>${sourcePills(p)}</td>
      <td class="vote" onclick="event.stopPropagation()">
        <button class="vbtn" title="Good match for this query"
          onclick="vote('${p.id}','good',this)">${ICON.thumbUp}</button>
        <button class="vbtn" title="Bad match for this query"
          onclick="vote('${p.id}','bad',this)">${ICON.thumbDown}</button>
        <button class="vbtn save" title="Save to a shortlist"
          onclick="saveTo('${p.id}',this)">${ICON.bookmark}</button>
      </td>
    </tr>`;
  });
  PAGE.rows = PAGE.rows.concat(rows);
  PAGE.offset = data.next_offset ?? PAGE.rows.length;

  const total = Math.max(data.total_matches ?? 0, PAGE.rows.length);
  // A dropped constraint must be loud: results that silently ignore
  // "in Hyderabad" read as wrong answers rather than a coverage gap.
  // A corrected spelling must be visible, or the answer quietly belongs to a
  // different question than the one that was asked.
  const corr = data.corrections || [];
  const corrBanner = corr.length ? `
    <div class="banner">Showing results for ${corr.map(c=>
      `<b>${esc(c.matched)}</b>`).join(", ")} — you typed ${corr.map(c=>
      `<i>${esc(c.typed)}</i>`).join(", ")}.</div>` : "";
  // the deployed snapshot cannot be written to, so live finds are not kept
  const roBanner = (data.storage === "read-only" && data.stored_from_live === 0
                    && (data.discovery_suggestions||[]).length) ? `
    <div class="banner warnbar">This deployment reads a fixed snapshot, so people
      found live are shown but not saved. Point <code>RIP_DATABASE_URL</code> at a
      writable database to let the graph grow here.</div>` : "";
  const unmatchedBanner = um.length ? `
    <div class="banner warnbar">
      <b>${um.map(esc).join(", ")}</b> ${um.length===1?"was":"were"} not applied —
      nothing in Seekr matches ${um.length===1?"that term":"those terms"} yet${
        PAGE.rows.length
          ? `, so these ${fmt(total)} results ignore ${um.length===1?"it":"them"}`
          : ""}.
      <button class="btn sm" onclick="runQuery('true')">Search paid sources too</button>
    </div>` : "";
  const appliedBits = (data.applied_clauses||[]).map(c=>c.term).join(" + ");
  const notFoundBanner = nf.length ? `
    <div class="banner warnbar">
      ${nf.map(x=>`<b>${esc(x.term)}</b> was not found as a ${esc(x.as)}`).join("; ")}
      ${PAGE.rows.length && appliedBits ? ` — showing ${esc(appliedBits)}` : ""}.
    </div>` : "";
  const sugg = data.discovery_suggestions || [];
  // Live hits are an OR dump across sources. Hide them on a corpus-only
  // search once AND-search already has people. When Live is on, keep them
  // visible — that is the rest of what upstream found.
  const showLive = sugg.length && (!PAGE.rows.length || opts.discover || LIVE);
  const suggBlock = showLive ? `
    <section class="block"><h2>Live candidates <span class="n">${sugg.length}</span></h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Name</th><th>Affiliation</th><th>Role &amp; place</th><th>Source</th><th></th></tr></thead>
        <tbody>${sugg.map(x=>`<tr>
          <td class="nm">${esc(x.name||"Unnamed")}</td>
          <td class="org">${esc(x.affiliation||"")||'<span class="muted">—</span>'}</td>
          <td class="sk">${esc([x.role,x.location].filter(Boolean).join(" · "))||'<span class="muted">—</span>'}</td>
          <td>${sourcePill(x.source, suggestionUrl(x))}</td>
          <td class="num"><button class="btn sm" onclick="event.stopPropagation();queueOne(${JSON.stringify(x.source||"")},${JSON.stringify(x.external_id||"")},this)">Add</button></td>
        </tr>`).join("")}</tbody></table></div></div></section>` : "";

  let main;
  if(PAGE.rows.length){
    main = `<div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Name</th><th>Organization</th><th>Location</th><th>Skills &amp; interests</th><th>Sources</th><th title="Was this a good match for your query?">Match</th></tr></thead>
        <tbody>${PAGE.rows.join("")}</tbody></table></div>
      ${data.has_more?`<div class="loadmore"><button class="btn" id="more" onclick="loadMore()">Load 50 more</button></div>`:""}
      </div>`;
  } else if(data.matched_nothing){
    main = emptyState("No filters could be applied",
      data.explanation || "None of those terms exist in the corpus yet.",
      !opts.discover ? `<button class="btn primary" onclick="runQuery('true')">Search live sources</button>` : "");
  } else if(!sugg.length){
    const why = data.empty_reason;
    // With filters, show how each one does on its own — that is what tells
    // you which one to relax.
    const perFilter = (why && why.each_filter_alone || []).length
      ? `<ul class="whylist">` + why.each_filter_alone.map(a=>
          `<li><code>${esc(a.filter)}${a.value===true?"":"="+esc(String(a.value))}</code>
           ${a.matches===null?"—":fmt(a.matches)+" on its own"}</li>`).join("") + `</ul>`
      : "";
    main = emptyState("No matches",
      why ? why.message : "No one in the corpus matches these filters.",
      (!opts.discover && !opts.filtered)
        ? `<button class="btn primary" onclick="runQuery('true')">Search paid sources too</button>` : "",
      perFilter);
  } else { main = ""; }

  $("#results").innerHTML = `
    <div class="meta">
      <div class="count">${PAGE.rows.length?`<b>${fmt(PAGE.rows.length)}</b> of ${fmt(total)} matching`:""}</div>
      <div class="pills">${pills}${unmatched}${notFoundPills}</div>
    </div>
    ${corrBanner}${roBanner}${unmatchedBanner}${notFoundBanner}${main}${suggBlock}`;
}
function emptyState(title, body, action, extraHtml){
  // body is escaped; extraHtml is markup we built ourselves
  return `<div class="card"><div class="empty">
    <div class="icon markempty">${markSvg("", 30)}</div>
    <h3>${esc(title)}</h3><p>${esc(body)}</p>${extraHtml||""}${action||""}
  </div></div>`;
}
// Feedback is recorded against the query it was judged on. It is NOT used to
// reorder anything here — Seekr does not rank. It is training data for the
// separate ranking tool, readable at GET /v1/feedback.
async function vote(personId, verdict, btn){
  const cell = btn.parentElement;
  cell.querySelectorAll(".vbtn").forEach(b=>b.classList.remove("on"));
  btn.classList.add("on");
  try{
    await api("/v1/feedback", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({person_id: personId, verdict, query: PAGE.q || ""})});
    btn.title = "Recorded";
  }catch(e){ btn.classList.remove("on"); btn.title = "Could not record: "+e.message; }
}

/* ---------------- shortlists ---------------- */
async function saveTo(personId, btn){
  let lists = {shortlists: []};
  try{ lists = await api("/v1/shortlists"); }catch(e){ }
  const names = lists.shortlists.map(l=>l.name);
  const prompt_ = names.length
    ? `Save to which shortlist?\n\nExisting: ${names.join(", ")}\n\nType a name (new or existing):`
    : "Name your first shortlist:";
  const name = window.prompt(prompt_, names[0] || "Shortlist");
  if(!name) return;
  btn.disabled = true;
  try{
    const sl = await api("/v1/shortlists", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({name})});
    const r = await api(`/v1/shortlists/${sl.id}/members`, {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({person_id: personId, query: PAGE.q || ""})});
    btn.classList.add("on");
    btn.title = r.added ? `Saved to ${sl.name}` : `Already on ${sl.name}`;
  }catch(e){ btn.title = "Could not save: "+e.message; btn.disabled = false; }
}

async function renderShortlists(){
  shell("#/shortlists", null, (setWorking(true), `<div class="loading">Loading shortlists…</div>`));
  let data;
  try{ data = await api("/v1/shortlists"); }
  catch(e){ $("#results") && ($("#results").innerHTML = esc(e.message)); return; }
  if(!data.shortlists.length){
    shell("#/shortlists", null, `<div class="empty">${ICON.empty}
      <p>No shortlists yet. Save someone from a search to start one.</p></div>`);
    return;
  }
  const blocks = await Promise.all(data.shortlists.map(l=>api(`/v1/shortlists/${l.id}`)));
  shell("#/shortlists", null, blocks.map(b=>`
    <section class="block"><h2>${esc(b.name)} <span class="n">${b.count}</span></h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Name</th><th>Found by</th><th>Added</th><th></th></tr></thead>
        <tbody>${b.members.map(m=>`<tr onclick="location.hash='#/person/${m.person_id}'">
          <td class="nm">${esc(m.canonical_name||"Unnamed")}</td>
          <td class="sk">${esc(m.found_by_query||"")||'<span class="muted">—</span>'}</td>
          <td class="org">${esc(String(m.added_at||"").slice(0,10))}</td>
          <td class="num"><button class="btn sm" onclick="event.stopPropagation();
            unsave(${b.id},'${m.person_id}',this)">Remove</button></td>
        </tr>`).join("")}</tbody></table></div></div></section>`).join(""));
}

async function unsave(listId, personId, btn){
  btn.disabled = true;
  try{
    await api(`/v1/shortlists/${listId}/members/${personId}`, {method:"DELETE"});
    btn.closest("tr").remove();
  }catch(e){ btn.textContent = "Failed"; btn.disabled = false; }
}

async function queueOne(source, externalId, btn){
  btn.disabled = true; btn.textContent = "Adding…";
  try{
    const r = await api("/v1/leads", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({source, external_id: externalId, reason:"queued from Seekr UI"})});
    btn.textContent = r.status==="queued" ? "Queued" : r.status.replace(/_/g," ");
  }catch(e){ btn.textContent = "Failed"; btn.disabled = false; }
}

/* ---------------- person ---------------- */
async function renderPerson(id){
  shell(null, `<a class="btn sm" href="#/search">${ICON.back} Back to results</a>`,
    (setWorking(true), `<div class="loading">Loading profile…</div>`));
  let p, ev, pubs, projs, orgs, prov, conf, graph, docs;
  try{
    [p, ev, pubs, projs, orgs, prov, conf, graph, docs] = await Promise.all([
      api("/v1/persons/"+id), api("/v1/persons/"+id+"/evidence"),
      api("/v1/persons/"+id+"/publications"), api("/v1/persons/"+id+"/projects"),
      api("/v1/persons/"+id+"/organizations"), api("/v1/persons/"+id+"/provenance"),
      api("/v1/persons/"+id+"/conflicts"), api("/v1/persons/"+id+"/graph"),
      api("/v1/persons/"+id+"/documents"),
    ]);
  }catch(e){
    if(e.message!=="unauthorized") $("#page").innerHTML = `<div class="banner">${esc(e.message)}</div>`;
    return;
  }
  const attrs = (p.attributes||[]).sort((a,b)=>b.evidence_count-a.evidence_count);
  const corroborated = attrs.some(a=>a.evidence_count>1);
  const disputed = (conf.conflicts||[]).filter(c=>c.status==="active").length;

  $("#page").innerHTML = `
    ${p.merged_into?`<div class="banner info">This record was merged; showing the canonical profile.</div>`:""}
    <div class="phead">
      <div style="flex:1; min-width:240px">
        <h1>${esc(p.canonical_name||"Unnamed")}</h1>
        <div class="role">${esc([p.current_role,p.current_organization,p.location].filter(Boolean).join(" · "))||"&nbsp;"}</div>
        ${brandLinks(p.profile_urls, BRANDS.length)}
        <div class="badges">
          ${corroborated?`<span class="badge ok">Corroborated</span>`:""}
          ${disputed?`<span class="badge warn">${disputed} disputed</span>`:""}
          <span class="badge">${prov.sources.length} source${prov.sources.length===1?"":"s"}</span>
          ${p.country?`<span class="badge">${esc(p.country)}</span>`:""}
        </div>
        ${p.aliases?.length?`<div class="idline aka" title="${esc(p.aliases.join(" · "))}">also ${esc(p.aliases.slice(0,5).join(" · "))}${p.aliases.length>5?` +${p.aliases.length-5} more`:""}</div>`:""}
        <div class="idline">${esc(p.id)} · updated ${esc((p.updated_at||"").slice(0,10))}</div>
      </div>
    </div>

    <div class="grid2">
      <section class="block"><h2>Attributes <span class="n">${attrs.length}</span></h2>
        <div class="card"><div class="inner"><table class="data">
          <tr><th>Type</th><th>Value</th><th class="num">Sources</th></tr>
          ${attrs.slice(0,40).map(a=>`<tr>
            <td class="muted">${esc(a.attribute_type)}</td>
            <td>${esc(a.value)}<div class="idline">${a.sources.map(esc).join(", ")}</div></td>
            <td class="num">${a.evidence_count}</td></tr>`).join("")
            || `<tr><td class="muted">No attributes yet</td></tr>`}
        </table></div></div></section>

      <section class="block"><h2>Affiliations <span class="n">${orgs.affiliations.length}</span></h2>
        <div class="card"><div class="inner"><table class="data">
          <tr><th>Organization</th><th>Role</th><th>Period</th></tr>
          ${orgs.affiliations.map(a=>`<tr>
            <td>${esc(a.organization)}<div class="idline">${esc(a.relation)}</div></td>
            <td>${esc(a.role||"—")}</td>
            <td class="muted">${esc([a.start_date,a.end_date].filter(Boolean).join("–")||(a.is_current?"current":"—"))}</td>
          </tr>`).join("") || `<tr><td class="muted">None recorded</td></tr>`}
        </table></div></div></section>
    </div>

    ${disputed?`<section class="block"><h2>Disputed facts <span class="n">${conf.conflicts.length}</span></h2>
      ${conf.conflicts.map(c=>`<div class="conflict">
        <div class="ct">${esc(c.attribute)}</div>
        <div class="vs">
          <div class="side"><b>${esc(c.side_a.value)}</b><span>per ${esc(c.side_a.source||"unknown")}</span></div>
          <div class="mid">vs</div>
          <div class="side"><b>${esc(c.side_b.value)}</b><span>per ${esc(c.side_b.source||"unknown")}</span></div>
        </div></div>`).join("")}</section>`:""}

    <section class="block"><h2>Links &amp; documents</h2>
      <div class="card"><div class="inner">
        ${docs.cvs.length?`<table class="data">
          <tr><th>CV / résumé</th><th>Found on</th></tr>
          ${docs.cvs.map(c=>`<tr>
            <td><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.url)}</a></td>
            <td class="muted">${esc(c.evidence||c.found_on||"")}</td></tr>`).join("")}</table>`
          : `<p class="muted" style="font-size:13px">No published CV found. Seekr only links documents a person or their institution publishes.</p>`}
        ${docs.profiles.length?`<table class="data" style="margin-top:12px">
          <tr><th>Profile</th><th>Type</th></tr>
          ${docs.profiles.map(pf=>`<tr>
            <td><a href="${esc(pf.url)}" target="_blank" rel="noopener">${esc(pf.url)}</a></td>
            <td class="muted">${esc(pf.kind)}</td></tr>`).join("")}</table>`:""}
      </div></div></section>

    ${pubs.publications.length?`<section class="block"><h2>Publications <span class="n">${pubs.publications.length}</span></h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Title</th><th>Venue</th><th>Year</th><th class="num">Citations</th></tr></thead>
        <tbody>${pubs.publications.slice(0,30).map(w=>`<tr>
          <td>${w.url?`<a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.title)}</a>`:esc(w.title)}</td>
          <td class="muted">${esc(w.venue||"—")}</td>
          <td class="muted">${esc((w.published_date||"").slice(0,4))}</td>
          <td class="num">${w.citations??"—"}</td></tr>`).join("")}</tbody>
      </table></div></div></section>`:""}

    ${projs.projects.length?`<section class="block"><h2>Projects <span class="n">${projs.projects.length}</span></h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Name</th><th>Tech</th><th>Activity</th><th>Last active</th></tr></thead>
        <tbody>${projs.projects.map(x=>`<tr>
          <td>${x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.name)}</a>`:esc(x.name)}</td>
          <td class="muted">${(x.technologies||[]).map(esc).join(", ")||"—"}</td>
          <td class="muted">${esc(Object.entries(x.activity||{}).map(([k,v])=>k+" "+v).join(" · "))||"—"}</td>
          <td class="muted">${esc(x.last_active_at||"—")}</td></tr>`).join("")}</tbody>
      </table></div></div></section>`:""}

    ${graph.edges.length?`<section class="block"><h2>Network <span class="n">${graph.nodes.length-1}</span></h2>
      <div class="card"><div class="net" id="net"><svg id="netsvg"></svg>
        <span class="legend">■ organization · ● co-author</span></div></div></section>`:""}

    <section class="block"><h2>Provenance <span class="n">${prov.sources.length}</span></h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Source</th><th>Record</th><th>Matched by</th><th>Review</th><th>Seen</th></tr></thead>
        <tbody>${prov.sources.map(s=>`<tr>
          <td>${sourcePill(s.source, s.url)}</td>
          <td>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.external_id)}</a>`:esc(s.external_id)}</td>
          <td class="muted">${esc(s.match_signals?.reason||s.match_method)}</td>
          <td>${s.review_state==="approved"?`<span class="vstate corroborated">approved</span>`
              :`<span class="vstate unverified">${esc(s.review_state||"unreviewed")}</span>`}</td>
          <td class="muted">${esc((s.last_observed||"").slice(0,10))}</td></tr>`).join("")}</tbody>
      </table></div></div></section>`;
  if(graph.edges.length) drawNetwork(graph, p.id);
}

/* radial layout with label-box separation; no external library */
const MAX_LABEL = 20;
function drawNetwork(graph, selfId){
  const svg = $("#netsvg"); if(!svg) return;
  const others = graph.nodes.filter(n=>n.id!==selfId);
  const rings = Math.max(1, Math.ceil(others.length/9));
  const W = 900, H = Math.min(700, 280 + rings*115), cx = W/2, cy = H/2;
  const label = (s)=> (s||"").length>MAX_LABEL ? s.slice(0,MAX_LABEL-1)+"…" : (s||"");
  const halfW = (s)=> Math.max(24, label(s).length*3.1 + 8);
  const nodes = graph.nodes.map(n=>{
    if(n.id===selfId) return {...n, x:cx, y:cy, fixed:true};
    const k = others.indexOf(n), ring = k % rings;
    const a = (k/Math.max(1,others.length))*Math.PI*2 - Math.PI/2;
    const base = n.type==="organization" ? 92 : 148 + ring*112;
    return {...n, x:cx+Math.cos(a)*base*(W/H)*0.78, y:cy+Math.sin(a)*base*0.72};
  });
  for(let pass=0; pass<160; pass++){
    for(const a of nodes){
      if(a.fixed) continue;
      for(const b of nodes){
        if(a===b) continue;
        const needX = halfW(a.label)+halfW(b.label)+10, needY = 30;
        const dx=a.x-b.x, dy=a.y-b.y;
        if(Math.abs(dx)<needX && Math.abs(dy)<needY){
          if(Math.abs(dx)/needX > Math.abs(dy)/needY) a.x += (needX-Math.abs(dx))*0.22*(dx<0?-1:1);
          else a.y += (needY-Math.abs(dy))*0.5*(dy<0?-1:1);
        }
      }
      a.x = Math.max(halfW(a.label)+6, Math.min(W-halfW(a.label)-6, a.x));
      a.y = Math.max(24, Math.min(H-14, a.y));
    }
  }
  const byId = Object.fromEntries(nodes.map(n=>[n.id,n]));
  const edges = graph.edges.map(e=>{
    const a=byId[e.from], b=byId[e.to]; if(!a||!b) return "";
    const t = e.type==="coauthor" ? `${e.shared_publications} shared publication(s)` : esc(e.type);
    return `<line class="edge${e.type==="coauthor"?"":" org"}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${t}</title></line>`;
  }).join("");
  const marks = nodes.map(n=>{
    const text = esc(label(n.label)), dy = n.y<cy ? -11 : 17;
    if(n.type==="organization")
      return `<g><rect class="n-org" x="${n.x-5}" y="${n.y-5}" width="10" height="10" rx="2"><title>${esc(n.label)}</title></rect>
        <text x="${n.x}" y="${n.y+dy}" text-anchor="middle">${text}</text></g>`;
    const cls = n.id===selfId ? "n-person n-self" : "n-person";
    const click = n.id===selfId ? "" : ` onclick="location.hash='#/person/${n.id}'" style="cursor:pointer"`;
    return `<g${click}><circle class="${cls}" cx="${n.x}" cy="${n.y}" r="${n.id===selfId?8:5.5}"><title>${esc(n.label)}</title></circle>
      <text x="${n.x}" y="${n.y+dy}" text-anchor="middle">${text}</text></g>`;
  }).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = edges + marks;
}

/* ---------------- review ---------------- */
async function renderReview(){
  shell("#/review", `<h1 class="title">Review queue</h1>
    <div class="sub">Merges Seekr was not confident enough to make on its own, and the good/bad votes from search. Votes are stored, not used to reorder results.</div>`,
    (setWorking(true), `<div class="loading">Loading queue…</div>`));
  let r, fb;
  try{
    [r, fb] = await Promise.all([
      api("/v1/review/merges"),
      api("/v1/feedback?limit=200").catch(()=>({feedback:[]})),
    ]);
  }
  catch(e){ if(e.message!=="unauthorized") $("#page").innerHTML = `<div class="banner">${esc(e.message)}</div>`; return; }
  const dup = r.possible_duplicates||[], fuz = r.fuzzy_merges||[];
  const votes = fb.feedback||[];

  $("#page").innerHTML = `
    <section class="block"><h2>Match votes <span class="n">${votes.length}</span></h2>
      ${votes.length ? `<div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Person</th><th>Query</th><th>Vote</th><th>When</th></tr></thead>
        <tbody>${votes.slice().reverse().map(f=>`<tr onclick="location.hash='#/person/${f.person_id}'">
          <td class="nm">${esc(f.canonical_name||f.person_id)}</td>
          <td class="sk">${esc(f.query||"")||'<span class="muted">—</span>'}</td>
          <td>${f.verdict==="good"?'<span class="badge ok">good match</span>':'<span class="badge warn">bad match</span>'}</td>
          <td class="muted">${esc(String(f.created_at||"").slice(0,10))}</td>
        </tr>`).join("")}</tbody></table></div></div>`
      : emptyState("No votes yet", "Thumbs up or down on a search result to record whether it matched the query.")}
    </section>
    <section class="block"><h2>Possible duplicates <span class="n">${dup.length}</span></h2>
      ${dup.length ? dup.map(d=>`<div class="conflict">
        <div class="vs">
          <div class="side"><b><a href="#/person/${d.person_id}">${esc(d.person_name)}</a></b></div>
          <div class="mid">same person?</div>
          <div class="side"><b><a href="#/person/${d.duplicate_person_id}">${esc(d.duplicate_person_name)}</a></b></div>
        </div>
        <div class="idline">${esc(d.signals?.reason||("score "+d.score))}</div>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn primary sm" onclick="act('/v1/review/duplicates/${d.candidate_id}/merge')">Merge</button>
          <button class="btn danger sm" onclick="act('/v1/review/duplicates/${d.candidate_id}/reject')">Different people</button>
        </div></div>`).join("")
      : emptyState("Nothing to review", "No duplicate pairs are waiting.")}
    </section>
    <section class="block"><h2>Fuzzy merges awaiting confirmation <span class="n">${fuz.length}</span></h2>
      ${fuz.length ? fuz.map(f=>`<div class="conflict">
        <div><b><a href="#/person/${f.person_id}">${esc(f.person_name)}</a></b>
          <span class="muted">← ${esc(f.source)}:${esc(f.external_id)} (${esc(f.record_name||"")})</span></div>
        <div class="idline">${esc(f.signals?.reason||f.match_method)}</div>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn primary sm" onclick="act('/v1/review/merges/${f.link_id}/approve')">Approve</button>
          <button class="btn danger sm" onclick="act('/v1/review/merges/${f.link_id}/split')">Split apart</button>
        </div></div>`).join("")
      : emptyState("All confirmed", "No fuzzy merges are waiting for a decision.")}
    </section>`;
}
async function act(path){
  try{ await api(path,{method:"POST"}); renderReview(); }
  catch(e){ alert(e.message); }
}

/* ---------------- sources ---------------- */
async function renderSources(){
  shell("#/sources", `<h1 class="title">Sources</h1>
    <div class="sub">Where the data comes from, and whether ingestion is healthy.</div>`,
    `<div class="loading"><div class="spinner"></div>Loading…</div>`);
  let facets, health, hooks;
  try{
    [facets, health, hooks] = await Promise.all([
      api("/v1/facets?field=source"), api("/v1/health/sources"),
      api("/v1/webhooks/health").catch(()=>null),
    ]);
  }catch(e){ if(e.message!=="unauthorized") $("#page").innerHTML = `<div class="banner">${esc(e.message)}</div>`; return; }

  const runs = {};
  (health.sources||[]).forEach(s=>{
    runs[s.source] = runs[s.source] || {ok:0, error:0, last:null};
    runs[s.source][s.status==="ok"?"ok":"error"] += s.runs;
    if(s.last_finished_at) runs[s.source].last = s.last_finished_at;
  });

  $("#page").innerHTML = `
    <section class="block"><h2>Coverage</h2>
      <div class="card"><div class="tablewrap"><table class="list">
        <thead><tr><th>Source</th><th class="num">People</th><th class="num">Runs OK</th><th class="num">Failed</th><th>Last run</th></tr></thead>
        <tbody>${facets.values.map(v=>{
          const r = runs[v.value]||{ok:0,error:0,last:null};
          return `<tr><td class="nm">${esc(v.value)}</td>
            <td class="num">${fmt(v.people)}</td>
            <td class="num">${fmt(r.ok)}</td>
            <td class="num">${r.error?`<span style="color:var(--danger)">${fmt(r.error)}</span>`:"0"}</td>
            <td class="muted">${esc((r.last||"").slice(0,16).replace("T"," "))||"—"}</td></tr>`;
        }).join("")}</tbody>
      </table></div></div></section>
    ${hooks?`<section class="block"><h2>Webhook delivery</h2>
      <div class="card"><div class="inner"><table class="data">
        <tr><td>Active subscriptions</td><td class="num">${fmt(hooks.active_subscriptions)}</td></tr>
        <tr><td>Pending</td><td class="num">${hooks.pending?`<span style="color:var(--warn)">${fmt(hooks.pending)}</span>`:"0"}</td></tr>
        <tr><td>Delivered</td><td class="num">${fmt(hooks.delivered)}</td></tr>
        <tr><td>Failed</td><td class="num">${hooks.failed?`<span style="color:var(--danger)">${fmt(hooks.failed)}</span>`:"0"}</td></tr>
      </table>${hooks.pending?`<p class="muted" style="font-size:12.5px;margin-top:10px">Deliveries only send when <code>deliver-webhooks</code> runs.</p>`:""}
      </div></div></section>`:""}`;
}

/* ---------------- router ---------------- */
async function route(){
  if(!token()) return renderGate();
  const h = location.hash || "#/search";
  try{
    if(h.startsWith("#/person/")) await renderPerson(h.split("/")[2]);
    else if(h==="#/shortlists") await renderShortlists();
    else if(h==="#/review") await renderReview();
    else if(h==="#/sources") await renderSources();
    else await renderSearch();
  }catch(e){
    if(e.message!=="unauthorized"){
      const page = $("#page");
      if(page) page.innerHTML = `<div class="banner">${esc(e.message)}</div>`;
    }
  }finally{
    // whatever happened, the page is no longer waiting on it
    setWorking(false);
  }
}
window.addEventListener("hashchange", route);
route();
