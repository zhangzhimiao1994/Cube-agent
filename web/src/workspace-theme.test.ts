import { existsSync, readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";

const legacy = readFileSync("src/styles.css", "utf8");
const conversation = readFileSync("src/conversation-workspace.css", "utf8");
const code = readFileSync("src/components/CodeContent.css", "utf8");
const theme = existsSync("src/workspace-theme.css") ? readFileSync("src/workspace-theme.css", "utf8") : "";

function addStyle(css: string) {
  const style = document.createElement("style");
  style.textContent = css;
  document.head.append(style);
  return style;
}

function addTheme() {
  // jsdom cannot match nested :has() inside :not() on ancestor selectors.
  // Test the active management cascade separately from the scope contract below.
  if (document.querySelector(".conversation-workspace")) {
    const style = addStyle(theme);
    const sheet = style.sheet!;
    for (let index = sheet.cssRules.length - 1; index >= 0; index--) {
      const rule = sheet.cssRules[index];
      if (rule.type !== CSSRule.STYLE_RULE || /^(body|:root):not\(:has\(\.conversation-workspace\)\)/.test((rule as CSSStyleRule).selectorText)) {
        sheet.deleteRule(index);
      }
    }
    return style;
  }
  return addStyle(theme
    .replaceAll("body:not(:has(.conversation-workspace))", "body")
    .replaceAll(":root:not(:has(.conversation-workspace))", ":root"));
}

function mount(content: string, themed = true) {
  addStyle(legacy);
  addStyle(conversation);
  addStyle(code);
  document.body.innerHTML = content;
  if (themed) addTheme();
}

const styles = (selector: string) => getComputedStyle(document.querySelector(selector)!);
afterEach(() => {
  document.head.querySelectorAll("style").forEach((node) => node.remove());
  document.body.innerHTML = "";
});

describe("shared workspace theme", () => {
  it.each([
    ["login", '<main class="auth-hero"><img class="auth-brand-logo" src="/brand/mofang-agent-light.png"><h1>Login</h1></main>', ".auth-brand-logo", "h1"],
    ["setup", '<main class="auth-hero"><img class="auth-brand-logo" src="/brand/mofang-agent-light.png"><h1>Setup</h1></main>', ".auth-brand-logo", "h1"],
    ["management sidebar", '<aside class="sidebar"><div class="brand-card compact-brand-card"><img src="/brand/mofang-agent-light.png"><span class="eyebrow">Mofang Agent</span></div></aside>', ".brand-card img", ".eyebrow"],
    ["management mobile header", '<aside class="sidebar"><span class="brand-lockup"><img src="/brand/mofang-agent-light.png"><strong>Mofang Agent</strong></span></aside>', ".brand-lockup img", "strong"],
    ["conversation sidebar", '<div class="is-conversation-workspace"><aside class="sidebar"><div class="brand-card compact-brand-card"><img src="/brand/mofang-agent-light.png"><span class="eyebrow">Mofang Agent</span></div></aside><section class="conversation-workspace"></section></div>', ".brand-card img", ".eyebrow"],
    ["conversation mobile header", '<div class="is-conversation-workspace"><aside class="sidebar"><span class="brand-lockup"><img src="/brand/mofang-agent-light.png"><strong>Mofang Agent</strong></span></aside><section class="conversation-workspace"></section></div>', ".brand-lockup img", "strong"],
  ])("softens only the logo image in %s without changing its size or text", (_name, markup, logoSelector, textSelector) => {
    mount(markup, false);
    const dimensions = () => {
      const logo = styles(logoSelector);
      return [logo.width, logo.height, logo.aspectRatio, logo.objectFit];
    };
    const textAppearance = () => {
      const text = styles(textSelector);
      const ancestors = [];
      for (let node = document.querySelector(logoSelector)!.parentElement; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        ancestors.push([style.opacity, style.filter]);
      }
      return [text.opacity, text.filter, ...ancestors];
    };
    const beforeText = textAppearance();
    addTheme();
    // Verify the real import order: conversation overrides load after the shared theme.
    addStyle(conversation);
    const logo = styles(logoSelector);
    expect(logo.filter).toBe("saturate(0.55)");
    expect(logo.opacity).toBe("0.78");
    expect(logo.backgroundColor).toBe("rgba(0, 0, 0, 0)");
    expect(logo.backgroundImage || "none").toBe("none");
    expect(logo.boxShadow).toBe("none");
    const size = logoSelector === ".brand-card img" ? "32px" : logoSelector === ".brand-lockup img" ? "2rem" : "4.5rem";
    expect(dimensions()).toEqual([size, size, "1", "contain"]);
    expect(textAppearance()).toEqual(beforeText);
    expect(document.querySelector(logoSelector)!.getAttribute("src")).toBe("/brand/mofang-agent-light.png");
  });

  it("avoids repeating the brand and account header above a management page", () => {
    mount('<div class="app-shell"><header class="topbar"><h2>Brand</h2><div class="user-chip">Account</div></header><main class="page-surface"><h2>Settings</h2></main></div>');
    expect(styles('.topbar').display).toBe('none');
    expect(styles('.page-surface').display).not.toBe('none');
  });
  it("uses the conversation palette on management pages", () => {
    mount('<main class="page-surface"><h2>Settings</h2></main>');
    const root = getComputedStyle(document.documentElement);
    expect(root.getPropertyValue("--bg").trim()).toBe("#fff");
    expect(root.getPropertyValue("--ink").trim()).toBe("#1f2329");
    expect(root.getPropertyValue("--line").trim()).toBe("#dfe3e8");
    expect(root.getPropertyValue("--accent").trim()).toBe("#087f8c");
    expect(root.colorScheme).toBe("light");
  });

  it("keeps a 208px sidebar and lets long management pages scroll in the document", () => {
    mount('<div class="app-shell nav-floating"><aside class="sidebar"><div class="floating-nav-panel"><div class="floating-nav-rail"></div></div></aside><div class="workspace"><main class="page-surface"><section><article>Long form</article></section></main></div></div>');
    expect(styles(".app-shell").getPropertyValue("--desktop-nav-column").trim()).toBe("208px");
    expect(styles(".page-surface").height).toBe("auto");
    expect(styles(".page-surface").overflow).toBe("visible");
    expect(styles(".page-surface").padding).toBe("24px");
    expect(styles(".page-surface").boxShadow).toBe("none");
    expect(styles("article").borderRadius).toBe("0");
  });

  it("applies compact controls and readable table cells without squeezing headers", () => {
    mount('<main class="page-surface"><label class="inline-check"><input type="checkbox" checked></label><input class="name"><button>Save</button><div class="table-shell"><table class="dense-table" aria-label="运行时能力注册表"><thead><tr><th>类型</th></tr></thead><tbody><tr><td>runtime_builtin</td></tr></tbody></table></div></main>');
    expect(styles("input.name").borderRadius).toBe("6px");
    expect(styles("input[type=checkbox]").appearance).toBe("auto");
    expect(styles("button").borderRadius).toBe("6px");
    expect(styles(".table-shell").overflowX).toBe("auto");
    expect(styles("table").tableLayout).toBe("auto");
    expect(styles("table").width).toBe("max-content");
    expect(styles("th").whiteSpace).toBe("nowrap");
    expect(styles("td").padding).toBe("8px 10px");
  });

  it("contains registry scrolling and wraps long capability IDs inside their own cell", () => {
    mount(`<main class="page-surface"><section><div class="table-shell"><table aria-label="运行时能力注册表"><tbody><tr><td><strong>${"technical-id-".repeat(40)}</strong></td><td>内置</td><td>无</td></tr></tbody></table></div></section></main>`);
    expect(styles(".table-shell").width).toBe("100%");
    expect(styles(".table-shell").minWidth).toBe("0");
    expect(styles(".table-shell").contain).toBe("inline-size");
    expect(styles("td:first-child").whiteSpace).toBe("normal");
    expect(styles("td:nth-child(2)").whiteSpace).toBe("nowrap");
    expect(styles("td:first-child strong").display).toBe("block");
    expect(styles("td:first-child strong").maxWidth).toBe("24rem");
    expect(styles("td:first-child strong").overflowWrap).toBe("anywhere");
  });

  it("uses the darker text accent on grey surfaces and removes the channel guide's dark fill", () => {
    mount('<main class="page-surface"><button class="table-sort-button"><span>Model</span></button><ol class="event-log-list"><li><strong>Event</strong></li></ol><section class="channel-command-guide"><h3>Resources</h3><p>Choose resources</p></section></main>');
    expect(styles(".table-sort-button").color).toBe("var(--accent-dark)");
    expect(styles(".event-log-list strong").color).toBe("var(--accent-dark)");
    expect(styles(".channel-command-guide").backgroundColor).toBe("rgba(0, 0, 0, 0)");
    expect(getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()).toBe("#087f8c");
  });

  it("wraps complete plugin IDs and resource metadata without clipping inline spans", () => {
    const pluginId = "plugin-package-sha256-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    mount(`<main class="page-surface"><section aria-label="已配置插件"><div class="card-grid"><article><h3>${pluginId}</h3><p>ID：<span>${pluginId}</span></p><p>内容 SHA-256：<span>${pluginId.slice(-64)}</span></p></article></div></section><article class="resource-card"><h3>${pluginId}</h3><dl><dt>包来源</dt><dd><span>${pluginId}</span></dd></dl></article><article class="skill-source-card"><p>来源 URL：<span>https://example.test/packages/${pluginId}</span></p></article></main>`);
    for (const selector of [".card-grid > article", ".resource-card", ".skill-source-card"]) {
      expect(styles(selector).minWidth, selector).toBe("0");
      expect(styles(selector).overflow, selector).toBe("visible");
    }
    for (const selector of [".card-grid h3", ".card-grid p", ".resource-card h3", ".resource-card dd", ".skill-source-card p"]) {
      expect(styles(selector).overflowWrap, selector).toBe("anywhere");
      expect(styles(selector).minWidth, selector).toBe("0");
      expect(styles(selector).whiteSpace, selector).toBe("normal");
      expect(styles(selector).textOverflow, selector).not.toBe("ellipsis");
    }
    // overflow-wrap inherits into the actual classless inline plugin ID span.
    const idSpan = document.querySelector(".card-grid p > span")!;
    expect(idSpan.className).toBe("");
    expect(idSpan.parentElement!.className).toBe("");
    expect(idSpan.textContent).toBe(pluginId);
  });

  it("overrides only the legacy important approval and OpenClaw text colors", () => {
    mount('<main class="page-surface"><p class="openclaw-policy-warning">Allowlist warning</p><span class="warning-text">待审批</span><span class="success-text">已审批</span></main>');
    const sheet = Array.from(document.styleSheets).at(-1)!;
    const rules = Array.from(sheet.cssRules).filter((rule): rule is CSSStyleRule => rule.type === CSSRule.STYLE_RULE);
    for (const [selector, color] of [[".openclaw-policy-warning", "var(--warning)"], [".warning-text", "var(--warning)"], [".success-text", "var(--success)"]]) {
      const override = rules.find((rule) => rule.selectorText.endsWith(` ${selector}`));
      // jsdom does not reliably resolve !important across style elements.
      expect(override?.style.getPropertyPriority("color"), selector).toBe("important");
      expect(override?.style.color, selector).toBe(color);
      expect(styles(selector).color, selector).toBe(color);
    }
  });

  it("preserves warning, error and success semantics above default badge colors", () => {
    mount('<main class="page-surface"><span class="level-pill level-info">Info</span><span class="level-pill level-warning">Warning</span><span class="level-pill level-error">Error</span><span class="status-pill success">Success</span><span class="status-pill danger">Failure</span><span class="status-chip warning">Not success</span></main>');
    const sheet = Array.from(document.styleSheets).at(-1)!;
    const base = Array.from(sheet.cssRules).find((rule) => rule.type === CSSRule.STYLE_RULE && (rule as CSSStyleRule).selectorText.includes(".level-pill")) as CSSStyleRule;
    expect(base.selectorText).toContain(":where(.status-pill");
    for (const [selector, color, background] of [
      [".level-info", "var(--accent-dark)", "var(--workspace-info-bg)"],
      [".level-warning", "var(--warning)", "var(--workspace-warning-bg)"],
      [".level-error", "var(--danger)", "var(--workspace-danger-bg)"],
      [".status-pill.success", "var(--success)", "var(--workspace-success-bg)"],
      [".status-pill.danger", "var(--danger)", "var(--workspace-danger-bg)"],
    ]) {
      expect(styles(selector).color, selector).toBe(color);
      expect(styles(selector).background, selector).toBe(background);
    }
    expect(styles(".status-chip.warning").color).toBe("var(--warning)");
    expect(styles(".status-chip.warning").background).toBe("var(--workspace-warning-bg)");
  });

  it("themes actual resource families and portals outside the app shell", () => {
    const families = ["skill-source-card", "plugin-readiness", "hermes-journey", "openclaw-policy-preview", "error-log-panel", "modal-card", "process-detail-modal", "composer-approval-popover"];
    mount('<div class="app-shell"><main class="page-surface"></main></div>' + families.map((name) => `<div class="${name}">Content</div>`).join(""));
    for (const family of families) {
      const style = styles(`.${family}`);
      expect(style.backgroundImage, family).toBe("none");
      expect(style.boxShadow, family).not.toMatch(/0 0 3[2468]px/);
      expect(style.borderRadius, family).toMatch(/^[0-8]px$/);
    }
  });

  it("preserves the conversation layout, portal controls, and syntax highlighting", () => {
    mount('<div class="app-shell is-conversation-workspace"><div class="page-surface"><section class="conversation-workspace"><div class="chat-panel"><article class="chat-message user">Message</article><form class="chat-composer"><textarea></textarea><button>Send</button></form></div></section></div></div><div class="process-detail-modal"><button class="secondary-action">Close</button></div><div class="bounded-text-block code-block-editor"><div class="code-block-toolbar"><button class="code-tool-button">Copy</button></div><pre><code><span class="hljs-keyword">const</span></code></pre></div>', false);
    const selectors = [".app-shell", ".page-surface", ".chat-panel", ".chat-message", ".chat-composer", "textarea", ".process-detail-modal button", ".code-tool-button", ".hljs-keyword"];
    const snapshot = () => selectors.map((selector) => {
      const style = styles(selector);
      return [style.color, style.background, style.borderRadius, style.padding, style.height, style.boxShadow];
    });
    const before = snapshot();
    addTheme();
    expect(snapshot()).toEqual(before);
    expect(getComputedStyle(document.documentElement).getPropertyValue("--ink").trim()).toBe("#1f2329");
  });

  it("preserves code reader controls outside the conversation too", () => {
    mount('<div class="bounded-text-block code-block-editor"><button class="code-tool-button">Copy</button><pre><code><span class="hljs-keyword">const</span></code></pre></div>', false);
    const before = [styles(".code-tool-button").cssText, styles(".hljs-keyword").color, styles("pre").backgroundColor];
    addTheme();
    expect([styles(".code-tool-button").cssText, styles(".hljs-keyword").color, styles("pre").backgroundColor]).toEqual(before);
  });

  it("loads between the legacy base and the conversation overrides without replacing mobile navigation", () => {
    const main = readFileSync("src/main.tsx", "utf8");
    expect(main.indexOf('import "./workspace-theme.css";')).toBeGreaterThan(main.indexOf('import "./styles.css";'));
    expect(main.indexOf('import "./conversation-workspace.css";')).toBeGreaterThan(main.indexOf('import "./workspace-theme.css";'));
    const style = addStyle(theme);
    function checkScope(rules: CSSRuleList) {
      for (const rule of Array.from(rules)) {
        if (rule.type === CSSRule.STYLE_RULE) {
          const selector = (rule as CSSStyleRule).selectorText;
          if (selector === ":is(.auth-brand-logo, .brand-card img, .brand-lockup img)") continue;
          expect(selector).toMatch(/^(body|:root):not\(:has\(\.conversation-workspace\)\)/);
          expect(selector).not.toMatch(/(^|[ >,])\*/);
        } else if (rule.type === CSSRule.MEDIA_RULE) checkScope((rule as CSSMediaRule).cssRules);
      }
    }
    checkScope(style.sheet!.cssRules);
    const media = Array.from(style.sheet?.cssRules ?? []).filter((rule): rule is CSSMediaRule => rule.type === CSSRule.MEDIA_RULE);
    const mobile = media.find((rule) => rule.conditionText === "(max-width: 980px)");
    expect(mobile).toBeDefined();
    expect(Array.from(mobile!.cssRules).some((rule) => rule.cssText.includes("grid-template-columns: minmax(0, 1fr)"))).toBe(true);
    expect(Array.from(mobile!.cssRules).some((rule) => rule.cssText.includes("translateX"))).toBe(false);
  });
});
