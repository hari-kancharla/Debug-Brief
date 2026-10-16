// Reproduce local Lighthouse measurements. Requires the loopback preview server.
// DEBUGBRIEF_BROWSER_PATH can select an isolated Chrome for Testing executable.
import fs from "node:fs/promises";
import lighthouse from "lighthouse";
import { launch } from "chrome-launcher";

const chrome = await launch({
  chromePath: process.env.DEBUGBRIEF_BROWSER_PATH || undefined,
  chromeFlags: ["--headless", "--no-sandbox"],
});
try {
  const scores = {};
  for (const mode of ["mobile", "desktop"]) {
    const config =
      mode === "desktop"
        ? (await import("lighthouse/core/config/desktop-config.js")).default
        : undefined;
    const result = await lighthouse(
      "http://127.0.0.1:8765/index.html",
      {
        port: chrome.port,
        logLevel: "error",
        output: "json",
        onlyCategories: [
          "performance",
          "accessibility",
          "best-practices",
          "seo",
        ],
      },
      config,
    );
    await fs.mkdir("artifacts/verification", { recursive: true });
    await fs.writeFile(
      `artifacts/verification/lighthouse-${mode}.json`,
      result.report,
    );
    scores[mode] = Object.fromEntries(
      Object.entries(result.lhr.categories).map(([key, value]) => [
        key,
        Math.round(value.score * 100),
      ]),
    );
  }
  await fs.writeFile(
    "artifacts/verification/lighthouse-scores.json",
    JSON.stringify(scores, null, 2) + "\n",
  );
  console.log(JSON.stringify(scores));
} finally {
  await chrome.kill();
}
