const { defineConfig } = require("@playwright/test");
const engine = process.env.DEBUGBRIEF_BROWSER_ENGINE || "chromium";
module.exports = defineConfig({
  testDir: "./tests/browser",
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  retries: 0,
  timeout: 30000,
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: `artifacts/verification/browser-${engine}-results.json` },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:8765",
    browserName: engine,
    launchOptions:
      engine === "chromium" && process.env.DEBUGBRIEF_BROWSER_PATH
        ? {
            executablePath: process.env.DEBUGBRIEF_BROWSER_PATH,
            args: [
              "--enable-webgl",
              "--use-gl=angle",
              "--use-angle=swiftshader",
            ],
          }
        : {},
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    { name: "tablet", use: { viewport: { width: 768, height: 1024 } } },
    {
      name: "mobile",
      use: {
        viewport: { width: 375, height: 812 },
        isMobile: engine !== "firefox",
        hasTouch: true,
      },
    },
  ],
  webServer: {
    command: "python3 scripts/serve_site.py",
    url: "http://127.0.0.1:8765",
    reuseExistingServer: !process.env.CI,
    timeout: 15000,
  },
});
