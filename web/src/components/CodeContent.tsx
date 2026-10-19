import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { type ReactNode, useMemo } from "react";

import "./CodeContent.css";

const highlighter = createLowlight({ bash, c, cpp, csharp, css, go, ini, javascript, json, powershell, python, rust, sql, typescript, xml, yaml });
highlighter.registerAlias({ javascript: ["jsx"], typescript: ["tsx"], powershell: ["ps1", "psm1", "psd1", "pwsh"], ini: ["toml"] });
type HighlightNode = ReturnType<typeof highlighter.highlight>["children"][number];

function renderTokens(nodes: HighlightNode[], depth = 0): ReactNode {
  if (depth > 32) throw new Error("highlight nesting limit");
  return nodes.map((node, index) => {
    if (node.type === "text") return node.value;
    if (node.type !== "element" || node.tagName !== "span") throw new Error("unexpected highlight token");
    const classes = node.properties.className;
    const className = Array.isArray(classes) ? classes.filter((value) => typeof value === "string" && /^[\w-]+$/.test(value)).join(" ") : undefined;
    return <span key={index} className={className}>{renderTokens(node.children, depth + 1)}</span>;
  });
}

export function CodeContent({ text, language }: { text: string; language: string }) {
  const content = useMemo(() => {
    const name = language.toLowerCase();
    // Keep unusually large/unsupported output readable without guessing a language.
    if (text.length > 100_000 || !highlighter.registered(name)) return text;
    try {
      return renderTokens(highlighter.highlight(name, text).children);
    } catch {
      return text;
    }
  }, [language, text]);
  return <code className="hljs" data-language={language}>{content}</code>;
}
