// Math: the LaTeX in Claude's replies, drawn as MathML, which the browser lays out itself (no library, nothing loaded
// from elsewhere). grovy finds the math ($…$ or \(…\) in a line, $$…$$ or \[…\] on lines of their own) and hands
// each piece here; what this can't read is shown as written, as code.
//
// A built-in extension that uses only window.grovy. It covers what replies usually hold: fractions, roots, scripts,
// Greek, operators and relations, big operators with limits, functions, accents, fonts (\mathbb, \mathcal, \mathbf...),
// \text, \left…\right, matrices and cases, aligned rows, colours. An unknown command shows in red; the rest still draws.
// The TeX comes from Claude, so it is untrusted: nodes are built one by one, never from markup, and the only
// attribute taken from it is a colour that passes a strict check. To turn it off: grovy.disable("math") in custom.js.
grovy.extension("math", grovy => {
  grovy.css(`
math { font-size:1.06em; }
.mathb math { margin:0 auto; }
mtd.c-l { text-align:left; } mtd.c-r { text-align:right; } mtd.c-c { text-align:center; }
mtable.m-al mtd.c-r { padding-right:0; } mtable.m-al mtd.c-l { padding-left:0; padding-right:1em; }
mtable.m-cases mtd { text-align:left; padding-left:0; padding-right:1em; }
mtext.m-err { color:var(--bad); }
.m-box { border:1px solid currentColor; padding:2px 4px; }`);

  const NS = "http://www.w3.org/1998/Math/MathML";
  function el(tag, attrs, ...kids) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
    for (const k of kids.flat(Infinity)) if (k != null) e.append(k);   // a string is added as text
    return e;
  }
  const mo = (t, a) => el("mo", a, t), row = (...k) => el("mrow", {}, ...k);
  const fence = t => mo(t, {stretchy: "false", lspace: "0", rspace: "0"});   // TeX's plain ( | ⟨: no space around
  const CLOSERS = new Set([")", "]", "}", "|", "‖", "⟩", "⌋", "⌉", "!", "′"]);   // "n! - 1" subtracts
  const SIGNS = new Set(["−", "+", "±", "∓"]);
  const space = em => el("mspace", {width: em + "em"});

  const GREEK = {alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε", zeta: "ζ", eta: "η",
    theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", varkappa: "ϰ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ",
    omicron: "ο", pi: "π", varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ", upsilon: "υ",
    phi: "ϕ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω", digamma: "ϝ",
    Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ",
    Omega: "Ω"};
  const ORD = {infty: "∞", partial: "∂", nabla: "∇", emptyset: "∅", varnothing: "∅", hbar: "ℏ", hslash: "ℏ", ell: "ℓ",
    aleph: "ℵ", beth: "ℶ", Re: "ℜ", Im: "ℑ", wp: "℘", prime: "′", degree: "°", top: "⊤", bot: "⊥", angle: "∠",
    triangle: "△", square: "□", Box: "□", checkmark: "✓", dagger: "†", ddagger: "‡", S: "§", P: "¶",
    clubsuit: "♣", diamondsuit: "♢", heartsuit: "♡", spadesuit: "♠", flat: "♭", natural: "♮", sharp: "♯",
    imath: "ı", jmath: "ȷ", infin: "∞", copyright: "©", dots: "…", ldots: "…", dotsc: "…", cdots: "⋯", dotsb: "⋯",
    dotsm: "⋯", hdots: "…", dotsi: "⋯", vdots: "⋮", ddots: "⋱", "%": "%", "$": "$", "#": "#", "&": "&", _: "_"};
  const OPS = {times: "×", cdot: "⋅", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆", circ: "∘", bullet: "∙",
    oplus: "⊕", ominus: "⊖", otimes: "⊗", oslash: "⊘", odot: "⊙", cup: "∪", cap: "∩", sqcup: "⊔", sqcap: "⊓",
    uplus: "⊎", setminus: "∖", smallsetminus: "∖", wedge: "∧", land: "∧", vee: "∨", lor: "∨", neg: "¬", lnot: "¬",
    diamond: "⋄", wr: "≀", amalg: "⨿", bigtriangleup: "△", bigtriangledown: "▽", triangleleft: "◃",
    triangleright: "▹", lhd: "⊲", rhd: "⊳",
    le: "≤", leq: "≤", ge: "≥", geq: "≥", leqslant: "⩽", geqslant: "⩾", ne: "≠", neq: "≠", lt: "<", gt: ">",
    ll: "≪", gg: "≫", lll: "⋘", ggg: "⋙", approx: "≈", approxeq: "≊", equiv: "≡", sim: "∼", simeq: "≃",
    cong: "≅", propto: "∝", asymp: "≍", doteq: "≐", coloneqq: "≔", eqqcolon: "≕", triangleq: "≜", lesssim: "≲",
    gtrsim: "≳", prec: "≺", succ: "≻", preceq: "⪯", succeq: "⪰", nless: "≮", ngtr: "≯", nleq: "≰", ngeq: "≱",
    in: "∈", notin: "∉", ni: "∋", owns: "∋", subset: "⊂", supset: "⊃", subseteq: "⊆", supseteq: "⊇",
    subsetneq: "⊊", supsetneq: "⊋", nsubseteq: "⊈", sqsubseteq: "⊑", sqsupseteq: "⊒",
    perp: "⊥", parallel: "∥", nparallel: "∦", mid: "∣", nmid: "∤", vdash: "⊢", dashv: "⊣", models: "⊨",
    vDash: "⊨", Vdash: "⊩", forall: "∀", exists: "∃", nexists: "∄", therefore: "∴", because: "∵",
    to: "→", rightarrow: "→", leftarrow: "←", gets: "←", leftrightarrow: "↔", Rightarrow: "⇒", Leftarrow: "⇐",
    Leftrightarrow: "⇔", implies: "⟹", impliedby: "⟸", iff: "⟺", longrightarrow: "⟶", longleftarrow: "⟵",
    longleftrightarrow: "⟷", Longrightarrow: "⟹", Longleftarrow: "⟸", Longleftrightarrow: "⟺", mapsto: "↦",
    longmapsto: "⟼", hookrightarrow: "↪", hookleftarrow: "↩", uparrow: "↑", downarrow: "↓", updownarrow: "↕",
    Uparrow: "⇑", Downarrow: "⇓", nearrow: "↗", searrow: "↘", swarrow: "↙", nwarrow: "↖", rightleftharpoons: "⇌",
    leftrightarrows: "⇆", rightrightarrows: "⇉", twoheadrightarrow: "↠", rightharpoonup: "⇀", leadsto: "⇝",
    colon: ":", vert: "|", Vert: "‖", "|": "‖", lvert: "|", rvert: "|", lVert: "‖", rVert: "‖", langle: "⟨",
    rangle: "⟩", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉", "{": "{", "}": "}", lbrace: "{", rbrace: "}",
    lbrack: "[", rbrack: "]", backslash: "∖", ulcorner: "⌜", urcorner: "⌝", llcorner: "⌞", lrcorner: "⌟"};
  const FENCES = new Set(["|", "‖", "⟨", "⟩", "⌊", "⌋", "⌈", "⌉", "{", "}", "[", "]", "∖"]);
  const BIG = {sum: "∑", prod: "∏", coprod: "∐", bigcup: "⋃", bigcap: "⋂", bigvee: "⋁", bigwedge: "⋀",
    bigoplus: "⨁", bigotimes: "⨂", bigodot: "⨀", biguplus: "⨄", bigsqcup: "⨆"};
  const INTS = {int: "∫", iint: "∬", iiint: "∭", oint: "∮", oiint: "∯", intop: "∫"};
  const FUNCS = ["sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "coth", "log", "ln", "lg", "exp", "dim", "ker", "deg", "arg", "hom", "Pr", "det", "gcd", "lim", "max", "min",
    "sup", "inf", "liminf", "limsup"];
  const LIMIT_FUNCS = new Set(["lim", "max", "min", "sup", "inf", "liminf", "limsup", "det", "gcd", "Pr"]);
  const FUNC_TEXT = {liminf: "lim inf", limsup: "lim sup"};
  // accents: [character, stretchy, under]
  const ACCENTS = {hat: ["^"], widehat: ["^", 1], check: ["ˇ"], widecheck: ["ˇ", 1], tilde: ["~"],
    widetilde: ["~", 1], bar: ["¯"], overline: ["‾", 1], vec: ["⃗"], overrightarrow: ["→", 1],
    overleftarrow: ["←", 1], overleftrightarrow: ["↔", 1], dot: ["˙"], ddot: ["¨"], dddot: ["⃛"], acute: ["´"],
    grave: ["`"], breve: ["˘"], mathring: ["˚"], underline: ["_", 1, 1], underrightarrow: ["→", 1, 1],
    underleftarrow: ["←", 1, 1], overbrace: ["⏞", 1], underbrace: ["⏟", 1, 1]};
  const NEGATE = {"=": "≠", "<": "≮", ">": "≯", "≤": "≰", "≥": "≱", "∈": "∉", "∋": "∌", "⊂": "⊄", "⊃": "⊅",
    "⊆": "⊈", "⊇": "⊉", "≡": "≢", "∼": "≁", "≈": "≉", "≅": "≇", "∣": "∤", "∥": "∦", "∃": "∄", "→": "↛"};
  const SPACES = {",": 0.1667, thinspace: 0.1667, ":": 0.2222, ">": 0.2222, medspace: 0.2222, ";": 0.2778,
    thickspace: 0.2778, "!": -0.1667, negthinspace: -0.1667, quad: 1, qquad: 2, " ": 0.3333, enspace: 0.5};
  const SIZES = {big: 1.2, Big: 1.8, bigg: 2.4, Bigg: 3};
  // letters in the Mathematical Alphanumeric Symbols block: [A, a, 0] for each font (null: no digits there)
  const ALPHA = {bold: [0x1D400, 0x1D41A, 0x1D7CE], italic: [0x1D434, 0x1D44E, null],
    bolditalic: [0x1D468, 0x1D482, 0x1D7CE], script: [0x1D49C, 0x1D4B6, null], fraktur: [0x1D504, 0x1D51E, null],
    bb: [0x1D538, 0x1D552, 0x1D7D8], sans: [0x1D5A0, 0x1D5BA, 0x1D7E2], tt: [0x1D670, 0x1D68A, 0x1D7F6]};
  const HOLES = {"italic:h": "ℎ", "script:B": "ℬ", "script:E": "ℰ", "script:F": "ℱ", "script:H": "ℋ",
    "script:I": "ℐ", "script:L": "ℒ", "script:M": "ℳ", "script:R": "ℛ", "script:e": "ℯ", "script:g": "ℊ",
    "script:o": "ℴ", "fraktur:C": "ℭ", "fraktur:H": "ℌ", "fraktur:I": "ℑ", "fraktur:R": "ℜ", "fraktur:Z": "ℨ",
    "bb:C": "ℂ", "bb:H": "ℍ", "bb:N": "ℕ", "bb:P": "ℙ", "bb:Q": "ℚ", "bb:R": "ℝ", "bb:Z": "ℤ"};
  const FONTS = {mathbf: "bold", boldsymbol: "bolditalic", bm: "bolditalic", mathit: "italic",
    mathcal: "script", mathscr: "script", mathfrak: "fraktur", mathbb: "bb", Bbb: "bb", mathsf: "sans",
    mathtt: "tt", mathrm: "normal", mathup: "normal", rm: "normal", bf: "bold", it: "italic", cal: "script"};
  function styled(ch, font) {
    const hole = HOLES[font + ":" + ch];
    if (hole) return hole;
    const base = ALPHA[font], c = ch.charCodeAt(0);
    if (!base) return ch;
    if (c >= 65 && c <= 90) return String.fromCodePoint(base[0] + c - 65);
    if (c >= 97 && c <= 122) return String.fromCodePoint(base[1] + c - 97);
    if (c >= 48 && c <= 57 && base[2]) return String.fromCodePoint(base[2] + c - 48);
    return ch;
  }
  const ENVS = {matrix: ["", ""], pmatrix: ["(", ")"], bmatrix: ["[", "]"], Bmatrix: ["{", "}"], vmatrix: ["|", "|"],
    Vmatrix: ["‖", "‖"], smallmatrix: ["", ""], array: ["", ""], cases: ["{", ""], rcases: ["", "}"],
    dcases: ["{", ""], aligned: 0, align: 0, "align*": 0, alignat: 0, "alignat*": 0, alignedat: 0, split: 0,
    eqnarray: 0, "eqnarray*": 0, gathered: 0, gather: 0, "gather*": 0, equation: 0, "equation*": 0, multline: 0,
    "multline*": 0, displaymath: 0, subarray: ["", ""]};
  const COLOR = /^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|[a-zA-Z]{1,20})$/;
  const LENGTH = /^\s*(-?\d*\.?\d+)\s*(em|ex|pt|mu|px)\s*$/;
  const MAX_LEN = 4000, MAX_DEPTH = 40;

  const isMo = k => k.localName === "mo";
  const LIMITS = new WeakSet(), FUNC = new WeakSet();   // scripts go under/over these; a thin space follows these

  class Parser {
    constructor(src, display) { this.s = src; this.i = 0; this.display = display; this.font = null; this.depth = 0; }
    fail(why) { throw new Error(why); }
    at(t) { return this.s.startsWith(t, this.i); }
    atCmd(name) { return this.at("\\" + name) && !/[A-Za-z]/.test(this.s[this.i + name.length + 1] || ""); }
    skip() { while (this.i < this.s.length && /\s/.test(this.s[this.i])) this.i++; }
    deeper(fn) {
      if (++this.depth > MAX_DEPTH) this.fail("nested too deeply");
      try { return fn(); } finally { this.depth--; }
    }

    // a list of nodes, up to the end or until stop() (which is not consumed)
    expr(stop) {
      const out = [];
      let prev = null;   // the last node that isn't a space
      for (;;) {
        this.skip();
        if (this.i >= this.s.length || stop()) return out;
        const r = this.atom();
        if (r && r.rest) { out.push(this.rest(r, stop)); return out; }
        if (r == null) continue;
        // a sign at the start or after an operator is unary ("= -2", "(-x"): no space as if subtracting
        if (isMo(r) && SIGNS.has(r.textContent) && (!prev || (isMo(prev) && !CLOSERS.has(prev.textContent))))
          r.setAttribute("form", "prefix");
        if (r.localName !== "mspace") prev = r;
        out.push(r);
        this.skip();
        if (FUNC.has(r) && this.i < this.s.length && !this.at("(") && !this.at("[") && !this.at("\\{") &&
            !this.atCmd("left") && !this.atCmd("langle") && !stop()) out.push(space(0.1667));
      }
    }
    rest(r, stop) {   // \displaystyle, \color{c}, \rm...: everything after it in this group
      const saved = [this.display, this.font];
      if (r.display != null) this.display = r.display;
      if (r.font !== undefined) this.font = r.font;
      try { return this.deeper(() => r.rest(this.expr(stop))); } finally { [this.display, this.font] = saved; }
    }
    // rows of cells, split by & and \\, until end() (not consumed)
    rows(end) {
      const rows = [[]];
      for (;;) {
        rows[rows.length - 1].push(this.expr(() => end() || this.at("&") || this.at("\\\\")));
        if (this.at("&")) { this.i++; continue; }
        if (this.at("\\\\")) {
          this.i += 2; this.skip();
          if (this.at("[")) this.raw("[", "]");   // \\[4pt]: extra space, ignored
          rows.push([]); continue;
        }
        break;
      }
      const last = rows[rows.length - 1];
      if (rows.length > 1 && last.length === 1 && !last[0].length) rows.pop();   // a trailing \\
      return rows;
    }

    atom() {
      const c = this.s[this.i];
      const base = c === "^" || c === "_" || c === "'" ? row() : this.base();
      if (base == null || base.rest) return base;
      return this.scripts(base);
    }
    scripts(base) {
      let sub = null, sup = null, primes = 0;
      for (;;) {
        this.skip();
        if (this.at("'")) { primes++; this.i++; }
        else if (this.at("^") && !sup) { this.i++; sup = this.arg(); }
        else if (this.at("_") && !sub) { this.i++; sub = this.arg(); }
        else if (this.atCmd("limits")) { this.i += 7; LIMITS.add(base); }
        else if (this.atCmd("nolimits")) { this.i += 9; LIMITS.delete(base); }
        else break;
      }
      if (primes) { const p = mo(["′", "″", "‴"][primes - 1] || "′".repeat(primes)); sup = sup ? row(p, sup) : p; }
      if (!sub && !sup) return base;
      const lim = LIMITS.has(base) && !primes;
      const node = sub && sup ? el(lim ? "munderover" : "msubsup", {}, base, sub, sup)
        : sub ? el(lim ? "munder" : "msub", {}, base, sub) : el(lim ? "mover" : "msup", {}, base, sup);
      if (FUNC.has(base)) FUNC.add(node);
      return node;
    }
    // a command's argument: a {group}, or one character or command
    arg() {
      this.skip();
      if (this.i >= this.s.length) this.fail("an argument is missing");
      if (this.at("{")) return this.group();
      if (this.at("}") || this.at("&") || this.at("^") || this.at("_")) this.fail("an argument is missing");
      if (this.at("\\")) {
        const r = this.deeper(() => this.command(this.name()));
        if (r && r.rest) this.fail("this can't be an argument");
        return r || row();
      }
      const ch = String.fromCodePoint(this.s.codePointAt(this.i));
      this.i += ch.length;
      return this.char(ch);
    }
    group() {
      this.i++;
      const kids = this.deeper(() => this.expr(() => this.at("}")));
      if (!this.at("}")) this.fail("a { has no }");
      this.i++;
      return row(kids);
    }
    raw(open = "{", close = "}") {   // the text of a {…} argument, as written
      this.skip();
      if (!this.at(open)) this.fail("expected " + open);
      let depth = 0;
      const start = ++this.i;
      for (; this.i < this.s.length; this.i++) {
        const c = this.s[this.i];
        if (c === "\\") { this.i++; continue; }
        if (c === open) depth++;
        else if (c === close) { if (!depth--) return this.s.slice(start, this.i++); }
      }
      this.fail("a " + open + " has no " + close);
    }
    name() {
      const m = /^\\([A-Za-z]+|[^A-Za-z])/su.exec(this.s.slice(this.i, this.i + 40));
      if (!m) this.fail("a \\ ends the formula");
      this.i += m[0].length;
      return m[1];
    }

    char(ch) {
      if (/[0-9]/.test(ch)) return el("mn", {}, this.font && this.font !== "normal" ? styled(ch, this.font) : ch);
      if (/[A-Za-z]/.test(ch)) return this.letter(ch);
      if (/\p{L}/u.test(ch)) return el("mi", {}, ch);
      if ("()[]|".includes(ch)) return fence(ch);
      if (ch === "-") return mo("−");
      if (ch === "*") return mo("∗");
      if (ch === "~") return space(0.3333);
      return mo(ch);
    }
    letter(ch) {
      if (!this.font) return el("mi", {}, ch);
      if (this.font === "normal") return el("mi", {mathvariant: "normal"}, ch);
      return el("mi", {mathvariant: "normal"}, styled(ch, this.font));
    }
    base() {
      const s = this.s, c = s[this.i];
      if (c === "{") return this.group();
      if (c === "}") this.fail("a } has no {");
      if (c === "&") this.fail("& outside a table");
      if (c === "\\") return this.deeper(() => this.command(this.name()));
      const num = /^(\d+(?:\.\d+)?|\.\d+)/.exec(s.slice(this.i, this.i + 64));
      if (num) {
        this.i += num[0].length;
        return el("mn", {}, this.font && this.font !== "normal" ? [...num[0]].map(d => styled(d, this.font)).join("") : num[0]);
      }
      const ch = String.fromCodePoint(s.codePointAt(this.i));
      this.i += ch.length;
      return this.char(ch);
    }
    delim() {   // the delimiter after \left, \right, \big...: a character or a command; "" for "."
      this.skip();
      if (this.at("\\")) {
        const n = this.name(), t = OPS[n] || ORD[n];
        if (!t) this.fail("\\" + n + " isn't a delimiter");
        return t;
      }
      const ch = this.s[this.i++];
      if (ch == null) this.fail("a delimiter is missing");
      if (ch === ".") return "";
      if (ch === "<") return "⟨";
      if (ch === ">") return "⟩";
      if (!"()[]|/".includes(ch)) this.fail(JSON.stringify(ch) + " isn't a delimiter");
      return ch;
    }

    command(n) {
      if (n in GREEK) return el("mi", /[Α-Ω]/.test(GREEK[n]) ? {mathvariant: "normal"} : {}, GREEK[n]);
      if (n in SPACES) return space(SPACES[n]);
      if (n in ORD) return el("mi", {}, ORD[n]);
      if (n in OPS) return FENCES.has(OPS[n]) ? fence(OPS[n]) : mo(OPS[n]);
      if (n in BIG) { const o = mo(BIG[n]); if (this.display) LIMITS.add(o); return o; }
      if (n in INTS) return mo(INTS[n]);
      if (FUNCS.includes(n)) {
        const f = el("mi", {}, FUNC_TEXT[n] || n);
        FUNC.add(f);
        if (this.display && LIMIT_FUNCS.has(n)) LIMITS.add(f);
        return f;
      }
      if (n in FONTS) {
        const font = FONTS[n];
        if (["rm", "bf", "it", "cal"].includes(n)) return {font, rest: kids => row(kids)};   // {\rm x}: the rest of the group
        const saved = this.font;
        this.font = font;
        try { return this.arg(); } finally { this.font = saved; }
      }
      if (n in ACCENTS) {
        const [ch, stretchy, under] = ACCENTS[n], body = this.arg(), o = mo(ch, {stretchy: stretchy ? "true" : "false"});
        const node = under ? el("munder", {accentunder: "true"}, body, o) : el("mover", {accent: "true"}, body, o);
        if (n === "overbrace" || n === "underbrace") LIMITS.add(node);
        return node;
      }
      if (n in SIZES) return mo(this.delim(), {stretchy: "true", symmetric: "true", minsize: SIZES[n] + "em", maxsize: SIZES[n] + "em"});
      const sized = /^(big|Big|bigg|Bigg)([lrm])$/.exec(n);
      if (sized) { const s = SIZES[sized[1]]; return mo(this.delim(), {stretchy: "true", symmetric: "true", minsize: s + "em", maxsize: s + "em"}); }
      switch (n) {
        case "frac": case "dfrac": case "tfrac": case "cfrac": {
          const f = el("mfrac", {}, this.arg(), this.arg());
          return n === "frac" ? f : el("mrow", {displaystyle: n === "tfrac" ? "false" : "true"}, f);
        }
        case "binom": case "dbinom": case "tbinom":
          return row(fence("("), el("mfrac", {linethickness: "0"}, this.arg(), this.arg()), fence(")"));
        case "sqrt": {
          this.skip();
          if (this.at("[")) {
            const index = new Parser(this.raw("[", "]"), false), idx = row(index.expr(() => false));
            return el("mroot", {}, this.arg(), idx);
          }
          return el("msqrt", {}, this.arg());
        }
        case "text": case "textrm": case "textup": case "textnormal": case "mbox": case "hbox": case "textsf":
        case "texttt": case "textit": case "emph": case "textbf": case "textmd": case "operatorname": {
          const star = n === "operatorname" && this.at("*") && !!++this.i;   // \operatorname*: limits below
          // spacing commands become spaces, other commands and bare braces go (\text{\textbf{x}}: x), \$ is $
          const text = this.raw().replace(/\\(?:[,:;! ]|q?quad\b)/g, " ").replace(/\\[A-Za-z]+\s*/g, "")
            .replace(/(?<!\\)[{}$]/g, "").replace(/\\([{}$%&#_])/g, "$1").replace(/~/g, " ");
          if (n === "operatorname") {
            const f = el("mi", {}, text);
            FUNC.add(f);
            if (star && this.display) LIMITS.add(f);
            return f;
          }
          const style = {textbf: "font-weight:bold", textit: "font-style:italic", emph: "font-style:italic",
                         texttt: "font-family:monospace", textsf: "font-family:sans-serif"}[n];
          return el("mtext", {style}, text.replace(/ /g, "\u00a0"));
        }
        case "left": {
          const open = this.delim();
          const kids = this.deeper(() => this.expr(() => this.atCmd("right")));
          if (!this.atCmd("right")) this.fail("a \\left has no \\right");
          this.i += 6;
          const close = this.delim();
          return row(open ? mo(open, {stretchy: "true", symmetric: "true", form: "prefix"}) : null, kids,
                     close ? mo(close, {stretchy: "true", symmetric: "true", form: "postfix"}) : null);
        }
        case "middle": return mo(this.delim(), {stretchy: "true", symmetric: "true"});
        case "right": this.fail("a \\right has no \\left");   // fail() throws: no fallthrough
        case "not": {
          this.skip();
          const next = this.arg(), t = next.textContent;
          if (NEGATE[t]) return mo(NEGATE[t]);
          return mo(t + "̸");
        }
        case "overset": case "stackrel": { const top = this.arg(), body = this.arg(); return el("mover", {}, body, top); }
        case "underset": { const bottom = this.arg(), body = this.arg(); return el("munder", {}, body, bottom); }
        case "xrightarrow": case "xleftarrow": case "xRightarrow": case "xLeftarrow": {
          this.skip();
          const below = this.at("[") ? row(new Parser(this.raw("[", "]"), false).expr(() => false)) : null;
          const above = this.arg(), arrow = mo({xrightarrow: "→", xleftarrow: "←", xRightarrow: "⇒", xLeftarrow: "⇐"}[n], {stretchy: "true"});
          return below ? el("munderover", {}, arrow, below, above) : el("mover", {}, arrow, above);
        }
        case "pmod": return row(space(0.4444), fence("("), el("mi", {}, "mod"), space(0.3333), this.arg(), fence(")"));
        case "bmod": case "mod": return row(space(n === "mod" ? 0.4444 : 0.2222), el("mi", {}, "mod"), space(0.2222));
        case "color": {
          const c = this.raw().trim();
          if (!COLOR.test(c)) this.fail("unknown colour " + JSON.stringify(c));
          return {rest: kids => el("mrow", {style: "color:" + c}, kids)};
        }
        case "textcolor": case "colorbox": {
          const c = this.raw().trim();
          if (!COLOR.test(c)) this.fail("unknown colour " + JSON.stringify(c));
          return el("mrow", {style: (n === "colorbox" ? "background:" : "color:") + c}, this.arg());
        }
        case "boxed": case "fbox": return el("mrow", {class: "m-box"}, n === "fbox" ? el("mtext", {}, this.raw()) : this.arg());
        case "phantom": return el("mphantom", {}, this.arg());
        case "displaystyle": case "textstyle": case "scriptstyle": {
          const display = n === "displaystyle";
          return {display, rest: kids => el("mrow", {displaystyle: String(display), scriptlevel: n === "scriptstyle" ? "1" : null}, kids)};
        }
        case "mathop": { const o = this.arg(); if (this.display) LIMITS.add(o); return o; }
        case "substack": {
          this.skip();
          if (!this.at("{")) this.fail("\\substack needs {…}");
          this.i++;
          const rows = this.deeper(() => this.rows(() => this.at("}")));
          if (!this.at("}")) this.fail("a { has no }");
          this.i++;
          return this.table(rows, () => "c", "", false);
        }
        case "begin": return this.deeper(() => this.env(this.raw().trim()));
        case "end": this.fail("an \\end has no \\begin");
        case "hspace": case "mspace": if (this.at("*")) this.i++; return this.length(this.raw());
        case "kern": case "mkern": case "hskip": case "mskip": {
          this.skip();
          const m = /^-?\d*\.?\d+\s*(em|ex|pt|mu|px)/.exec(this.s.slice(this.i, this.i + 24));
          if (!m) return null;
          this.i += m[0].length;
          return this.length(m[0]);
        }
        case "label": case "tag": case "ref": case "eqref": this.raw(); return null;
        case "cline": this.raw(); return null;
        case "hline": case "hdashline": case "nonumber": case "notag": case "limits": case "nolimits": case "\\": case "allowbreak": case "relax":
        case "displaybreak": case "strut": return null;
      }
      return el("mtext", {class: "m-err"}, "\\" + n);
    }

    length(text) {   // \hspace{1em} and the like: a space, within reason
      const m = LENGTH.exec(text);
      if (!m) return null;
      const v = parseFloat(m[1]), em = {em: v, ex: v / 2, pt: v / 10, mu: v / 18, px: v / 16}[m[2]];
      return space(Math.max(-2, Math.min(10, em)).toFixed(3));
    }
    env(name) {
      if (!(name in ENVS)) this.fail("unknown environment " + name);
      let spec = null;
      if (name === "array" || name === "subarray" || name.startsWith("alignat")) spec = this.raw();
      const end = () => this.atCmd("end");
      const rows = this.rows(end);
      if (!end()) this.fail("\\begin{" + name + "} has no \\end");
      this.i += 4;
      if (this.raw().trim() !== name) this.fail("\\begin{" + name + "} ends with a different \\end");
      const d = ENVS[name];
      if (Array.isArray(d)) {
        const cases = /cases/.test(name);
        const cols = spec && !name.startsWith("alignat") ? spec.replace(/[^lcr]/g, "") : "";
        const t = this.table(rows, j => cases ? "l" : cols[j] || "c", cases ? "m-cases" : "", name === "dcases",
                             name === "smallmatrix" || name === "subarray");
        return row(d[0] ? mo(d[0], {stretchy: "true", symmetric: "true"}) : null, t,
                   d[1] ? mo(d[1], {stretchy: "true", symmetric: "true"}) : null);
      }
      if (/gather|equation|multline|displaymath/.test(name)) return this.table(rows, () => "c", "", true);
      return this.table(rows, j => j % 2 ? "l" : "r", "m-al", true);   // align, aligned, split...: =, ≤ line up
    }
    table(rows, align, cls, display, small) {
      if (rows.length === 1 && rows[0].length === 1 && !cls && !small) return el("mrow", {displaystyle: display ? "true" : null}, rows[0][0]);
      return el("mtable", {class: cls || null, scriptlevel: small ? "1" : null},
        rows.map(r => el("mtr", {}, r.map((cell, j) => el("mtd", {class: "c-" + align(j)},
          el("mrow", {displaystyle: display ? "true" : null}, cell))))));
    }
  }

  // tex (untrusted) -> a <math> node; throws an Error saying what's wrong, and grovy then shows the TeX as written
  function render(tex, display) {
    tex = String(tex);
    if (tex.length > MAX_LEN) throw new Error("too long to draw");
    const p = new Parser(tex, !!display);
    const rows = p.rows(() => false);
    if (p.i < tex.length) p.fail("can't read this");
    const body = rows.length === 1 && rows[0].length === 1 ? row(rows[0][0])
      : p.table(rows, j => rows.some(r => r.length > 1) ? (j % 2 ? "l" : "r") : "c",
                rows.some(r => r.length > 1) ? "m-al" : "", true);
    return el("math", {display: display ? "block" : null}, body);
  }
  grovy.math(render);
}, {about: "LaTeX formulas in replies"});
