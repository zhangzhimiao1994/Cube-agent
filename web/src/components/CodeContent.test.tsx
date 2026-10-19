import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CodeContent } from "./CodeContent";

describe("safe code highlighting", () => {
  it.each([
    ["javascript", '  const message = "<tag>\\n";  \n'],
    ["tsx", 'const count: number = 1;\nconst element = <span>{count}</span>;'],
    ["python", 'def greet(name):\n    return "hi " + name\n'],
    ["powershell", '$name = "node"\nGet-Process -Name $name | Select-Object -First 1'],
    ["pwsh", '$value = 42\nWrite-Host "value: $value"'],
    ["c++", 'int main() { return 0; }'],
    ["json", '{"name":"sample","count":42,"ready":true}'],
    ["html", '<button class="example">Hello</button>'],
  ])("highlights %s while preserving source text exactly", (language, text) => {
    const { container } = render(<CodeContent text={text} language={language} />);
    const code = container.querySelector("code")!;
    expect(code.textContent).toBe(text);
    expect(code.querySelectorAll('span[class*="hljs-"]').length).toBeGreaterThan(0);
    expect(code.getAttribute("data-language")).toBe(language);
  });

  it("renders only safe spans and text for HTML injection payloads", () => {
    const text = '</code><script>window.PWNED=1</script><img src=x onerror="alert(1)"><a href="javascript:alert(1)">x</a>';
    const { container } = render(<CodeContent text={text} language="html" />);
    expect(container.querySelector("code")?.textContent).toBe(text);
    expect(container.querySelectorAll("script,img,a,iframe")).toHaveLength(0);
    expect(container.querySelector("code")!.querySelectorAll("span").length).toBeGreaterThan(0);
    for (const span of container.querySelectorAll("code span")) {
      expect(span.getAttributeNames()).toEqual(["class"]);
    }
  });

  it.each(["unknown-language", "text", 'unknown" onclick="alert(1)'])("keeps %s as exact inert plain text without autodetection", (language) => {
    const text = '  const literal = "&lt;script&gt;\\n";  \n\n';
    const { container } = render(<CodeContent text={text} language={language} />);
    const code = container.querySelector("code")!;
    expect(code.textContent).toBe(text);
    expect(code.children).toHaveLength(0);
    expect(code.getAttributeNames()).not.toContain("onclick");
  });

  it("updates highlighted tokens when streaming source or language changes", () => {
    const { container, rerender } = render(<CodeContent language="javascript" text="const count = 1;" />);
    expect(container.querySelector(".hljs-keyword")?.textContent).toBe("const");
    rerender(<CodeContent language="powershell" text={'$count = 2\nWrite-Host "done"'} />);
    expect(container.querySelector("code")?.textContent).toBe('$count = 2\nWrite-Host "done"');
    expect(container.querySelector(".hljs-variable")?.textContent).toBe("$count");
    expect(screen.queryByText("const")).toBeNull();
  });
});
