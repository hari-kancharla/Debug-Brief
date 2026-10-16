const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");
const axe = require.resolve("axe-core/axe.min.js");
const fixture = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "../../site/data/sample-pr.json"),
    "utf8",
  ),
);
const {
  validateReport,
  formatDuration,
  MAX_BYTES,
} = require("../../site/explorer.js");
const clone = () => JSON.parse(JSON.stringify(fixture));
async function upload(page, data, name = "report.json") {
  await page.locator("#report-file").setInputFiles({
    name,
    mimeType: "application/json",
    buffer: Buffer.from(typeof data === "string" ? data : JSON.stringify(data)),
  });
}

// Generated reports and the Explorer intentionally restrict script execution.
// Keep trace instrumentation out of those documents; retain production checks.
test.use({ trace: "off" });
for (const route of [
  "index.html",
  "docs.html",
  "explorer.html?sample=pr",
  "sample-report.html",
  "sample-handoff.html",
  "sample-incident.html",
]) {
  test(`${route}: rendering, assets, accessibility and overflow`, async ({
    page,
    browserName,
  }, testInfo) => {
    const errors = [];
    const missing = [];
    const remote = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 400) missing.push(response.url());
    });
    page.on("request", (request) => {
      if (!request.url().startsWith("http://127.0.0.1:8765/"))
        remote.push(request.url());
    });
    await page.addInitScript(() => {
      window.__policyViolations = [];
      document.addEventListener("securitypolicyviolation", (event) => {
        window.__policyViolations.push(event.effectiveDirective);
      });
    });
    await page.goto("/" + route);
    if (route.includes("sample="))
      await expect(page.locator("#import-status")).toContainText("Opened");
    if (route === "index.html") {
      await expect(page.locator("#hero-title")).toHaveText("Red, then green.");
      await expect(page.locator("#panel-pr .report-sheet")).toContainText(
        "expired cache",
      );
    }
    await expect(page.locator("h1")).toBeVisible();
    const overflow = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: innerWidth,
    }));
    expect(overflow.document).toBeLessThanOrEqual(overflow.viewport + 1);
    // Audit injection is test instrumentation; production console is asserted
    // before the audit so sandbox-denied audit probes are not product errors.
    // WebKit's native <select> shadow stylesheet emits this console warning
    // even in a minimal strict-CSP page with no application scripts or styles.
    // Require the exact single native warning and no page inline styles. Any
    // additional violation or error must still fail.
    const nativeSelectWarning =
      "Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline' does not appear in the style-src directive of the Content Security Policy.";
    const nativeSelect =
      browserName === "webkit" && route.startsWith("explorer.html");
    expect(await page.evaluate(() => window.__policyViolations)).toEqual(
      nativeSelect ? ["style-src-attr"] : [],
    );
    if (nativeSelect)
      expect(await page.locator("style, [style]").count()).toBe(0);
    expect(
      nativeSelect
        ? errors.filter((message) => message !== nativeSelectWarning)
        : errors,
    ).toEqual([]);
    await page.evaluate(fs.readFileSync(axe, "utf8"));
    const audit = await page.evaluate(async () =>
      axe.run(document, {
        iframes: false,
        runOnly: {
          type: "tag",
          values: [
            "wcag2a",
            "wcag2aa",
            "wcag21a",
            "wcag21aa",
            "wcag22a",
            "wcag22aa",
          ],
        },
      }),
    );
    expect(
      audit.violations.map((item) => ({
        id: item.id,
        targets: item.nodes.map((node) => node.target),
      })),
    ).toEqual([]);
    expect(missing).toEqual([]);
    expect(
      remote.filter((url) => !url.startsWith("https://cdn.jsdelivr.net/")),
    ).toEqual([]);
    if (
      [
        "index.html",
        "docs.html",
        "explorer.html?sample=pr",
        "sample-report.html",
      ].includes(route)
    ) {
      const name = route.split(".")[0];
      if (route === "index.html") {
        await page.locator("#panel-pr").scrollIntoViewIfNeeded();
        await page.evaluate(() => window.scrollTo(0, 0));
      }
      await page.screenshot({
        path: testInfo.outputPath(`${name}.png`),
        fullPage: true,
      });
    }
  });
}

test("report modes and installation copy", async ({
  page,
  context,
  browserName,
}) => {
  test.skip(
    browserName !== "chromium",
    "Clipboard permission grants are Chromium-specific; native copy is checked separately.",
  );
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await page.locator("#tab-handoff").click();
  await expect(page.locator("#panel-handoff")).toBeVisible();
  await expect(page.locator("#panel-pr")).toBeHidden();
  await expect(page.locator("#panel-handoff")).toContainText("expired cache");
  await page.locator("#tab-incident").click();
  await expect(page.locator("#panel-incident .verdict")).toContainText(
    "unverified",
  );
  for (const [provider, command] of Object.entries({
    pipx: "pipx install debugbrief",
    uv: "uv tool install debugbrief",
    pip: "python -m pip install debugbrief",
  })) {
    await page.locator(`[data-install-provider="${provider}"]`).click();
    await expect(page.locator("[data-install-command]")).toHaveText(command);
    await page
      .getByRole("button", { name: "Copy install command", exact: true })
      .click();
    await expect(page.locator("[data-toast]")).toHaveText("Copied.");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
      command,
    );
  }
});

test("tab keyboard pattern and skip link", async ({ page, browserName }) => {
  await page.goto("/");
  // macOS WebKit uses Option-Tab for links with its default keyboard settings.
  await page.keyboard.press(browserName === "webkit" ? "Alt+Tab" : "Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.locator("#tab-pr").focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#tab-handoff")).toBeFocused();
  await expect(page.locator("#tab-handoff")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.keyboard.press("End");
  await expect(page.locator("#tab-incident")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(page.locator("#tab-pr")).toBeFocused();
});

test("navigation works on desktop and mobile", async ({ page }) => {
  await page.goto("/");
  await page
    .locator(".primary-nav")
    .getByRole("link", { name: "Docs" })
    .click();
  await expect(page).toHaveURL(/docs.html$/);
  const menu = page.locator(".docs-menu");
  if (
    (await menu.getAttribute("open")) === null &&
    (await menu.locator("summary").isVisible())
  )
    await menu.locator("summary").click();
  await page
    .locator(".docs-nav a")
    .filter({ hasText: "evidence explorer" })
    .click();
  await expect(page.locator("#explorer")).toBeInViewport();
});

test("documentation search, empty state, slash and Escape", async ({
  page,
}) => {
  await page.goto("/docs.html");
  await page.keyboard.press("/");
  const search = page.locator("[data-doc-filter-input]");
  await expect(search).toBeFocused();
  const sectionCount = await page.locator("[data-doc-item]").count();
  await search.fill("nonesuch xyz");
  await expect(page.locator("[data-doc-filter-status]")).toHaveText(
    "No matching headings.",
  );
  await expect(page.locator("[data-doc-filter-results] a")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-doc-filter]")).not.toBeVisible();
  await expect(page.locator("[data-doc-item]:visible")).toHaveCount(
    sectionCount,
  );
  await page.locator("[data-doc-filter-open]").click();
  await search.fill("verification");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator("[data-doc-filter-results] a")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#checks$/);
  await expect(page.locator("#checks")).toBeInViewport();
});

test("empty, import, command inspection, filter, copy, clear, reload privacy", async ({
  page,
  context,
  browserName,
}) => {
  test.skip(
    browserName !== "chromium",
    "Clipboard permission grants are Chromium-specific; native copy is checked separately.",
  );
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/explorer.html");
  await expect(
    page.getByRole("heading", { name: "No report open", exact: true }),
  ).toBeVisible();
  await upload(page, fixture);
  await expect(page.locator("#import-status")).toContainText(
    "Opened report.json",
  );
  await page.locator("#tab-commands").click();
  await expect(page.locator(".command-record")).toHaveCount(3);
  await page.getByRole("combobox", { name: "Outcome" }).selectOption("failed");
  await expect(page.locator(".command-record")).toHaveCount(1);
  await page.locator(".command-record summary").click();
  await expect(page.locator(".command-output pre")).toContainText(
    "AssertionError",
  );
  await page
    .getByRole("combobox", { name: "Outcome" })
    .selectOption("verification");
  await expect(page.locator(".command-record")).toHaveCount(2);
  await page
    .getByRole("searchbox", { name: "Search command or output" })
    .fill("ruff");
  await expect(page.locator(".command-record")).toHaveCount(1);
  await page.locator("#tab-changes").click();
  await expect(page.locator("#view-changes")).toContainText(
    "tests/test_cache.py",
  );
  await page.locator("#tab-notes").click();
  await expect(page.locator("#view-notes")).toContainText("expiry boundary");
  await page.locator("#tab-evidence").click();
  await page
    .getByRole("button", { name: "Copy reproduce / recorded failure command" })
    .click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    fixture.reproduce_command,
  );
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  await page.locator("#clear-report").click();
  await expect(
    page.getByRole("heading", { name: "No report open", exact: true }),
  ).toBeVisible();
  await expect(page.locator("#choose-report")).toBeFocused();
  await upload(page, fixture);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "No report open", exact: true }),
  ).toBeVisible();
});

test("all samples and late failure remain unverified", async ({ page }) => {
  await page.goto("/explorer.html");
  for (const mode of ["pr", "handoff", "incident"]) {
    await page.locator(`[data-sample="${mode}"]`).click();
    await expect(page.locator("#import-status")).toContainText(
      `illustrative ${mode} fixture`,
    );
  }
  await expect(page.locator("#view-overview")).toContainText(
    "Reported unverified",
  );
  await expect(page.locator("#view-overview")).toContainText("still failing");
  await expect(page.locator("#view-overview")).toContainText(
    "Capture was incomplete",
  );
  await page.locator("#tab-evidence").focus();
  await page.keyboard.press("ArrowUp");
  await expect(page.locator("#tab-timeline")).toBeFocused();
  await expect(page.locator("#view-timeline")).toBeVisible();
});

test("malformed, incompatible, contradictory and oversized imports preserve last report", async ({
  page,
}) => {
  await page.goto("/explorer.html?sample=pr");
  await expect(page.locator("#import-status")).toContainText("Opened");
  for (const data of [
    "{bad",
    "null",
    "[]",
    "{}",
    { ...fixture, schema_version: 99 },
    { ...fixture, evidence: { state: "verified" } },
    {
      ...fixture,
      commands: [{ command: "test", status: "passed", exit_code: 1 }],
    },
    { ...fixture, notes: [{}] },
    { ...fixture, timeline: Array(2001).fill({ text: "x" }) },
  ]) {
    await upload(page, data);
    await expect(page.locator("#import-status")).toHaveAttribute(
      "data-error",
      "true",
    );
    await expect(page.locator("#view-overview")).toContainText(fixture.title);
  }
  await upload(page, " ".repeat(MAX_BYTES + 1), "large.json");
  await expect(page.locator("#import-status")).toContainText("5 MiB");
});

test("legacy and incomplete reports expose omissions without invented command history", async ({
  page,
}) => {
  const legacy = clone();
  delete legacy.commands;
  delete legacy.schema_version;
  delete legacy.git;
  delete legacy.counts;
  await page.goto("/explorer.html");
  await upload(page, legacy);
  await expect(page.locator("#import-status")).toContainText("missing fields");
  await expect(page.locator("#view-overview")).toContainText(
    "commands was not included",
  );
  await page.locator("#tab-commands").click();
  await expect(page.locator("#view-commands")).toContainText(
    "older report has no structured command history",
  );
  await expect(page.locator(".command-record")).toHaveCount(0);
});

test("malicious imported text cannot create markup or network traffic", async ({
  page,
}) => {
  const report = clone();
  const payload =
    '<img src="https://example.invalid/steal" onerror="window.__injected=1"><script>window.__injected=1</script>';
  report.title = payload;
  report.notes = [payload];
  report.commands[0].stdout_preview = payload;
  report.commands[0].working_directory = payload;
  report.verify_working_directory = payload;
  report.changed_files[0].path = payload;
  const requests = [];
  await page.goto("/explorer.html");
  await page.waitForLoadState("networkidle");
  page.on("request", (request) => requests.push(request.url()));
  await upload(page, report, "<img src=x>.json");
  await expect(page.locator("#view-overview h2")).toHaveText(payload);
  await page.locator("#tab-notes").click();
  await expect(page.locator("#view-notes")).toContainText(payload);
  expect(
    await page
      .locator(
        ".explorer-content img,.explorer-content script,.explorer-content iframe",
      )
      .count(),
  ).toBe(0);
  expect(await page.evaluate(() => window.__injected)).toBeUndefined();
  expect(requests).toEqual([]);
});

test("drag and drop loads a report", async ({ page }) => {
  await page.goto("/explorer.html");
  const transfer = await page.evaluateHandle((json) => {
    const data = new DataTransfer();
    data.items.add(
      new File([json], "dropped.json", { type: "application/json" }),
    );
    return data;
  }, JSON.stringify(fixture));
  await page
    .locator("#drop-zone")
    .dispatchEvent("drop", { dataTransfer: transfer });
  await expect(page.locator("#import-status")).toContainText(
    "Opened dropped.json",
  );
});

test("reduced motion and 200 percent zoom reflow", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  expect(
    await page.evaluate(
      () => getComputedStyle(document.documentElement).scrollBehavior,
    ),
  ).toBe("auto");
  // Doubling CSS pixel size approximates browser zoom reflow; WCAG requires
  // content remain usable at a 320 CSS-pixel viewport as well.
  await page.setViewportSize({ width: 320, height: 900 });
  for (const route of [
    "index.html",
    "docs.html",
    "explorer.html?sample=incident",
    "sample-report.html",
  ]) {
    await page.goto("/" + route);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(321);
  }
});

test("validator limits nesting and rejects invalid result shapes", () => {
  expect(validateReport(fixture).omissions).toEqual([]);
  let nested = "end";
  for (let i = 0; i < 18; i++) nested = { child: nested };
  expect(() => validateReport({ ...fixture, extra: nested })).toThrow(
    /complex/,
  );
  expect(() =>
    validateReport({ ...fixture, counts: { commands: -1 } }),
  ).toThrow(/numeric/);
  expect(() =>
    validateReport({ ...fixture, notes: ["x".repeat(200001)] }),
  ).toThrow(/character limit/);
});

test("generated report output is visible when printing", async ({ page }) => {
  await page.goto("/sample-report.html");
  await expect(page.locator(".record-body").first()).toBeHidden();
  await page.emulateMedia({ media: "print" });
  await expect(page.locator(".record-body").first()).toBeVisible();
  await expect(page.locator(".record-body").first()).toContainText(
    "AssertionError",
  );
});

test("content reflows at the layout width of 200 percent browser zoom", async ({
  page,
}) => {
  // A 1440-pixel window at 200% zoom has a 720 CSS-pixel layout viewport.
  await page.setViewportSize({ width: 720, height: 500 });
  for (const route of [
    "index.html",
    "docs.html",
    "explorer.html?sample=pr",
    "sample-report.html",
  ]) {
    await page.goto("/" + route);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(721);
    await expect(page.locator("h1")).toBeVisible();
  }
});

test("site works under a GitHub Pages repository prefix", async ({ page }) => {
  await page.route("**/Debug-Brief/**", async (route) => {
    const url = new URL(route.request().url());
    url.pathname = url.pathname.replace(/^\/Debug-Brief\//, "/");
    const response = await route.fetch({ url: url.href });
    await route.fulfill({ response });
  });
  await page.goto("/Debug-Brief/index.html");
  await expect(page.locator("#hero-title")).toHaveText("Red, then green.");
  await page
    .locator(".primary-nav")
    .getByRole("link", { name: "Docs" })
    .click();
  await expect(page).toHaveURL(/\/Debug-Brief\/docs.html$/);
  await page.goto("/Debug-Brief/explorer.html?sample=pr");
  await expect(page.locator("#import-status")).toContainText("Opened");
  // Finish in-flight prefix rewrites before the test disposes their responses.
  await page.unrouteAll({ behavior: "wait" });
});

test("actual duration precision stays readable without modifying imported evidence", async ({
  page,
}) => {
  const report = clone();
  report.red_to_green.window_seconds = 46.74799990653992;
  report.commands[0].duration_seconds = 0.418000000004;
  report.commands[1].duration_seconds = 77.217;
  report.commands[2].duration_seconds = 0.0000001;
  await page.goto("/explorer.html");
  await upload(page, report);
  await expect(page.locator("#view-overview")).toContainText("46.748s");
  await expect(page.locator("#view-overview")).not.toContainText(
    "46.74799990653992s",
  );
  await page.locator("#tab-commands").click();
  for (const [index, expected] of [
    [0, "0.418s"],
    [1, "1m 17s"],
    [2, "<0.001s"],
  ]) {
    await page.locator(".command-record summary").nth(index).click();
    await expect(page.locator(".command-output").nth(index)).toContainText(
      expected,
    );
  }
  expect(report.red_to_green.window_seconds).toBe(46.74799990653992);
  expect(formatDuration(0)).toBe("0s");
  expect(formatDuration(59.9999)).toBe("1m 0s");
  expect(formatDuration(3600.1)).toBe("1h 0m 0s");
  expect(formatDuration(NaN)).toBe("Not recorded");
});

test("wide reports use the page and separate timeline dates from event text", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/sample-incident.html");
  const report = await page.locator("main").boundingBox();
  expect(report.width).toBeGreaterThanOrEqual(1750);
  expect(report.x).toBeLessThanOrEqual(64);
  const time = await page.locator(".timeline .time").first().boundingBox();
  const text = await page
    .locator(".timeline .event-text")
    .first()
    .boundingBox();
  expect(text.x).toBeGreaterThan(time.x + time.width);
  expect(Math.abs(time.y - text.y)).toBeLessThanOrEqual(3);
  await page.setViewportSize({ width: 375, height: 812 });
  const mobileTime = await page
    .locator(".timeline .time")
    .first()
    .boundingBox();
  const mobileText = await page
    .locator(".timeline .event-text")
    .first()
    .boundingBox();
  expect(mobileText.y).toBeGreaterThan(mobileTime.y);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(375);
});

test("working directories stay attached to checks and reproduction instructions", async ({
  page,
}) => {
  const report = clone();
  report.commands[0].working_directory = "packages/cache";
  report.commands[1].working_directory = "packages/cache";
  report.commands[2].working_directory = "packages/api";
  report.reproduce_working_directory = "packages/cache";
  report.verify_working_directory = "packages/cache";
  report.red_to_green.working_directory = "packages/cache";
  report.verification[0].working_directory = "packages/cache";
  report.verification[1].working_directory = "packages/api";
  await page.goto("/explorer.html");
  await upload(page, report);
  await expect(page.locator("#view-overview")).toContainText(
    "Working directory: packages/cache",
  );
  await expect(page.locator("#view-overview")).toContainText(
    "Working directory: packages/api",
  );
  await page.locator("#tab-commands").click();
  await page.locator(".command-record summary").nth(0).click();
  await page.locator(".command-record summary").nth(2).click();
  await expect(page.locator(".command-output").nth(0)).toContainText(
    "packages/cache",
  );
  await expect(page.locator(".command-output").nth(2)).toContainText(
    "packages/api",
  );
  await page.locator("#tab-evidence").click();
  await expect(
    page.getByText("Run from: packages/cache", { exact: true }),
  ).toHaveCount(2);
  delete report.commands[0].working_directory;
  await upload(page, report);
  await page.locator("#tab-commands").click();
  await page.locator(".command-record summary").nth(0).click();
  await expect(page.locator(".command-output").nth(0)).toContainText(
    "Not recorded",
  );
});

test("search and filter boundaries have non-text contrast", async ({
  page,
}) => {
  const routes = [
    ["/docs.html", ".filter-dialog input"],
    [
      "/explorer.html?sample=pr",
      ".command-controls input, .command-controls select",
    ],
  ];
  for (const theme of ["light", "dark"])
    for (const [route, selector] of routes) {
      await page.goto(route);
      if ((await page.locator("html").getAttribute("data-theme")) !== theme)
        await page.locator("[data-theme-toggle]").click();
      if (route.includes("explorer")) {
        await expect(page.locator("#import-status")).toContainText("Opened");
        await page.locator("#tab-commands").click();
      }
      if (route.includes("docs"))
        await page.locator("[data-doc-filter-open]").click();
      const ratios = await page.locator(selector).evaluateAll((controls) => {
        const luminance = (color) => {
          const rgb = color
            .match(/[\d.]+/g)
            .slice(0, 3)
            .map(Number);
          const linear = rgb.map((v) => {
            v /= 255;
            return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
          });
          return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
        };
        return controls.map((control) => {
          const style = getComputedStyle(control);
          let adjacent = control.parentElement;
          while (
            adjacent &&
            getComputedStyle(adjacent).backgroundColor === "rgba(0, 0, 0, 0)"
          )
            adjacent = adjacent.parentElement;
          const colors = [
            style.backgroundColor,
            getComputedStyle(adjacent).backgroundColor,
          ];
          const border = luminance(style.borderTopColor);
          return colors.map((color) => {
            const background = luminance(color);
            return (
              (Math.max(border, background) + 0.05) /
              (Math.min(border, background) + 0.05)
            );
          });
        });
      });
      expect(ratios.length).toBeGreaterThan(0);
      for (const sides of ratios)
        for (const ratio of sides) expect(ratio).toBeGreaterThanOrEqual(3);
    }
});

test("theme persists while imported report contents do not", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/explorer.html");
  await upload(page, fixture);
  await page.locator("[data-theme-toggle]").click();
  await expect(page.locator("[data-theme-toggle]")).toHaveText("dark");
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute(
    "content",
    "#0D0D0C",
  );
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.getByRole("heading", { name: "No report open", exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([
    "debugbrief-theme",
  ]);
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
});

test("Explorer policy blocks external connections, inline handlers and scripts", async ({
  page,
}) => {
  await page.goto("/explorer.html");
  await page.waitForLoadState("networkidle");
  const result = await page.evaluate(async () => {
    window.__policyProbe = 0;
    const script = document.createElement("script");
    script.textContent = "window.__policyProbe = 1";
    document.body.append(script);
    const button = document.createElement("button");
    button.setAttribute("onclick", "window.__policyProbe = 2");
    document.body.append(button);
    button.click();
    let blocked = false;
    try {
      await fetch("https://example.invalid/report-upload", {
        method: "POST",
        body: "test-only",
      });
    } catch {
      blocked = true;
    }
    script.remove();
    button.remove();
    return { ran: window.__policyProbe, blocked };
  });
  expect(result).toEqual({ ran: 0, blocked: true });
  await page.locator('[data-sample="pr"]').click();
  await expect(page.locator("#import-status")).toContainText("Opened");
});

test("hero fits real desktop window heights with loaded fonts", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop window geometry.");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  for (const [width, height] of [
    [1885, 840],
    [1920, 960],
    [1440, 780],
    [1440, 900],
    [1280, 720],
    [1366, 650],
  ]) {
    await page.setViewportSize({ width, height });
    await expect(page.locator("[data-stage-replay]")).toBeEnabled();
    const bounds = await page.locator(".hero-run").boundingBox();
    expect(bounds.y + bounds.height, `${width}x${height}`).toBeLessThanOrEqual(
      height,
    );
  }
});

test("runner cells keep their height and notes work from the keyboard", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "desktop",
    "Fine-pointer hover coverage.",
  );
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator(".runner-cell")).toHaveCount(24);
  await expect(page.locator(".runner-cell button")).toHaveCount(0);
  for (const width of [375, 390, 768, 1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 });
    const grid = page.locator(".runner-grid");
    const before = await grid.boundingBox();
    await page.locator('[data-runner="npm/pnpm/yarn test"]').hover();
    const after = await grid.boundingBox();
    expect(Math.abs(after.height - before.height)).toBeLessThan(0.1);
    await expect(page.locator(".runner-cell").last()).toHaveText("--verify");
    await expect(page.locator(".runner-cell").last()).not.toHaveAttribute(
      "data-runner",
    );
  }
  const note = page.locator("#panel-pr [data-strip-note]").first();
  await note.focus();
  await note.press("Escape");
  await expect(page.locator("#strip-note-pr")).toBeHidden();
  for (const key of ["Enter", "Space"]) {
    await note.press(key);
    await expect(page.locator("#strip-note-pr")).toContainText(
      "expiry boundary",
    );
    await note.press("Escape");
  }
});

test("blocked animation CDN keeps every action enabled", async ({ page }) => {
  await page.route("https://cdn.jsdelivr.net/npm/gsap@*/**", (route) =>
    route.abort(),
  );
  await page.goto("/");
  await expect(page.locator("[data-stage-replay]")).toBeEnabled();
  await expect(page.locator("button:disabled")).toHaveCount(0);
  await page.locator("[data-install-provider=uv]").click();
  await expect(page.locator("[data-install-command]")).toHaveText(
    "uv tool install debugbrief",
  );
  await page.locator("#tab-incident").click();
  await expect(page.locator("#panel-incident")).toBeVisible();
});

test("WebGL draws a still final frame for reduced motion", async ({
  page,
  browserName,
}, testInfo) => {
  test.skip(
    browserName !== "chromium" || testInfo.project.name !== "desktop",
    "Software WebGL coverage in Chromium.",
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(page.locator(".hero-stage")).toHaveClass(/has-webgl/, {
    timeout: 15000,
  });
  const canvas = page.locator(".hero-stage > canvas");
  await expect(canvas).toBeVisible();
  await expect(page.locator(".hero-stage")).toHaveAttribute(
    "data-field-state",
    "2",
  );
  const before = await canvas.screenshot();
  // Sample at two different animation-frame boundaries, without changing state.
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  expect(await canvas.screenshot()).toEqual(before);
});

test("each Explorer tab starts with a level-two heading", async ({ page }) => {
  await page.goto("/explorer.html?sample=pr");
  await expect(page.locator("#import-status")).toContainText("Opened");
  for (const name of [
    "overview",
    "commands",
    "changes",
    "notes",
    "timeline",
    "evidence",
  ]) {
    await page.locator(`#tab-${name}`).click();
    const headings = page.locator(`#view-${name} :is(h2,h3,h4)`);
    await expect(headings.first()).toBeVisible();
    expect(await headings.first().evaluate((node) => node.tagName)).toBe("H2");
    if (name === "commands") {
      await page.locator(".command-record summary").first().click();
      await expect(page.locator(".command-output h3").first()).toBeVisible();
      await expect(page.locator(".command-output h4")).toHaveCount(0);
    }
  }
});
