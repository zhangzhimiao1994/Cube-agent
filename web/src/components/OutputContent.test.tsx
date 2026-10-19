import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OutputContent } from "./OutputContent";

const originalExecCommand = Object.getOwnPropertyDescriptor(document, "execCommand");
afterEach(() => {
  vi.restoreAllMocks();
  if (originalExecCommand) Object.defineProperty(document, "execCommand", originalExecCommand);
  else Reflect.deleteProperty(document, "execCommand");
});

describe("shared output rendering", () => {
  it("recognizes indented c++ and tilde fences as separate semantic code blocks", () => {
    const { container } = render(<OutputContent title="回复" text={
      "前文\n\n  ```c++\n  int main() {}\n  ```\n\n~~~css\n.card { display: grid; }\n~~~\n\n后文"
    } />);
    expect(screen.getByLabelText("c++完整内容").querySelector("code")?.textContent).toBe("int main() {}");
    expect(screen.getByLabelText("css完整内容").querySelector("code")?.textContent).toBe(".card { display: grid; }");
    expect(container.querySelectorAll("pre > code")).toHaveLength(2);
    expect(screen.getByText("前文")).toBeTruthy();
    expect(screen.getByText("后文")).toBeTruthy();
  });

  it("keeps empty headers, escaped pipes and safe multiline table cells intact", () => {
    render(<OutputContent title="回复" text={"| | 说明 |\n| :--- | ---: |\n| a\\|b | first<br>second<br />third |\n| | last |"} />);
    const table = screen.getByRole("table", { name: "回复表格 1" });
    const headers = within(table).getAllByRole("columnheader");
    expect(headers).toHaveLength(2);
    expect(headers[0].textContent).toBe("");
    expect(headers[1].getAttribute("scope")).toBe("col");
    expect(within(table).getByRole("cell", { name: "a|b" })).toBeTruthy();
    const multiline = within(table).getByRole("cell", { name: /first.*second.*third/ });
    expect(multiline.querySelectorAll("br")).toHaveLength(2);
    expect(table.querySelectorAll("col")).toHaveLength(2);
    expect(headers[1].style.textAlign).toBe("right");
  });

  it("retains tables that have a header and no data rows", () => {
    render(<OutputContent title="回复" text={"| 名称 | 状态 |\n| --- | --- |\n\n后文"} />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(2);
    expect(table.querySelectorAll("tbody tr")).toHaveLength(0);
    expect(screen.getByText("后文")).toBeTruthy();
  });

  it("copies and expands multiple code blocks independently without copying fences", async () => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const first = Array.from({ length: 21 }, (_, i) => `  line ${i}: <tag>\\n`).join("\n");
    const second = Array.from({ length: 21 }, (_, i) => `second ${i}`).join("\n");
    render(<OutputContent title="回复" text={`before\n\n\`\`\`python\n${first}\n\`\`\`\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n\`\`\`sql\n${second}\n\`\`\`\n\nafter`} />);
    const pre = screen.getByLabelText("python完整内容");
    expect(pre.querySelector("code")?.textContent).toBe(first);
    const expands = screen.getAllByRole("button", { name: /^展开$/ });
    await user.click(expands[0]);
    expect(expands[0].getAttribute("aria-expanded")).toBe("true");
    expect(expands[1].getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(pre);
    await user.click(screen.getByRole("button", { name: "复制 python 代码" }));
    await waitFor(() => expect(copy).toHaveBeenLastCalledWith(first));
    await user.click(screen.getByRole("button", { name: "复制 sql 代码" }));
    await waitFor(() => expect(copy).toHaveBeenLastCalledWith(second));
    expect(screen.getByText("before")).toBeTruthy();
    expect(screen.getByText("after")).toBeTruthy();
    expect(screen.getByRole("table")).toBeTruthy();
  });

  it("keeps failed copies out of the success state", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("clipboard denied"));
    render(<OutputContent title="回复" text={"```js\nconst value = 1;\n```"} />);
    await user.click(screen.getByRole("button", { name: "复制 js 代码" }));
    expect(screen.queryByRole("button", { name: "已复制 js 代码" })).toBeNull();
  });

  it.each(["missing", "rejected"])("falls back from %s clipboard with exact bytes and restores focus and selection", async (mode) => {
    userEvent.setup();
    if (mode === "missing") vi.spyOn(navigator, "clipboard", "get").mockReturnValue(undefined as unknown as Clipboard);
    else vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("insecure context"));
    const selected: string[] = [];
    const exec = vi.fn(() => {
      selected.push((document.activeElement as HTMLTextAreaElement).value);
      return true;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: exec });
    const text = "  exact <tag>\\n &lt;literal&gt;  \nlast  ";
    const { container } = render(<><input aria-label="原输入" defaultValue="abcdef" /><OutputContent title="回复" format="code" text={text} language="text" /></>);
    const input = screen.getByRole("textbox", { name: "原输入" }) as HTMLInputElement;
    input.focus(); input.setSelectionRange(1, 4);
    const selection = document.getSelection()!;
    const range = document.createRange(); range.selectNodeContents(container.querySelector("code")!);
    selection.removeAllRanges(); selection.addRange(range);
    const originalSelection = selection.toString();
    // Keyboard activation retains the real caller focus so restoration is observable.
    screen.getByRole("button", { name: "复制 text 代码" }).click();
    await waitFor(() => expect(screen.getByRole("button", { name: "已复制 text 代码" })).toBeTruthy());
    expect(selected).toEqual([text]);
    expect(exec).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd]).toEqual([1, 4]);
    expect(selection.toString()).toBe(originalSelection);
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });

  it.each([false, "throw"])("does not report success when fallback returns %s", async (result) => {
    const user = userEvent.setup();
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue(undefined as unknown as Clipboard);
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => {
      if (result === "throw") throw new Error("copy blocked");
      return false;
    }) });
    render(<OutputContent title="回复" format="code" text="exact" language="text" />);
    await user.click(screen.getByRole("button", { name: "复制 text 代码" }));
    expect(screen.queryByRole("button", { name: "已复制 text 代码" })).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("复制失败");
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });

  it("uses the same HTTP-compatible fallback for complete prose without trimming", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue(undefined as unknown as Clipboard);
    let copied = "";
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => {
      copied = (document.activeElement as HTMLTextAreaElement).value;
      return true;
    }) });
    const text = "  " + "正文\\n &lt;literal&gt; ".repeat(30) + "  \n";
    render(<OutputContent title="回复" format="plain" text={text} />);
    await user.click(screen.getByRole("button", { name: "复制全文" }));
    expect(copied).toBe(text);
    expect(screen.getByRole("button", { name: "已复制" })).toBeTruthy();
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });

  it("supports fenced code nested inside lists and quotations", () => {
    render(<OutputContent title="回复" text={"> 前文\n>\n> ```python\n> print(1)\n> ```\n\n- 示例\n\n  ~~~css\n  a { color: red; }\n  ~~~"} />);
    expect(screen.getByLabelText("python完整内容").textContent).toBe("print(1)");
    expect(screen.getByLabelText("css完整内容").textContent).toBe("a { color: red; }");
  });

  it("preserves an unfinished streaming fence and later text on completion", () => {
    const { rerender } = render(<OutputContent title="回复" text={"```c++\nint main() {"} />);
    expect(screen.getByLabelText("c++完整内容").textContent).toBe("int main() {");
    rerender(<OutputContent title="回复" text={"```c++\nint main() {}\n```\n\nfinished"} />);
    expect(screen.getByLabelText("c++完整内容").textContent).toBe("int main() {}");
    expect(screen.getByText("finished")).toBeTruthy();
  });

  it("keeps raw HTML, unsafe URLs and code injection as inert text", () => {
    const attack = '<img src="https://outside.invalid/x" onerror="window.PWNED=1"><script>window.PWNED=1</script>';
    const { container } = render(<OutputContent title="回复" text={
      `${attack}\n\n[x](javascript:alert(1)) ![remote](https://outside.invalid/p.png)\n\n| A | B |\n|---|---|\n| <br onclick="alert(1)"> | <svg onload="alert(1)"> |\n\n\`\`\`html\n${attack}\n\`\`\``
    } />);
    expect(container.querySelectorAll("script,img,iframe,svg,a")).toHaveLength(0);
    expect(screen.getByLabelText("html完整内容").querySelector("code")?.textContent).toBe(attack);
    expect(container.textContent).toContain('onclick="alert(1)"');
    expect(container.textContent).toContain("javascript:alert(1)");
  });

  it("does not globally decode literal newlines, entities or escaped fences", () => {
    const text = "literal \\n &lt;script&gt; \\`\\`\\`python";
    const { container } = render(<OutputContent title="回复" text={text} />);
    expect(container.querySelector("pre")).toBeNull();
    expect(container.textContent).toContain("\\n");
    expect(container.textContent).toContain("&lt;script&gt;");
    expect(container.querySelector("script")).toBeNull();
  });

  it.each([128, 2048])("falls back to complete safe bounded text at depth %s without recursive rendering", async (depth) => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const text = "> ".repeat(depth) + "text <script>window.PWNED=1</script>";
    const { container } = render(<OutputContent title="回复" text={text} />);
    expect(screen.getByLabelText("text完整内容").textContent).toBe(text);
    expect(container.querySelector("script")).toBeNull();
    await user.click(screen.getByRole("button", { name: "复制全文" }));
    await waitFor(() => expect(copy).toHaveBeenCalledWith(text));
  });
});
