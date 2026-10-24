import { lexer, type Token, type Tokens } from "marked";
import { Check, Copy, Maximize2, Minimize2, WrapText } from "lucide-react";
import { createElement, Fragment, type CSSProperties, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";

import { CodeContent } from "./CodeContent";

function copyWithSelection(text: string) {
  const focused = document.activeElement;
  const selection = document.getSelection();
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
  const inputSelection = focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement
    ? { start: focused.selectionStart, end: focused.selectionEnd, direction: focused.selectionDirection } : null;
  const temporary = document.createElement("textarea");
  temporary.value = text;
  temporary.readOnly = true;
  temporary.tabIndex = -1;
  temporary.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
  document.body.appendChild(temporary);
  try {
    temporary.focus({ preventScroll: true });
    temporary.select();
    if (!document.execCommand?.("copy")) throw new Error("copy failed");
  } finally {
    temporary.remove();
    if (focused instanceof HTMLElement) focused.focus({ preventScroll: true });
    if (inputSelection && (focused instanceof HTMLInputElement || focused instanceof HTMLTextAreaElement)
      && inputSelection.start !== null && inputSelection.end !== null) {
      focused.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction ?? undefined);
    }
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }
}

export async function copyOutputText(text: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Permission failures and non-secure HTTP contexts can still allow gesture-bound copying.
  }
  copyWithSelection(text);
}

function useCopyFeedback(text: string) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const currentText = useRef(text);
  useEffect(() => {
    currentText.current = text;
    setCopied(false);
    setFailed(false);
    return () => clearTimeout(timer.current);
  }, [text]);
  async function copy() {
    try {
      await copyOutputText(text);
      if (currentText.current !== text) return;
      setCopied(true);
      setFailed(false);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
      setFailed(true);
    }
  }
  return { copied, failed, copy };
}

export function BoundedTextBlock({
  copyAriaLabel, label, text, language, shouldCollapse: shouldCollapseOverride,
  collapseLabel = "展开", expandLabel = "收起",
}: {
  copyAriaLabel: string;
  label: string;
  text: string;
  language?: string;
  shouldCollapse?: boolean;
  collapseLabel?: string;
  expandLabel?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [wrapped, setWrapped] = useState(false);
  const { copied, failed, copy } = useCopyFeedback(text);
  const contentId = useId();
  const contentRef = useRef<HTMLPreElement>(null);
  const shouldCollapse = shouldCollapseOverride ?? (text.length > 1200 || text.split("\n").length > 18);
  useEffect(() => {
    if (expanded) contentRef.current?.focus();
  }, [expanded]);
  return (
    <div className={`bounded-text-block${language ? ` code-block-editor${wrapped ? " is-wrapped" : ""}${!shouldCollapse && expanded ? " is-expanded" : ""}` : ""}${shouldCollapse && !expanded ? " is-collapsed" : ""}${shouldCollapse && expanded ? " is-expanded" : ""}`}>
      <div className="bounded-text-block-header">
        <span>{label}</span>
        <div className={language ? "code-block-toolbar" : undefined} role={language ? "toolbar" : undefined} aria-label={language ? `${label}代码工具` : undefined}>
          {language ? <button type="button" className="code-tool-button" aria-label={wrapped ? "不换行" : "换行"}
            title={wrapped ? "关闭自动换行" : "自动换行"} aria-pressed={wrapped} aria-controls={contentId}
            onClick={() => setWrapped((current) => !current)}><WrapText size={16} aria-hidden="true" /></button> : null}
          {shouldCollapse || language ? (
            <button type="button" className={language ? "code-tool-button" : "text-button"} aria-expanded={expanded} aria-controls={contentId}
              aria-label={expanded ? expandLabel : collapseLabel} title={expanded ? expandLabel : collapseLabel}
              onClick={() => setExpanded((current) => !current)}>
              {language ? (expanded ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />) : (expanded ? expandLabel : collapseLabel)}
            </button>
          ) : null}
          <button type="button" className={language ? "code-tool-button" : "text-button"} aria-label={copied ? `已${copyAriaLabel}` : copyAriaLabel}
            title={copied ? "已复制" : copyAriaLabel}
            onClick={() => { void copy(); }}>
            {language ? (copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />) : (copied ? "已复制" : "复制")}
          </button>
          {failed ? <span role="status">复制失败</span> : null}
        </div>
      </div>
      <pre id={contentId} ref={contentRef} tabIndex={0} aria-label={`${label}完整内容`}>
        {language ? <CodeContent text={text} language={language} /> : text}
      </pre>
    </div>
  );
}

function MessageParagraph({ text, children }: { text: string; children?: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const { copied, failed, copy } = useCopyFeedback(text);
  const contentId = useId();
  const lines = text.split("\n");
  const shouldCollapse = text.length > 900 || lines.length > 12;
  const preview = lines.slice(0, 10).join("\n");
  const displayText = shouldCollapse && !expanded
    ? `${preview.slice(0, 900)}${text.length > 900 || lines.length > 10 ? "\n..." : ""}` : text;
  const copyable = shouldCollapse || text.length > 240 || lines.length > 4;
  return (
    <div className={`message-paragraph${shouldCollapse ? " is-collapsible" : ""}${shouldCollapse && !expanded ? " is-collapsed" : ""}`}>
      {copyable ? (
        <div className="message-paragraph-tools" aria-label="消息文本工具">
          {shouldCollapse ? (
            <button type="button" className="message-expand-button" aria-expanded={expanded} aria-controls={contentId}
              onClick={() => setExpanded((current) => !current)}>
              {expanded ? "收起" : "展开全文"}
            </button>
          ) : null}
          <button type="button" className="message-copy-button" aria-label={copied ? "已复制全文" : "复制全文"}
            title={copied ? "已复制" : "复制全文"} onClick={() => { void copy(); }}>
            {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
          </button>
          {failed ? <span role="status">复制失败</span> : null}
        </div>
      ) : null}
      <p id={contentId} className={children !== undefined && shouldCollapse && !expanded ? "message-paragraph-preview" : undefined}>
        {children ?? displayText}
      </p>
    </div>
  );
}

function outputInline(tokens: Token[], table = false): ReactNode {
  return tokens.map((token, index) => {
    if (token.type === "br" || (table && token.type === "html" && /^<br\s*\/?\s*>$/i.test(token.raw))) return <br key={index} />;
    if (token.type === "codespan") return <code key={index} className="output-inline-code">{token.text}</code>;
    if (token.type === "strong") return <strong key={index}>{outputInline(token.tokens ?? [], table)}</strong>;
    if (token.type === "em") return <em key={index}>{outputInline(token.tokens ?? [], table)}</em>;
    if (token.type === "del") return <del key={index}>{outputInline(token.tokens ?? [], table)}</del>;
    if (token.type === "text" && token.tokens) return <Fragment key={index}>{outputInline(token.tokens, table)}</Fragment>;
    // HTML and destinations remain text; only the exact table break token is interpreted.
    return <Fragment key={index}>{token.type === "text" || token.type === "escape" ? token.text : token.raw}</Fragment>;
  });
}

function OutputTable({ token, title, index }: { token: Tokens.Table; title: string; index: number }) {
  const label = `${title}表格 ${index}`;
  return (
    <div className="message-table-wrap" role="region" aria-label={`${label}滚动区域`} tabIndex={0}>
      <table aria-label={label} className="message-table"
        style={{ "--output-column-count": token.header.length } as CSSProperties}>
        <colgroup>{token.header.map((_, column) => <col key={column} />)}</colgroup>
        <thead><tr>{token.header.map((cell, column) => (
          <th key={column} scope="col" style={{ textAlign: token.align[column] ?? "left" }}>{outputInline(cell.tokens, true)}</th>
        ))}</tr></thead>
        <tbody>{token.rows.map((row, rowIndex) => (
          <tr key={rowIndex}>{token.header.map((_, column) => (
            <td key={column} style={{ textAlign: token.align[column] ?? "left" }}>{outputInline(row[column]?.tokens ?? [], true)}</td>
          ))}</tr>
        ))}</tbody>
      </table>
    </div>
  );
}

export function outputFileLanguage(filename: string, mime = "") {
  const extension = filename.toLowerCase().split(".").pop() ?? "";
  const languages: Record<string, string> = {
    js: "javascript", jsx: "jsx", ts: "typescript", tsx: "tsx", py: "python", sh: "bash",
    yml: "yaml", yaml: "yaml", json: "json", html: "html", htm: "html", css: "css",
    sql: "sql", toml: "toml", xml: "xml", cpp: "c++", c: "c", cs: "c#", rs: "rust", go: "go",
    ps1: "powershell", psm1: "powershell", psd1: "powershell",
  };
  return languages[extension] ?? (mime.includes("json") ? "json" : "text");
}

export function isMarkdownOutputFile(filename: string, mime = "") {
  return /\.(md|markdown)$/i.test(filename) || mime.toLowerCase().split(";", 1)[0].trim() === "text/markdown";
}

function tokenizeOutput(text: string): Token[] | null {
  try {
    const tokens = lexer(text, { gfm: true });
    const pending = [{ tokens: tokens as Token[], depth: 0 }];
    while (pending.length) {
      const current = pending.pop()!;
      if (current.depth > 32) return null;
      for (const token of current.tokens) {
        if ("tokens" in token && token.tokens) pending.push({ tokens: token.tokens, depth: current.depth + 1 });
        if (token.type === "list") {
          for (const item of (token as Tokens.List).items) pending.push({ tokens: item.tokens, depth: current.depth + 1 });
        }
        if (token.type === "table") {
          for (const cell of [...(token as Tokens.Table).header, ...(token as Tokens.Table).rows.flat()]) {
            pending.push({ tokens: cell.tokens, depth: current.depth + 1 });
          }
        }
      }
    }
    return tokens;
  } catch {
    return null;
  }
}

export function hasStructuredOutput(text: string): boolean {
  if (outputMachineLanguage(text) !== null) return true;
  function contains(items: Token[]): boolean {
    return items.some((token) => token.type === "code" || token.type === "table"
      || (token.type === "blockquote" && contains(token.tokens ?? []))
      || (token.type === "list" && (token as Tokens.List).items.some((item) => contains(item.tokens))));
  }
  const tokens = text.includes("\n") ? tokenizeOutput(text) : [];
  return tokens !== null && contains(tokens);
}

export function outputMachineLanguage(text: string): "json" | "python" | "text" | null {
  if (/^\s*(?:EXPLICIT_CAPABILITY_CALLS_JSON|UNTRUSTED_CAPABILITY_RESULTS_JSON)\s*=/.test(text)) return "text";
  if (!/^\s*[\[{]/.test(text)) return null;
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value === "object" && value !== null) return "json";
  } catch {
    // Public events may truncate a mapping. Display the exact prefix, never evaluate or repair it.
    const prefix = text.match(/^\s*\{\s*(["'])[^"'\r\n]+\1\s*:/);
    if (prefix) return prefix[1] === "'" ? "python" : "json";
  }
  return null;
}

export function OutputContent({ text, title, format = "markdown", language = "text", copyAriaLabel }: {
  text: string;
  title: string;
  format?: "markdown" | "code" | "plain";
  language?: string;
  copyAriaLabel?: string;
}) {
  const machineLanguage = useMemo(() => format === "markdown" ? outputMachineLanguage(text) : null, [format, text]);
  const tokens = useMemo(() => format === "markdown" && !machineLanguage ? tokenizeOutput(text) : [], [format, machineLanguage, text]);
  if (!text) return null;
  if (machineLanguage) return <BoundedTextBlock label={machineLanguage} language={machineLanguage} text={text} copyAriaLabel={`复制 ${machineLanguage} 代码`} />;
  if (tokens === null) return <BoundedTextBlock label="text" text={text} copyAriaLabel="复制全文" />;
  if (format === "code") return <BoundedTextBlock label={language} language={language} text={text}
    copyAriaLabel={copyAriaLabel ?? `复制 ${language} 代码`} />;
  if (format === "plain") return <MessageParagraph text={text} />;
  let tableIndex = 0;
  function blocks(items: Token[], listItem = false): ReactNode {
    return items.map((token, index) => {
      if (token.type === "space") return null;
      if (token.type === "heading") return createElement(`h${token.depth}`, { key: index, className: "output-heading" }, outputInline(token.tokens ?? []));
      if (token.type === "hr") return <hr key={index} className="output-rule" />;
      if (token.type === "paragraph" || token.type === "text") {
        const inline = token.tokens ?? [token];
        if (listItem) return <p key={index}>{outputInline(inline)}</p>;
        const formatted = inline.some((item) => item.type !== "text" && item.type !== "escape");
        return <MessageParagraph key={index} text={token.raw}>{formatted ? outputInline(inline) : undefined}</MessageParagraph>;
      }
      if (token.type === "code") {
        const codeLanguage = token.lang?.split(/\s+/, 1)[0] || "text";
        return <BoundedTextBlock key={index} label={codeLanguage} language={codeLanguage} text={token.text}
          copyAriaLabel={`复制 ${codeLanguage} 代码`} />;
      }
      if (token.type === "table") return <OutputTable key={index} token={token as Tokens.Table} title={title} index={++tableIndex} />;
      if (token.type === "blockquote") return <blockquote key={index} className="output-content output-quote">{blocks(token.tokens ?? [], listItem)}</blockquote>;
      if (token.type === "list") {
        const list = token as Tokens.List;
        const children = list.items.map((item, itemIndex) => <li key={itemIndex} className="output-content">{blocks(item.tokens, true)}</li>);
        return list.ordered ? <ol key={index} className="output-list" start={typeof list.start === "number" ? list.start : undefined}>{children}</ol>
          : <ul key={index} className="output-list">{children}</ul>;
      }
      return <MessageParagraph key={index} text={token.raw} />;
    });
  }
  return <div className="message-body output-content">{blocks(tokens)}</div>;
}
