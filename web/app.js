/* 本地 AI 对话与调试界面 —— 原生 JS，无构建、无 CDN、无外部依赖 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var app = $("app"), streamEl = $("stream");
  var inner = null;

  var state = {
    sessions: [],
    current: null,
    messages: [],
    models: [],
    params: {},
    streaming: false,
    abort: null,
    health: null,
    selectMode: false,
    selected: {},
    kind: "all",            // 左侧筛选：all / text / image / video
    kindOf: {},             // 模型 id → 类型，用于给会话打标签
    mode: "chat",           // chat | generate（图片/视频走独立工作区）
    generating: false,
    genAbort: null,
    generated: [],          // 当前分类下的生成产物
    defaults: null,         // 服务端持久化的默认参数，启动时拉取
    desktop: false          // 是否运行在桌面壳里（决定标题栏与几个能力走哪条路）
  };

  var DEFAULTS = {
    model: "", ctx_max: 8192, temperature: 0.7, top_p: 0.9, max_tokens: 1024,
    system_prompt: "", compact_enabled: true, trigger_ratio: 0.75,
    target_ratio: 0.45, keep_recent_turns: 4, enable_thinking: false
  };

  /* ============================== 工具 ============================== */

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 与服务端 tokens.py 同口径的粗估，仅用于打开会话时先画出进度条
  var CJK = /[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]/g;
  function estTokens(text) {
    if (!text) return 0;
    var cjk = (String(text).match(CJK) || []).length;
    return Math.ceil(cjk * 1.05 + (text.length - cjk) / 3.5) + 1;
  }

  function fmtNum(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function relTime(ts) {
    if (!ts) return "";
    var d = Date.now() - ts;
    if (d < 60000) return "刚刚";
    if (d < 3600000) return Math.floor(d / 60000) + " 分钟前";
    if (d < 86400000) return Math.floor(d / 3600000) + " 小时前";
    return Math.floor(d / 86400000) + " 天前";
  }

  /* ============================== Markdown ============================== */

  var KW = {
    python: "def class return if elif else for while in not and or import from as with try except finally raise yield lambda None True False self print".split(" "),
    js: "function var let const return if else for while of in new class this async await try catch finally throw typeof instanceof null undefined true false import export default".split(" "),
    javascript: "function var let const return if else for while of in new class this async await try catch finally throw typeof instanceof null undefined true false import export default".split(" "),
    sql: "select from where insert into values update set delete create table index primary key references not null default order by group having limit join left right inner on as and or".split(" "),
    bash: "cd ls cat echo export source if then else fi for do done function".split(" "),
    sh: "cd ls cat echo export source if then else fi for do done function".split(" "),
    json: ["true", "false", "null"],
    go: "func package import var const type struct interface return if else for range go defer chan map string int error nil".split(" "),
    rust: "fn let mut pub use mod struct enum impl match if else for while return self None Some Ok Err".split(" ")
  };

  function highlight(code, lang) {
    var kws = KW[String(lang || "").toLowerCase()];
    if (!kws || !kws.length) return code;
    var kwRe = new RegExp("\\b(" + kws.join("|") + ")\\b", "g");
    var tok = /(&quot;[\s\S]*?&quot;|&#39;[\s\S]*?&#39;|`[^`]*`)|(\/\/[^\n]*|#[^\n]*)/g;
    var out = "", last = 0, m;
    while ((m = tok.exec(code)) !== null) {
      out += code.slice(last, m.index).replace(kwRe, '<span class="k">$1</span>');
      out += '<span class="' + (m[1] ? "s" : "c") + '">' + m[0] + "</span>";
      last = m.index + m[0].length;
    }
    return out + code.slice(last).replace(kwRe, '<span class="k">$1</span>');
  }

  function inlineMd(s) {
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
    return s;
  }

  function textBlock(txt) {
    var lines = txt.split("\n"), out = [], buf = [], list = null;
    function flushP() {
      if (buf.length) {
        out.push("<p>" + inlineMd(esc(buf.join("\n"))).replace(/\n/g, "<br>") + "</p>");
        buf = [];
      }
    }
    function flushL() {
      if (list) {
        out.push("<" + list.tag + ">" + list.items.map(function (i) {
          return "<li>" + inlineMd(esc(i)) + "</li>";
        }).join("") + "</" + list.tag + ">");
        list = null;
      }
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, ""), m;
      if (!line.trim()) { flushP(); flushL(); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flushP(); flushL();
        var lv = m[1].length;
        out.push("<h" + lv + ">" + inlineMd(esc(m[2])) + "</h" + lv + ">");
        continue;
      }
      if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { flushP(); flushL(); out.push("<hr>"); continue; }
      if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
        flushP();
        if (!list || list.tag !== "ul") { flushL(); list = { tag: "ul", items: [] }; }
        list.items.push(m[1]); continue;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushP();
        if (!list || list.tag !== "ol") { flushL(); list = { tag: "ol", items: [] }; }
        list.items.push(m[1]); continue;
      }
      if ((m = line.match(/^&gt;\s?(.*)$/)) || (m = line.match(/^>\s?(.*)$/))) {
        flushP(); flushL();
        out.push("<blockquote>" + inlineMd(esc(m[1])) + "</blockquote>");
        continue;
      }
      buf.push(line);
    }
    flushP(); flushL();
    return out.join("");
  }

  function mdToHtml(src) {
    var parts = [], last = 0, m;
    var fence = /```([a-zA-Z0-9_+-]*)[ \t]*\n?([\s\S]*?)```/g;
    while ((m = fence.exec(src)) !== null) {
      parts.push({ t: "text", v: src.slice(last, m.index) });
      parts.push({ t: "code", lang: m[1] || "text", v: m[2].replace(/\n$/, "") });
      last = m.index + m[0].length;
    }
    parts.push({ t: "text", v: src.slice(last) });
    return parts.map(function (p) {
      if (p.t === "code") {
        return '<div class="code"><span class="lang">' + esc(p.lang) + "</span>" +
          '<button class="cp" type="button">复制</button>' +
          "<pre><code>" + highlight(esc(p.v), p.lang) + "</code></pre></div>";
      }
      return textBlock(p.v);
    }).join("");
  }

  function decorateCode(root) {
    Array.prototype.forEach.call(root.querySelectorAll(".code .cp"), function (btn) {
      btn.addEventListener("click", function () {
        var code = btn.parentNode.querySelector("code");
        if (!code) return;
        var text = code.textContent;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            btn.textContent = "已复制";
            setTimeout(function () { btn.textContent = "复制"; }, 1200);
          });
        }
      });
    });
  }

  /* ============================== 网络 ============================== */

  function api(path, opt) {
    opt = opt || {};
    var init = { method: opt.method || "GET", headers: {} };
    if (opt.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opt.body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var msg = (data && data.error && data.error.message) || ("HTTP " + res.status);
          var code = (data && data.error && data.error.code) || "HTTP_" + res.status;
          var err = new Error(msg);
          err.code = code;
          throw err;
        }
        return data;
      });
    });
  }

  async function* readSSE(res) {
    var reader = res.body.getReader();
    var dec = new TextDecoder();
    var buf = "";
    while (true) {
      var r = await reader.read();
      if (r.done) break;
      buf += dec.decode(r.value, { stream: true });
      var idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        var raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        var ev = "message", data = "";
        raw.split("\n").forEach(function (line) {
          if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
          else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
        });
        if (data) {
          try { yield { type: ev, data: JSON.parse(data) }; }
          catch (e) { /* 忽略坏帧 */ }
        }
      }
    }
  }

  /* ============================== 渲染 ============================== */

  function ensureInner() {
    if (!inner) {
      streamEl.innerHTML = "";
      inner = document.createElement("div");
      inner.className = "stream-inner";
      streamEl.appendChild(inner);
    }
    return inner;
  }

  function nearBottom() {
    return streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 120;
  }

  function scrollDown(force) {
    if (force || nearBottom()) {
      streamEl.scrollTop = streamEl.scrollHeight;
    }
  }

  function addMessageEl(role) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    wrap.innerHTML = '<div class="who">' + (role === "user" ? "我" : "助手") + '</div><div class="body"></div>';
    ensureInner().appendChild(wrap);
    return wrap.querySelector(".body");
  }

  function fillMessage(bodyEl, text) {
    bodyEl.classList.remove("streaming");
    bodyEl.innerHTML = mdToHtml(text || "");
    decorateCode(bodyEl);
  }

  function metricsEl(data) {
    var bits = [];
    if (data.engine_ms > 500) bits.push("加载 " + (data.engine_ms / 1000).toFixed(1) + "s");
    if (data.ttft_ms != null) bits.push("首字 " + data.ttft_ms + " ms");
    if (data.tps) bits.push(data.tps + " tok/s");
    if (data.completion_tokens != null) bits.push(data.completion_tokens + " tokens");
    if (data.prompt_tokens != null) bits.push("上下文 " + data.prompt_tokens);
    var el = document.createElement("div");
    el.className = "meta-line";
    el.textContent = bits.join("  ·  ");
    return el;
  }

  function errEl(msg, code) {
    var el = document.createElement("div");
    el.className = "errtip";
    el.textContent = (code ? "[" + code + "] " : "") + msg;
    return el;
  }

  function compactNote(info, before) {
    var el = document.createElement("div");
    el.className = "cbar-note";
    var modeText = {
      llm: "模型摘要",
      extractive: "本地抽取式（摘要请求失败，已降级）",
      trim: "滑窗丢弃"
    }[info.mode] || info.mode || "";
    var bits = [];
    if (info.compacted) {
      bits.push("已压缩 " + info.turns + " 轮历史 → 摘要 " + fmtNum(info.summary_tokens) + " tokens");
    }
    if (info.trimmed) bits.push("丢弃 " + info.trimmed + " 轮");
    bits.push("占用 " + fmtNum(info.before) + " → " + fmtNum(info.after));
    if (modeText) bits.push(modeText);
    el.innerHTML = '<span class="tag">自动压缩</span><span>' + esc(bits.join("　·　")) + "</span>";
    if (info.summary) {
      var btn = document.createElement("button");
      btn.className = "expand";
      btn.type = "button";
      btn.textContent = "查看摘要";
      var sum = document.createElement("div");
      sum.className = "sum";
      sum.textContent = info.summary;
      btn.addEventListener("click", function () {
        el.classList.toggle("open");
        btn.textContent = el.classList.contains("open") ? "收起摘要" : "查看摘要";
      });
      el.appendChild(btn);
      el.appendChild(sum);
    }
    return el;
  }

  function renderGauge(used, max) {
    max = max || state.params.ctx_max || 8192;
    var pct = max ? Math.min(1, used / max) : 0;
    var fill = $("gaugeFill");
    fill.style.width = (pct * 100).toFixed(1) + "%";
    fill.className = pct > 0.85 ? "danger" : (pct > 0.6 ? "warn" : "");
    $("gaugeLabel").textContent = fmtNum(used) + " / " + fmtNum(max);
    $("gaugeLabel").title = "上下文占用 " + Math.round(pct * 100) + "%";
  }

  function renderSessions() {
    var box = $("slist");
    box.innerHTML = "";
    var list = state.sessions.filter(function (s) {
      if (state.kind === "all") return true;
      return (state.kindOf[s.model] || "text") === state.kind;
    });
    if (!list.length) {
      box.innerHTML = '<div style="padding:10px 9px;font-size:12px;color:#98a0aa">' +
        (state.kind === "all" ? "还没有会话" : "该分类下还没有会话") + "</div>";
      return;
    }
    list.forEach(function (s) {
      var el = document.createElement("div");
      var picked = state.selectMode && !!state.selected[s.id];
      el.className = "sitem" + (state.current && state.current.id === s.id ? " active" : "") +
        (state.selectMode ? " selecting" : "") + (picked ? " picked" : "");
      var compacted = s.summary && s.summary.covered_turns ? "已压缩" : "";
      var kk = state.kindOf[s.model] || "text";
      var badge = kk === "image" ? '<span class="kb">图</span>'
        : (kk === "video" ? '<span class="kb">视</span>' : "");
      el.innerHTML = (state.selectMode
        ? '<input type="checkbox" class="pick"' + (picked ? " checked" : "") + ">"
        : "") +
        '<div class="st">' + esc(s.title || "未命名") + "</div>" +
        '<div class="sm">' + badge + "<span>" + (s.turn_count || 0) + " 轮</span><span>" +
        relTime(s.updated_at) + "</span>" +
        (compacted ? "<span>" + compacted + "</span>" : "") + "</div>" +
        (state.selectMode ? "" : '<button class="del" title="删除会话">×</button>');

      el.querySelector(".st").addEventListener("dblclick", function (ev) {
        ev.stopPropagation();
        if (state.selectMode) return;
        startRename(el, s);
      });
      el.addEventListener("click", function (ev) {
        if (ev.target.classList.contains("del")) return;
        if (state.selectMode) {
          togglePick(s.id, !state.selected[s.id]);
          return;
        }
        openSession(s.id);
      });
      var del = el.querySelector(".del");
      if (del) {
        del.addEventListener("click", function (ev) {
          ev.stopPropagation();
          removeSession(s);
        });
      }
      box.appendChild(el);
    });
  }

  /* ---------------- 类型筛选 ---------------- */

  function sessionKind(s) {
    return state.kindOf[s.model] || "text";
  }

  function updateKindCounts() {
    var c = { all: state.sessions.length, text: 0, image: 0, video: 0 };
    state.sessions.forEach(function (s) {
      var k = sessionKind(s);
      c[k] = (c[k] || 0) + 1;
    });
    $("kAll").textContent = c.all;
    $("kText").textContent = c.text;
    $("kImage").textContent = c.image;
    $("kVideo").textContent = c.video;
    Array.prototype.forEach.call(document.querySelectorAll("#kindBar .kind"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-kind") === state.kind);
    });
  }

  function setKind(k) {
    state.kind = k;
    updateKindCounts();
    renderSessions();
    applyMode();
  }

  /* ---------------- 对话 / 生成 两种工作区 ---------------- */

  function genKind() {
    return (state.kind === "image" || state.kind === "video") ? state.kind : null;
  }

  function applyMode() {
    var gk = genKind();
    var gen = !!gk;
    state.mode = gen ? "generate" : "chat";
    document.body.classList.toggle("gen-mode", gen);

    streamEl.hidden = gen;
    $("genarea").hidden = !gen;
    if (!gen) showGenDir("");
    $("textParams").hidden = gen;
    $("genParams").hidden = !gen;
    $("textNote").hidden = gen;
    $("videoOnly").hidden = gk !== "video";

    if (gen) {
      $("sessTitle").textContent = gk === "video" ? "视频生成" : "图片生成";
      $("input").placeholder = gk === "video"
        ? "描述画面与动作，例如：一只红狐在雪地里行走，电影感"
        : "描述你想要的画面，例如：雨夜的霓虹街道，赛博朋克";
      $("btnSend").textContent = "生成";
      $("hint").textContent = "Enter 生成 · Shift+Enter 换行";
      fillModelsForKind(gk);
      loadGenerated();
    } else {
      $("input").placeholder = "输入消息，Enter 发送 / Shift+Enter 换行";
      $("btnSend").textContent = "发送";
      $("hint").textContent = "Enter 发送 · Shift+Enter 换行";
      fillModels(state.models);
      if (!state.current || !state.messages.length) showEmpty();
    }
  }

  // 生成模式下下拉只列该类型的模型，避免"选了文本模型却想生成图片"
  function fillModelsForKind(kind) {
    var sel = $("pModel");
    var list = state.models.filter(function (m) { return (m.kind || "text") === kind; });
    var cur = sel.value;
    sel.innerHTML = "";
    if (!list.length) {
      var o = document.createElement("option");
      o.value = "";
      o.textContent = "（models/" + kind + " 下没有可用模型）";
      sel.appendChild(o);
      sel.disabled = true;
      updateModelHint();
      return;
    }
    sel.disabled = false;
    list.forEach(function (m) {
      var op = document.createElement("option");
      op.value = m.id;
      var bits = [m.id];
      if (m.size_gb) bits.push(m.size_gb.toFixed(1) + "GB");
      if (m.missing && m.missing.length) bits.push("缺 " + m.missing.join("/"));
      op.textContent = bits.join(" · ");
      sel.appendChild(op);
    });
    if (cur && list.some(function (m) { return m.id === cur; })) sel.value = cur;
    else sel.value = list[0].id;
    // 用模型自带的推荐参数预填，用户不用自己猜尺寸与步数
    prefillGenParams(list.filter(function (m) { return m.id === sel.value; })[0]);
    updateModelHint();
  }

  function prefillGenParams(m) {
    var p = (m && m.params) || {};
    if (p.width) $("gW").value = p.width;
    if (p.height) $("gH").value = p.height;
    if (p.steps) { $("gSteps").value = p.steps; $("vSteps").textContent = p.steps; }
    if (p.cfg_scale) { $("gCfg").value = p.cfg_scale; $("vCfg").textContent = Number(p.cfg_scale).toFixed(1); }
    if (p.frames) $("gFrames").value = p.frames;
    if (p.fps) $("gFps").value = p.fps;
    updateSizeHint();
  }

  function updateSizeHint() {
    var w = Number($("gW").value) || 0;
    var h = Number($("gH").value) || 0;
    var fps = Number($("gFps").value) || 16;
    var fr = Number($("gFrames").value) || 1;
    var bits = [];
    if (state.kind === "video") {
      bits.push(fr + " 帧 ≈ " + (fr / fps).toFixed(1) + " 秒");
      bits.push("帧数越多越吃显存，8GB 建议 ≤ 17 帧");
    } else {
      if (w * h > 1024 * 1024) bits.push("超过 1024² 在 8GB 上有 OOM 风险");
      else bits.push("边长会按 8 对齐");
    }
    $("sizeHint").textContent = bits.join(" · ");
  }

  function bindKindBar() {
    Array.prototype.forEach.call(document.querySelectorAll("#kindBar .kind"), function (b) {
      b.addEventListener("click", function () { setKind(b.getAttribute("data-kind")); });
    });
  }

  /* ---------------- 批量选择 ---------------- */

  function setSelectMode(on) {
    state.selectMode = !!on;
    if (!on) state.selected = {};
    $("batchBar").hidden = !state.selectMode;
    $("btnSelect").classList.toggle("on", state.selectMode);
    $("btnSelect").textContent = state.selectMode ? "选择中" : "选择";
    renderSessions();
    updateBatchBar();
  }

  function togglePick(id, on) {
    if (on) state.selected[id] = true;
    else delete state.selected[id];
    renderSessions();
    updateBatchBar();
  }

  function pickedIds() {
    return Object.keys(state.selected);
  }

  function updateBatchBar() {
    var n = pickedIds().length;
    var total = state.sessions.length;
    $("selCount").textContent = n ? ("已选 " + n + " / " + total) : "未选中";
    $("btnBatchDel").disabled = n === 0;
    $("btnBatchDel").textContent = n ? ("删除 " + n + " 个") : "删除";
    $("selAll").checked = total > 0 && n === total;
    $("selAll").indeterminate = n > 0 && n < total;
  }

  function batchDelete() {
    var ids = pickedIds();
    if (!ids.length) return;
    var names = state.sessions.filter(function (s) { return state.selected[s.id]; })
      .map(function (s) { return "「" + (s.title || "未命名") + "」"; });
    var preview = names.slice(0, 3).join("、") + (names.length > 3 ? " 等" : "");
    if (!confirm("删除 " + ids.length + " 个对话：" + preview + "？\n\n消息会一并删除，不可撤销。")) return;

    var btn = $("btnBatchDel");
    btn.disabled = true;
    btn.textContent = "删除中…";
    api("/api/sessions/batch-delete", { method: "POST", body: { ids: ids } })
      .then(function (d) {
        var gone = d.ids || [];
        // 当前打开的会话被删掉了，就退回空状态，别停在一个已不存在的 id 上
        if (state.current && gone.indexOf(state.current.id) >= 0) {
          state.current = null;
          state.messages = [];
          $("sessTitle").textContent = "新对话";
          showEmpty();
        }
        state.selected = {};
        setSelectMode(false);
        return loadSessions();
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = "删除";
        alert("批量删除失败：" + (e.message || e));
      });
  }

  function startRename(itemEl, s) {
    var st = itemEl.querySelector(".st");
    var input = document.createElement("input");
    input.value = s.title || "";
    input.style.cssText = "width:100%;border:1px solid #bcdcd3;border-radius:3px;padding:1px 4px;font-size:13px";
    st.replaceWith(input);
    input.focus();
    input.select();
    function commit() {
      var v = input.value.trim();
      if (v && v !== s.title) {
        api("/api/sessions/" + s.id, { method: "PATCH", body: { title: v } }).then(function () {
          s.title = v;
          if (state.current && state.current.id === s.id) $("sessTitle").textContent = v;
          renderSessions();
        });
      } else {
        renderSessions();
      }
    }
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { input.value = s.title; input.blur(); }
    });
  }

  function removeSession(s) {
    if (!confirm("删除会话「" + (s.title || "未命名") + "」？该操作不可撤销。")) return;
    api("/api/sessions/" + s.id, { method: "DELETE" }).then(function () {
      if (state.current && state.current.id === s.id) {
        state.current = null;
        state.messages = [];
        inner = null;
        showEmpty();
      }
      loadSessions();
    });
  }

  function showEmpty() {
    inner = null;
    if (state.kind === "image" || state.kind === "video") {
      var what = state.kind === "image" ? "图片" : "视频";
      streamEl.innerHTML = '<div class="empty" id="empty">' +
        "<h1>" + what + "模型</h1>" +
        "<p>分类已就位，但" + what + "生成引擎还没接入——现有的 llama.cpp 只能跑语言模型。" +
        "把 " + what + "权重放进 models/" + (state.kind === "image" ? "image" : "video") +
        "/ 并接入生成引擎后，这里会出现生成面板。</p>" +
        '<div class="chips">' +
        '<button class="chipbtn" data-kind-jump="text">先去用语言模型</button>' +
        "</div></div>";
      Array.prototype.forEach.call(streamEl.querySelectorAll("[data-kind-jump]"), function (b) {
        b.addEventListener("click", function () { setKind(b.getAttribute("data-kind-jump")); });
      });
      renderGauge(0, state.params.ctx_max);
      return;
    }
    streamEl.innerHTML = '<div class="empty" id="empty">' +
      "<h1>开始一段本地对话</h1>" +
      "<p>模型跑在你这台机器上，请求不出网卡。上下文超过阈值时历史会被自动压缩成摘要，原始消息仍然完整保留。</p>" +
      '<div class="chips">' +
      '<button class="chipbtn" data-p="用三句话解释 KV cache 是什么，以及它为什么吃显存">解释 KV cache</button>' +
      '<button class="chipbtn" data-p="帮我写一个 Python 函数，把 Markdown 表格转成 CSV，要求处理转义">写个转换函数</button>' +
      '<button class="chipbtn" data-p="这段对话如果继续下去，上下文会怎么增长？给一个估算方法">估算上下文</button>' +
      "</div></div>";
    bindChips();
    renderGauge(0, state.params.ctx_max);
  }

  function bindChips() {
    Array.prototype.forEach.call(document.querySelectorAll(".chipbtn"), function (b) {
      b.addEventListener("click", function () {
        $("input").value = b.getAttribute("data-p");
        autoGrow();
        $("input").focus();
      });
    });
  }

  function renderMessages() {
    inner = null;
    if (!state.messages.length) { showEmpty(); return; }
    ensureInner();
    state.messages.forEach(function (m) {
      var body = addMessageEl(m.role === "user" ? "user" : "assistant");
      if (m.role === "user") {
        body.textContent = m.content;
      } else {
        fillMessage(body, m.content);
        if (m.meta && (m.meta.ttft_ms != null || m.meta.tps)) {
          var line = metricsEl({
            ttft_ms: m.meta.ttft_ms, tps: m.meta.tps, engine_ms: m.meta.engine_ms,
            completion_tokens: m.meta.completion_tokens, prompt_tokens: m.meta.prompt_tokens
          });
          body.parentNode.appendChild(line);
        }
      }
    });
    // 摘要横幅
    if (state.current && state.current.summary && state.current.summary.covered_turns) {
      var note = document.createElement("div");
      note.className = "cbar-note";
      note.innerHTML = '<span class="tag">历史摘要</span><span>前 ' + state.current.summary.covered_turns +
        " 轮已压缩为 " + fmtNum(state.current.summary.tokens) + " tokens（" +
        (state.current.summary.mode === "llm" ? "模型摘要" : "抽取式") + "）</span>";
      var btn = document.createElement("button");
      btn.className = "expand"; btn.type = "button"; btn.textContent = "查看摘要";
      var sum = document.createElement("div");
      sum.className = "sum"; sum.textContent = state.current.summary.text || "";
      btn.addEventListener("click", function () {
        note.classList.toggle("open");
        btn.textContent = note.classList.contains("open") ? "收起摘要" : "查看摘要";
      });
      note.appendChild(btn); note.appendChild(sum);
      inner.insertBefore(note, inner.firstChild);
    }
    var used = state.messages.reduce(function (a, m) { return a + estTokens(m.content || ""); }, 0);
    renderGauge(used, state.params.ctx_max);
    scrollDown(true);
  }

  /* ============================== 会话 ============================== */

  function loadSessions() {
    return api("/api/sessions").then(function (d) {
      state.sessions = d.sessions || [];
      // 清掉已经不存在的选中项，否则「已选 N 个」会显示成删不掉的幽灵数字
      var exist = {};
      state.sessions.forEach(function (s) { exist[s.id] = true; });
      Object.keys(state.selected).forEach(function (id) {
        if (!exist[id]) delete state.selected[id];
      });
      renderSessions();
      updateKindCounts();
      if (state.selectMode) updateBatchBar();
    });
  }

  function applySessionParams() {
    var base = state.defaults || DEFAULTS;
    var p = Object.assign({}, base, state.current ? state.current.params : {});
    state.params = p;
    // 模型以会话为准（会话创建时绑定），参数里的 model 只对新会话有效
    var bound = state.current && state.current.model;
    $("pModel").value = bound || p.model || (state.models[0] && state.models[0].id) || "";
    $("pCtx").value = String(p.ctx_max || 8192);
    $("pTemp").value = p.temperature != null ? p.temperature : 0.7;
    $("pTopP").value = p.top_p != null ? p.top_p : 0.9;
    $("pMaxTok").value = p.max_tokens || 1024;
    $("pCompact").checked = p.compact_enabled !== false;
    $("pTrig").value = p.trigger_ratio != null ? p.trigger_ratio : 0.75;
    $("pTarget").value = p.target_ratio != null ? p.target_ratio : 0.45;
    $("pKeep").value = p.keep_recent_turns || 4;
    $("pSys").value = p.system_prompt || "";
    $("pThink").checked = p.enable_thinking === true;
    if (state.health && state.health.snapshot_level) {
      $("pSnap").value = state.health.snapshot_level;
    }
    setModelLock(!!(state.current && state.messages.length));
    syncParamLabels();
    updateModelHint();
    $("modelTag").textContent = bound || p.model || "默认模型";
  }

  // 会话已有消息 → 模型锁定。这是 Q2 的界面侧：看得到，也改不了。
  function setModelLock(locked) {
    var sel = $("pModel");
    sel.disabled = locked;
    $("modelLock").hidden = !locked;
    sel.title = locked ? "会话已绑定模型，对话途中不能更换；如需换模型请新建对话" : "";
  }

  function openSession(id) {
    return api("/api/sessions/" + id + "/messages").then(function (d) {
      state.current = d.session;
      state.messages = d.messages || [];
      $("sessTitle").textContent = d.session.title || "未命名";
      applySessionParams();
      renderMessages();
      renderSessions();
      if (window.innerWidth <= 900) app.classList.remove("side-open");
    });
  }

  function newChat(silent) {
    if (state.current && state.messages.length === 0) {
      if (!silent) showEmpty();
      setModelLock(false);
      return Promise.resolve();
    }
    var body = { model: $("pModel").value || undefined, params: collectParams() };
    return api("/api/sessions", { method: "POST", body: body }).then(function (d) {
      state.current = d.session;
      state.messages = [];
      $("sessTitle").textContent = d.session.title || "新对话";
      applySessionParams();
      showEmpty();
      if (!silent) $("input").focus();
      return loadSessions();
    });
  }

  /* ============================== 发送 ============================== */

  function setStreaming(on) {
    state.streaming = on;
    $("btnSend").hidden = on;
    $("btnStop").hidden = !on;
    $("btnSend").disabled = on;
    $("hint").textContent = on ? "生成中… Esc 停止" : "Enter 发送 · Shift+Enter 换行";
    $("btnCompact").disabled = on;
  }

  /* ---------------- 图片 / 视频生成 ---------------- */

  function setGenerating(on) {
    state.generating = on;
    $("btnSend").hidden = on;
    $("btnStop").hidden = !on;
    $("btnSend").disabled = on;
    $("hint").textContent = on ? "生成中… Esc 停止" : "Enter 生成 · Shift+Enter 换行";
    $("genIntro").hidden = on || $("genList").children.length > 0;
  }

  function genCollect() {
    return {
      model: $("pModel").value,
      prompt: $("input").value.trim(),
      negative: "",
      width: Number($("gW").value) || 512,
      height: Number($("gH").value) || 512,
      steps: Number($("gSteps").value) || 20,
      cfg_scale: Number($("gCfg").value) || 7,
      seed: $("gSeed").value === "" ? null : Number($("gSeed").value),
      frames: state.kind === "video" ? (Number($("gFrames").value) || 9) : 1,
      fps: Number($("gFps").value) || 16
    };
  }

  async function generateOnce() {
    if (state.generating) return;
    var prompt = $("input").value.trim();
    if (!prompt) { $("input").focus(); return; }
    var body = genCollect();
    if (!body.model) {
      genNotice("models/" + state.kind + " 下没有可用模型，先把权重文件放进去");
      return;
    }
    var entry = modelById(body.model);
    if (entry && entry.missing && entry.missing.length) {
      genNotice("模型缺少组件：" + entry.missing.join("、") + "，请补齐后再生成");
      return;
    }

    $("input").value = "";
    autoGrow();
    setGenerating(true);

    var card = genTaskCard(prompt, body);
    $("genList").insertBefore(card.el, $("genList").firstChild);
    state.genAbort = new AbortController();

    try {
      var res = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: state.genAbort.signal
      });
      if (!res.ok) {
        var txt = await res.text();
        card.fail({ message: "HTTP " + res.status + " " + txt.slice(0, 200) });
      } else {
        for await (var ev of readSSE(res)) {
          if (ev.type === "stage") card.stage(ev.data);
          else if (ev.type === "progress") card.progress(ev.data);
          else if (ev.type === "result") card.done(ev.data);
          else if (ev.type === "error") card.fail(ev.data);
        }
      }
    } catch (e) {
      if (e.name === "AbortError") card.cancelled();
      else card.fail({ message: e.message || String(e) });
    } finally {
      setGenerating(false);
      state.genAbort = null;
      loadGenerated();
    }
  }

  async function stopGenerate() {
    if (!state.generating) return;
    try {
      await fetch("/api/generate/stop", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: $("pModel").value, kind: state.kind })
      });
    } catch (e) { /* 忽略 */ }
    if (state.genAbort) state.genAbort.abort();
  }

  function genTaskCard(prompt, body) {
    var el = document.createElement("div");
    el.className = "gencard running";
    el.innerHTML =
      '<div class="gbody">' +
        '<div class="gstage"><span class="spin"></span><span class="gs-text">准备生成…</span></div>' +
        '<div class="gbar2"><i></i></div>' +
        '<div class="gsteps"><span class="gs-step">' + esc(body.width + "×" + body.height) +
          (state.kind === "video" ? " · " + body.frames + " 帧" : "") + " · " + body.steps + " 步</span>" +
          '<span class="gs-eta"></span></div>' +
        '<div class="gprompt" style="margin-top:8px">' + esc(prompt) + "</div>" +
      "</div>";
    var textEl = el.querySelector(".gs-text");
    var fillEl = el.querySelector(".gbar2 i");
    var stepEl = el.querySelector(".gs-step");
    var etaEl = el.querySelector(".gs-eta");

    return {
      el: el,
      stage: function (d) {
        if (!d) return;
        textEl.textContent = d.label || d.stage || "生成中…";
      },
      progress: function (d) {
        if (!d || !d.total) return;
        var pct = Math.max(2, Math.round(d.step / d.total * 100));
        fillEl.style.width = pct + "%";
        stepEl.textContent = "第 " + d.step + " / " + d.total + " 步" + (d.it ? " · " + d.it : "");
        etaEl.textContent = d.eta ? "剩余 " + d.eta : "";
      },
      done: function (r) {
        el.parentNode.replaceChild(genResultCard(r, prompt), el);
      },
      fail: function (e) {
        el.classList.remove("running");
        el.innerHTML = '<div class="gbody"><div class="gerr">' +
          esc((e && (e.message || e.code)) || "生成失败") + "</div>" +
          '<div class="gprompt" style="margin-top:7px">' + esc(prompt) + "</div></div>";
      },
      cancelled: function () {
        el.classList.remove("running");
        el.innerHTML = '<div class="gbody"><div class="gstage" style="color:var(--muted)">已停止</div>' +
          '<div class="gprompt" style="margin-top:7px">' + esc(prompt) + "</div></div>";
      }
    };
  }

  function genResultCard(r, prompt) {
    var el = document.createElement("div");
    el.className = "gencard";
    var isVideo = (r.kind === "video") || /\.(webm|mp4|avi)$/i.test(r.url || "");
    var media;
    if (isVideo) {
      media = '<video src="' + esc(r.url) + '" autoplay loop muted playsinline></video>';
    } else {
      media = '<img src="' + esc(r.url) + '" alt="" loading="lazy">';
    }
    var meta = [];
    meta.push(r.width + "×" + r.height);
    if (isVideo && r.frames) meta.push(r.frames + " 帧");
    if (r.req && r.req.steps) meta.push(r.req.steps + " 步");
    meta.push((r.elapsed_ms / 1000).toFixed(1) + " s");
    meta.push((r.size / 1024 / 1024).toFixed(2) + " MB");

    var warns = (r.warnings || []).map(function (w) {
      return '<div class="gwarn">' + esc(w) + "</div>";
    }).join("");

    el.innerHTML =
      '<div class="media">' + media + "</div>" +
      '<div class="gbody">' +
        '<div class="gprompt">' + esc(prompt || r.prompt || "") + "</div>" +
        '<div class="gmeta">' + meta.map(function (x) { return "<span>" + esc(x) + "</span>"; }).join("") + "</div>" +
        warns +
        '<div class="gacts">' +
          '<a href="' + esc(r.url) + '" download>下载</a>' +
          '<button class="gagain">按此参数再来一张</button>' +
        "</div>" +
      "</div>";
    var again = el.querySelector(".gagain");
    if (again) {
      again.addEventListener("click", function () {
        $("input").value = prompt || r.prompt || "";
        autoGrow();
        var req = r.req || {};
        if (r.model) $("pModel").value = r.model;
        if (req.width) $("gW").value = req.width;
        if (req.height) $("gH").value = req.height;
        if (req.steps) { $("gSteps").value = req.steps; $("vSteps").textContent = req.steps; }
        if (req.cfg_scale) { $("gCfg").value = req.cfg_scale; $("vCfg").textContent = Number(req.cfg_scale).toFixed(1); }
        if (req.frames) $("gFrames").value = req.frames;
        $("input").focus();
      });
    }
    return el;
  }

  function genNotice(msg) {
    var el = document.createElement("div");
    el.className = "gencard";
    el.innerHTML = '<div class="gbody"><div class="gerr">' + esc(msg) + "</div></div>";
    $("genList").insertBefore(el, $("genList").firstChild);
    $("genIntro").hidden = true;
  }

  function modelById(id) {
    for (var i = 0; i < state.models.length; i++) {
      if (state.models[i].id === id) return state.models[i];
    }
    return null;
  }

  /* 产物目录：界面上直接显示真实路径，并给一个「打开文件夹」。
     用户最常问的就是「生成的东西存哪了」——与其让他翻 data 目录，不如直接跳过去。 */
  var genDirPath = "";

  function showGenDir(path) {
    genDirPath = path || "";
    var bar = $("genBar");
    bar.hidden = !path;
    $("genPath").textContent = path || "";
    $("genPath").title = path || "";
  }

  function toast(msg) {
    // 借输入区左侧那行状态文字做提示，用完自动还原，不新增浮层
    var el = $("hint");
    if (!el) return;
    var keep = el.textContent;
    el.textContent = msg;
    setTimeout(function () { el.textContent = keep; }, 2600);
  }

  function openGenFolder() {
    if (!genDirPath) return;
    // 桌面窗口才有 pywebview 的原生 API；浏览器模式下退化为选中路径文本，
    // 让用户自己复制——至少路径是明确给出的。
    var apiObj = window.pywebview && window.pywebview.api;
    if (apiObj && apiObj.open_folder) {
      apiObj.open_folder(genKind()).then(function (r) {
        if (!r || !r.opened) toast("打不开目录：" + ((r && r.error) || "未知原因"));
      }).catch(function () { toast("打开文件夹失败"); });
      return;
    }
    copyText(genDirPath);
    toast("已复制路径：" + genDirPath);
  }

  function copyText(t) {
    try {
      var ta = document.createElement("textarea");
      ta.value = t;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e) { /* 复制失败就只提示路径 */ }
  }

  function loadGenerated() {
    var gk = genKind();
    if (!gk) return Promise.resolve();
    return api("/api/generated?limit=80").then(function (d) {
      var list = (d.generated || []).filter(function (x) { return x.kind === gk; });
      state.generated = list;
      showGenDir((d.dirs || {})[gk] || "");
      var box = $("genList");
      box.innerHTML = "";
      list.forEach(function (x) {
        var m = x.meta || {};
        box.appendChild(genResultCard({
          url: x.url, kind: x.kind, width: m.width || "?",
          height: m.height || "?", frames: m.frames,
          elapsed_ms: m.elapsed_ms || 0, size: x.size,
          warnings: m.warnings || [], req: m.req, prompt: m.prompt,
          model: m.model
        }, m.prompt || ""));
      });
      $("genIntro").hidden = list.length > 0 || state.generating;
    }).catch(function () { /* 静默：列表拉不到不影响生成 */ });
  }

  async function send() {
    if (state.mode === "generate") return generateOnce();
    var input = $("input");
    var content = input.value.trim();
    if (!content || state.streaming) return;

    if (!state.current) {
      await newChat(true);
    }
    if (!state.current) return;

    input.value = "";
    autoGrow();
    ensureInner();
    var ub = addMessageEl("user");
    ub.textContent = content;
    state.messages.push({ role: "user", content: content });
    scrollDown(true);

    var body = {
      session_id: state.current.id,
      content: content,
      params: Object.assign({}, state.params, collectParams())
    };
    state.params = body.params;

    var ab = addMessageEl("assistant");
    ab.classList.add("streaming");

    // 引擎加载阶段的状态行：首次对话要几十秒，不能白屏
    var stageEl = document.createElement("div");
    stageEl.className = "stage-line";
    ab.parentNode.insertBefore(stageEl, ab);

    // 思考过程：单独一块，默认折叠
    var thinkWrap = null, thinkBody = null, thinkFull = "";

    var cursor = document.createElement("span");
    cursor.className = "cursor";
    ab.appendChild(cursor);
    var textNode = document.createTextNode("");
    ab.insertBefore(textNode, cursor);

    var pending = "", thinkPending = "", raf = 0, full = "";
    function flush() {
      raf = 0;
      if (pending) { textNode.data += pending; pending = ""; }
      if (thinkPending && thinkBody) { thinkBody.data += thinkPending; thinkPending = ""; }
      scrollDown();
    }

    setStreaming(true);
    var controller = new AbortController();
    state.abort = controller;
    var usageData = null;
    var lastMeta = null;

    try {
      var res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal
      });
      if (!res.ok) {
        var errBody = await res.json().catch(function () { return {}; });
        throw new Error((errBody.error && errBody.error.message) || ("HTTP " + res.status));
      }

      for await (var ev of readSSE(res)) {
        if (ev.type === "stage") {
          if (ev.data.stage === "engine") {
            stageEl.textContent = "正在加载模型 " + (ev.data.model || "") +
              "（" + fmtNum(ev.data.ctx_max) + " 上下文，首次可能需要几十秒）…";
          } else {
            stageEl.remove();
          }
        } else if (ev.type === "think") {
          if (!thinkWrap) {
            thinkWrap = document.createElement("div");
            thinkWrap.className = "think";
            thinkWrap.innerHTML = '<span class="tag">思考过程</span><div class="tbody"></div>';
            ab.parentNode.insertBefore(thinkWrap, ab);
            thinkBody = document.createTextNode("");
            thinkWrap.querySelector(".tbody").appendChild(thinkBody);
          }
          thinkPending += ev.data.t || "";
          thinkFull += ev.data.t || "";
          if (!raf) raf = requestAnimationFrame(flush);
        } else if (ev.type === "compact") {
          var note = compactNote(ev.data, null);
          ab.parentNode.parentNode.insertBefore(note, ab.parentNode);
          scrollDown();
        } else if (ev.type === "meta") {
          lastMeta = ev.data;
          if (ev.data.session_id) state.current.id = ev.data.session_id;
          renderGauge(ev.data.ctx_used, ev.data.ctx_max);
          $("modelTag").textContent = ev.data.model || "";
        } else if (ev.type === "delta") {
          pending += ev.data.t || "";
          full += ev.data.t || "";
          if (!raf) raf = requestAnimationFrame(flush);
        } else if (ev.type === "usage") {
          usageData = ev.data;
        } else if (ev.type === "error") {
          if (stageEl.parentNode) stageEl.remove();
          ab.parentNode.appendChild(errEl(ev.data.message, ev.data.code));
        } else if (ev.type === "done") {
          /* 结束标记 */
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") {
        ab.parentNode.appendChild(errEl(e.message || String(e), e.code));
      }
    }
    if (stageEl.parentNode) stageEl.remove();
    if (raf) { cancelAnimationFrame(raf); flush(); }
    if (stageEl.parentNode) stageEl.remove();
    cursor.remove();
    ab.classList.remove("streaming");
    fillMessage(ab, full);
    if (usageData) ab.parentNode.appendChild(metricsEl(usageData));
    state.messages.push({ role: "assistant", content: full, meta: usageData || {},
      thinking: thinkFull });
    setStreaming(false);
    state.abort = null;
    setModelLock(true);
    scrollDown();
    loadSessions();
  }

  function stop() {
    if (!state.streaming) return;
    if (state.current) {
      api("/api/chat/stop", { method: "POST", body: { session_id: state.current.id } }).catch(function () {});
    }
    if (state.abort) state.abort.abort();
  }

  /* ============================== 参数 ============================== */

  function syncParamLabels() {
    $("vTemp").textContent = Number($("pTemp").value).toFixed(2);
    $("vTopP").textContent = Number($("pTopP").value).toFixed(2);
    $("vTrig").textContent = Math.round(Number($("pTrig").value) * 100) + "%";
    $("vTarget").textContent = Math.round(Number($("pTarget").value) * 100) + "%";
  }

  var saveTimer = 0;
  function collectParams() {
    return {
      model: $("pModel").value,
      ctx_max: Number($("pCtx").value),
      temperature: Number($("pTemp").value),
      top_p: Number($("pTopP").value),
      max_tokens: Number($("pMaxTok").value),
      compact_enabled: $("pCompact").checked,
      trigger_ratio: Number($("pTrig").value),
      target_ratio: Number($("pTarget").value),
      keep_recent_turns: Number($("pKeep").value),
      enable_thinking: $("pThink").checked,
      system_prompt: $("pSys").value
    };
  }

  function persistParams() {
    state.params = collectParams();
    $("modelTag").textContent = (state.current && state.current.model) || state.params.model || "";
    updateModelHint();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      // ① 会话自己的参数：只影响这一次对话
      if (state.current) {
        var body = { params: state.params };
        if (state.messages.length === 0 && $("pModel").value) body.model = $("pModel").value;
        api("/api/sessions/" + state.current.id, { method: "PATCH", body: body })
          .then(function (d) {
            if (d && d.session) {
              state.current.model = d.session.model;
              state.current.params = d.session.params;
              $("modelTag").textContent = d.session.model || "";
            }
          })
          .catch(function () {});
      }
      // ② 全局默认值：决定「下次新建对话」的起点，也是重启后能留住的东西
      api("/api/settings", { method: "POST", body: globalSettings() }).catch(function () {});
    }, 400);
  }

  // 面板上哪些值属于「用户默认」，与单个会话无关
  function globalSettings() {
    var s = {
      ctx_max: Number($("pCtx").value),
      temperature: Number($("pTemp").value),
      top_p: Number($("pTopP").value),
      max_tokens: Number($("pMaxTok").value),
      compact_enabled: $("pCompact").checked,
      trigger_ratio: Number($("pTrig").value),
      target_ratio: Number($("pTarget").value),
      keep_recent_turns: Number($("pKeep").value),
      enable_thinking: $("pThink").checked
    };
    // 只在还没开始对话时才把当前模型记成默认模型：
    // 否则打开一个旧会话就会把它的模型悄悄变成全局默认
    if (state.messages.length === 0 && $("pModel").value) {
      s.default_model = $("pModel").value;
    }
    return s;
  }

  function bindPanel() {
    ["pCtx", "pTemp", "pTopP", "pMaxTok", "pCompact", "pTrig", "pTarget",
      "pKeep", "pSys", "pThink"].forEach(function (id) {
      $(id).addEventListener("input", function () { syncParamLabels(); persistParams(); });
      $(id).addEventListener("change", function () { syncParamLabels(); persistParams(); });
    });

    // 模型：对话模式下写回会话；生成模式下只换引擎加载的权重，不碰会话参数
    $("pModel").addEventListener("change", function () {
      if (state.mode === "generate") {
        prefillGenParams(modelById($("pModel").value));
        updateModelHint();
        return;
      }
      syncParamLabels();
      persistParams();
    });

    // 生成参数：只影响下一次生成，不落会话
    ["gW", "gH", "gSteps", "gCfg", "gSeed", "gFrames", "gFps"].forEach(function (id) {
      $(id).addEventListener("input", function () { syncGenLabels(); updateSizeHint(); });
      $(id).addEventListener("change", function () { syncGenLabels(); updateSizeHint(); });
    });

    // 快照密度是全局设置，不是会话参数
    $("pSnap").addEventListener("change", function () {
      api("/api/settings", { method: "POST", body: { snapshot_level: $("pSnap").value } })
        .catch(function () {});
    });
  }

  function syncGenLabels() {
    $("vSteps").textContent = $("gSteps").value;
    $("vCfg").textContent = Number($("gCfg").value).toFixed(1);
  }

  // 拉取服务端持久化的默认参数。失败就退回代码里的兜底值，不影响使用。
  function loadSettings() {
    return api("/api/settings").then(function (d) {
      var c = d.config || {};
      var patch = {};
      ["ctx_max", "max_tokens", "temperature", "top_p", "compact_enabled",
        "trigger_ratio", "target_ratio", "keep_recent_turns", "enable_thinking"
      ].forEach(function (k) {
        if (c[k] !== undefined && c[k] !== null) patch[k] = c[k];
      });
      state.defaults = Object.assign({}, DEFAULTS, patch);
      if (!state.current) applySessionParams();
    }).catch(function () {});
  }

  /* ============================== 健康检查 ============================== */

  function pollHealth() {
    api("/api/health").then(function (h) {
      state.health = h;
      var dot = $("healthDot");
      var eng = h.engine || {};
      if (h.backend_ok) {
        dot.className = "dot ok";
        $("healthText").textContent = "引擎就绪";
        $("healthText").title = (eng.adapter || "") + " · " + (eng.base || "");
      } else if (eng.managed && eng.engine_running === false) {
        // 引擎没加载不是故障，是懒加载的正常状态
        dot.className = "dot ok";
        $("healthText").textContent = "待加载";
        $("healthText").title = "首次对话时会自动加载模型";
      } else {
        dot.className = "dot bad";
        $("healthText").textContent = "引擎未连接";
        $("healthText").title = (h.error && h.error.message) || eng.base || "";
      }
      if (h.gpu) {
        $("gpuInfo").textContent = "显存 " + h.gpu.used_mb + " / " + h.gpu.total_mb + " MiB";
      } else {
        $("gpuInfo").textContent = "显存信息不可用";
      }
      if (h.models && h.models.length) fillModels(h.models);
      if (h.snapshot_level && document.activeElement !== $("pSnap")) {
        $("pSnap").value = h.snapshot_level;
      }
      updateModelHint();
    }).catch(function () {
      $("healthDot").className = "dot bad";
      $("healthText").textContent = "服务未启动";
    });
  }

  function fillModels(models) {
    state.models = models;
    state.kindOf = {};
    models.forEach(function (m) { state.kindOf[m.id] = m.kind || "text"; });

    var sel = $("pModel");
    var cur = sel.value || state.params.model;
    sel.innerHTML = "";
    var groups = [["text", "语言模型"], ["image", "图片模型"], ["video", "视频模型"]];
    groups.forEach(function (pair) {
      var list = models.filter(function (m) { return (m.kind || "text") === pair[0]; });
      if (!list.length) return;
      var g = document.createElement("optgroup");
      g.label = pair[1];
      list.forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.id;
        var bits = [m.id];
        if (m.quant) bits.push(m.quant);
        if (m.size_gb) bits.push(m.size_gb.toFixed(1) + "GB");
        o.textContent = bits.join(" · ");
        if (m.fits === false) o.textContent += "（显存放不下）";
        g.appendChild(o);
      });
      sel.appendChild(g);
    });
    if (state.mode === "generate") {
      // 生成模式：下拉只保留当前类型的模型，别让用户选到文本模型
      fillModelsForKind(genKind());
    } else {
      if (cur) sel.value = cur;
      if (!sel.value && models[0]) sel.value = models[0].id;
      updateModelHint();
    }
    updateKindCounts();
  }

  // 选中模型的显存预估：让「装不下」在点发送之前就看得见
  function updateModelHint() {
    var id = $("pModel").value;
    var m = null;
    state.models.forEach(function (x) { if (x.id === id) m = x; });
    var hint = $("modelHint");
    if (!m || !m.est) { hint.textContent = ""; return; }
    var ctx = Number($("pCtx").value) || 8192;
    // 权重与附加开销是固定的，只有 KV 随上下文线性增长
    var fixed = (m.est.weights_mb || 0) + (m.est.overhead_mb || 0);
    var total = Math.round(fixed + (m.est.kv_per_1k_mb || 0) * ctx / 1024);
    var gpu = state.health && state.health.gpu;
    var txt = "预计占用 " + total + " MiB";
    if (gpu) {
      txt += " / " + gpu.total_mb + " MiB";
      if (total > gpu.total_mb - 200) txt += "　显存不足，请降上下文";
    }
    hint.textContent = txt;
    hint.className = "fieldhint" + (gpu && total > gpu.total_mb - 200 ? " bad" : "");
  }

  /* ============================== 桌面壳适配 ============================== */

  // 只在桌面壳里生效；浏览器模式下整段不执行，页面与之前完全一致
  function initDesktop() {
    if (state.desktop || !window.pywebview || !window.pywebview.api) return;
    state.desktop = true;
    document.body.classList.add("desktop");

    // 外链交给系统浏览器：否则点了链接，整个应用就变成一个奇怪的浏览器
    document.addEventListener("click", function (e) {
      var a = e.target.closest ? e.target.closest("a[href^='http']") : null;
      if (!a) return;
      e.preventDefault();
      window.pywebview.api.open_external(a.href);
    });
  }

  /* ============================== 其它交互 ============================== */

  function autoGrow() {
    var el = $("input");
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 180) + "px";
  }

  function compactNow() {
    if (!state.current) return;
    var btn = $("btnCompact");
    btn.disabled = true;
    btn.textContent = "压缩中…";
    fetch("/api/sessions/" + state.current.id + "/compact", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        btn.textContent = "压缩";
        if (!d.compacted) {
          alert("未压缩：" + ({
            no_gain: "压缩收益不足（可压缩内容太少）",
            too_few_turns: "可压缩轮次不足",
            disabled: "自动压缩已关闭",
            below_trigger: "占用尚未超过触发阈值"
          }[d.reason] || d.reason || "无可用内容"));
          return;
        }
        var note = compactNote({
          compacted: true, mode: d.mode, turns: d.turns, before: d.before, after: d.after,
          summary_tokens: (d.summary || "").length, trimmed: 0, summary: d.summary
        });
        ensureInner().insertBefore(note, inner.firstChild);
        renderGauge(d.after, state.params.ctx_max);
        loadSessions();
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = "压缩";
        alert("压缩失败：" + e.message);
      });
  }

  function exportSession() {
    if (!state.current) return;
    var safe = (state.current.title || "对话").replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 60);
    var name = safe + ".md";

    // 浏览器模式：走原来的新标签页下载
    if (!state.desktop) {
      window.open("/api/sessions/" + state.current.id + "/export?format=md", "_blank");
      return;
    }

    // 桌面模式：WebView 里的 window.open 不会弹下载框，改走原生保存对话框
    fetch("/api/sessions/" + state.current.id + "/export?format=md")
      .then(function (r) { return r.text(); })
      .then(function (text) { return window.pywebview.api.save_export(name, text); })
      .then(function (res) {
        if (res && res.error) alert("导出失败：" + res.error);
      })
      .catch(function (e) { alert("导出失败：" + (e.message || e)); });
  }

  /* ============================== 初始化 ============================== */

  function boot() {
    bindChips();
    bindPanel();
    bindKindBar();

    $("btnSend").addEventListener("click", send);
    $("btnStop").addEventListener("click", function () {
      if (state.mode === "generate") stopGenerate();
      else stop();
    });
    $("newChat").addEventListener("click", function () {
      setSelectMode(false);
      newChat();
    });
    $("btnSelect").addEventListener("click", function () {
      setSelectMode(!state.selectMode);
    });
    $("btnCancelSel").addEventListener("click", function () { setSelectMode(false); });
    $("btnBatchDel").addEventListener("click", batchDelete);
    $("selAll").addEventListener("change", function () {
      if (this.checked) {
        state.sessions.forEach(function (s) { state.selected[s.id] = true; });
      } else {
        state.selected = {};
      }
      renderSessions();
      updateBatchBar();
    });
    $("btnPanel").addEventListener("click", function () {
      app.classList.toggle("panel-open");
      if (window.innerWidth <= 900) app.classList.remove("side-open");
    });
    $("closePanel").addEventListener("click", function () { app.classList.remove("panel-open"); });
    $("toggleSide").addEventListener("click", function () { app.classList.toggle("side-open"); });
    $("scrim").addEventListener("click", function () {
      app.classList.remove("side-open");
      app.classList.remove("panel-open");
    });
    $("btnCompact").addEventListener("click", compactNow);
    $("btnExport").addEventListener("click", exportSession);
    $("btnOpenFolder").addEventListener("click", openGenFolder);

    var input = $("input"), composing = false;
    input.addEventListener("compositionstart", function () { composing = true; });
    input.addEventListener("compositionend", function () { composing = false; });
    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !composing && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (state.mode === "generate" && state.generating) stopGenerate();
        else if (state.streaming) stop();
        else if (state.selectMode) setSelectMode(false);
      }
    });

    applySessionParams();
    pollHealth();
    setInterval(pollHealth, 12000);
    // 先取回用户的默认参数，再加载会话：否则新对话会先用兜底值渲染一次再跳变
    loadSettings().then(function () { return loadSessions(); }).then(function () {
      if (state.sessions.length) openSession(state.sessions[0].id);
      else {
        applySessionParams();
        showEmpty();
      }
      applyMode();          // 按当前分类决定显示对话区还是生成区
      syncGenLabels();
      updateSizeHint();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
