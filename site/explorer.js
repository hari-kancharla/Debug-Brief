/* JSON is untrusted. Validate bounded input; construct DOM exclusively as text.
   No report URLs, markup, styles or commands are ever executed or fetched. */
(() => {
  "use strict";
  const MAX_BYTES = 5 * 1024 * 1024;
  const MAX_ROWS = 2000;
  const states = new Set([
    "passed",
    "failed",
    "timed_out",
    "error",
    "interrupted",
    "broken_pipe",
  ]);
  const isObject = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  function validateReport(value) {
    if (!isObject(value)) throw new Error("Expected a JSON report object.");
    let nodes = 0;
    const queue = [[value, 0]];
    while (queue.length) {
      const [entry, depth] = queue.pop();
      if (++nodes > 50000 || depth > 16)
        throw new Error("Report is too complex to inspect safely.");
      if (typeof entry === "string" && entry.length > 200000)
        throw new Error("A report field exceeds the 200,000 character limit.");
      if (entry && typeof entry === "object") {
        const children = Object.values(entry);
        if (Array.isArray(entry) && children.length > MAX_ROWS)
          throw new Error("A report list exceeds the 2,000 item limit.");
        children.forEach((child) => queue.push([child, depth + 1]));
      }
    }
    if (value.schema_version !== undefined && value.schema_version !== 1)
      throw new Error(
        "Unsupported report schema version. This Explorer supports version 1 and earlier unversioned reports.",
      );
    if (!["pr", "handoff", "incident"].includes(value.mode))
      throw new Error(
        "Expected a DebugBrief report mode: pr, handoff, or incident. Session state files cannot be imported; export with debugbrief end --format json.",
      );
    if (typeof value.title !== "string" || typeof value.session_id !== "string")
      throw new Error("The report needs a title and session_id.");
    if (
      !isObject(value.evidence) ||
      !["verified", "unverified"].includes(value.evidence.state) ||
      typeof value.evidence.basis !== "string"
    )
      throw new Error(
        "Missing or invalid evidence state and basis. No verification status can be inferred.",
      );
    const text = (object, keys) =>
      keys.forEach((key) => {
        if (
          object[key] !== undefined &&
          object[key] !== null &&
          typeof object[key] !== "string"
        )
          throw new Error(`Invalid text field: ${key}.`);
      });
    const flag = (object, keys) =>
      keys.forEach((key) => {
        if (object[key] !== undefined && typeof object[key] !== "boolean")
          throw new Error(`Invalid boolean field: ${key}.`);
      });
    const number = (object, keys, integer = false, negative = false) =>
      keys.forEach((key) => {
        const n = object[key];
        if (n === undefined || n === null) return;
        if (
          typeof n !== "number" ||
          !Number.isFinite(n) ||
          (!negative && n < 0) ||
          (integer && !Number.isInteger(n))
        )
          throw new Error(`Invalid numeric field: ${key}.`);
      });
    const list = (object, key, check) => {
      if (object[key] === undefined) return;
      if (!Array.isArray(object[key]) || object[key].length > MAX_ROWS)
        throw new Error(`Invalid list: ${key}.`);
      object[key].forEach(check);
    };
    const string = (item) => {
      if (typeof item !== "string")
        throw new Error("Expected text in a report list.");
    };
    const object = (item) => {
      if (!isObject(item))
        throw new Error("Expected an object in a report list.");
    };
    text(value, [
      "status",
      "project_root",
      "started_at",
      "ended_at",
      "one_liner",
      "observed_error",
      "reproduce_command",
      "verify_command",
      "reproduce_working_directory",
      "verify_working_directory",
      "command_capture_status",
    ]);
    text(value.evidence, ["scope", "command", "root_cause"]);
    if (
      value.evidence.root_cause &&
      value.evidence.root_cause !== "not_inferred"
    )
      throw new Error(
        "Unsupported root cause claim. DebugBrief reports do not infer root causes.",
      );
    list(value.evidence, "invalid_verification_commands", string);
    flag(value, ["redaction_applied"]);
    for (const [key, fields] of [
      ["counts", ["commands", "failed_commands", "notes"]],
      ["line_stats", ["added", "deleted"]],
    ]) {
      if (value[key] !== undefined) {
        object(value[key]);
        number(value[key], fields, true);
      }
    }
    if (value.git !== undefined) {
      object(value.git);
      text(value.git, ["branch", "initial_sha", "final_sha"]);
      flag(value.git, ["is_repo", "detached_head"]);
    }
    ["notes", "warnings"].forEach((key) => list(value, key, string));
    list(value, "changed_files", (item) => {
      object(item);
      string(item.path);
      text(item, ["status"]);
    });
    list(value, "timeline", (item) => {
      object(item);
      string(item.text);
      text(item, ["timestamp", "kind", "status", "working_directory"]);
      if (item.status != null && !states.has(item.status))
        throw new Error("Unsupported timeline status.");
    });
    list(value, "verification", (item) => {
      object(item);
      string(item.command);
      text(item, ["tool", "working_directory"]);
      flag(item, ["is_test"]);
    });
    const command = (item) => {
      object(item);
      string(item.command);
      text(item, [
        "timestamp",
        "status",
        "tool",
        "stdout_preview",
        "stderr_preview",
        "working_directory",
      ]);
      if (!states.has(item.status))
        throw new Error("A command has an unsupported or missing status.");
      number(item, ["exit_code"], true, true);
      number(item, ["duration_seconds"]);
      flag(item, [
        "is_test",
        "is_verification",
        "redacted",
        "stdout_truncated",
        "stderr_truncated",
      ]);
      if (item.status === "passed" && item.exit_code !== 0)
        throw new Error("A passed command must have exit code 0.");
      if (
        item.is_verification &&
        (item.status !== "passed" || item.exit_code !== 0)
      )
        throw new Error(
          "Contradictory verification result in command history.",
        );
    };
    list(value, "commands", command);
    ["currently_failing", "ruled_out"].forEach((key) =>
      list(value, key, (item) => {
        object(item);
        string(item.command);
        text(item, ["status", "timestamp", "working_directory"]);
        number(item, ["exit_code"], true, true);
        if (!states.has(item.status) || item.status === "passed")
          throw new Error("A failed command list contains an invalid outcome.");
      }),
    );
    if (value.red_to_green !== undefined && value.red_to_green !== null) {
      object(value.red_to_green);
      string(value.red_to_green.command);
      text(value.red_to_green, ["failed_at", "passed_at", "working_directory"]);
      number(value.red_to_green, ["window_seconds"]);
      list(value.red_to_green, "changed_files", string);
      flag(value.red_to_green, ["commit_boundary"]);
    }
    const omissions = [];
    [
      "commands",
      "changed_files",
      "verification",
      "notes",
      "timeline",
      "warnings",
      "counts",
      "git",
      "command_capture_status",
    ].forEach((key) => {
      if (value[key] === undefined)
        omissions.push(
          `${key.replaceAll("_", " ")} was not included in this report.`,
        );
    });
    return { report: value, omissions };
  }
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "Not recorded";
    const rounded = Number(seconds.toFixed(3));
    if (seconds > 0 && rounded === 0) return "<0.001s";
    if (rounded < 60) return `${rounded}s`;
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    if (hours) return `${hours}h ${minutes}m ${remainder}s`;
    return `${minutes}m ${remainder}s`;
  }
  // Pure validator can also run in Node regression tests without a DOM.
  if (typeof module !== "undefined" && module.exports)
    module.exports = { validateReport, formatDuration, MAX_BYTES };
  if (typeof document === "undefined") return;

  const $ = (id) => document.getElementById(id);
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const heading = (text, level = 3) => make(`h${level}`, text, "data-heading");
  const empty = (text) => make("p", text, "muted small");
  const sentence = (value) =>
    typeof value === "string" && value
      ? value[0].toUpperCase() + value.slice(1).toLowerCase()
      : value;
  const capital = (value) =>
    typeof value === "string" && value ? value[0].toUpperCase() + value.slice(1) : value;
  const listView = (values, render, fallback) => {
    if (!values?.length) return empty(fallback);
    const list = make("ul", undefined, "data-list");
    values.forEach((value) => {
      const li = make("li");
      li.append(render(value));
      list.append(li);
    });
    return list;
  };
  const metadata = (values) => {
    const list = make("dl", undefined, "metadata-list");
    values.forEach(([label, value]) => {
      list.append(make("dt", label), make("dd", value ?? "Not recorded"));
    });
    return list;
  };
  const badge = (text, state) =>
    make(
      "span",
      text,
      `badge ${state === "passed" || state === "verified" ? "badge-pass" : state === "unverified" ? "badge-warn" : "badge-fail"}`,
    );
  let current = null;
  let revision = 0;
  const initialPanels = [
    ...document.querySelectorAll('.explorer-content [role="tabpanel"]'),
  ].map((panel) => panel.cloneNode(true));
  function status(message, error = false) {
    $("import-status").textContent = message;
    $("import-status").dataset.error = String(error);
  }
  function activateOverview() {
    $("tab-overview").click();
  }
  function warningView(report, omissions) {
    const messages = [...(report.warnings || []), ...omissions];
    if (report.command_capture_status !== "full")
      messages.push(
        `Command capture: ${report.command_capture_status || "not recorded"}. Other commands may be absent.`,
      );
    if (report.redaction_applied)
      messages.push(
        "Secret-like values were masked. Redaction is best effort; review the remaining content before sharing.",
      );
    if (report.evidence.invalid_verification_commands?.length)
      messages.push(
        "Verification records have invalid timestamps: " +
          report.evidence.invalid_verification_commands.join(", "),
      );
    if (!messages.length)
      return empty(
        "No warnings were included. This does not guarantee completeness or secret detection.",
      );
    const box = make("div", undefined, "warning-list");
    messages.forEach((message) => box.append(make("p", message)));
    return box;
  }
  const directoryLabel = (value) =>
    value === "." ? "Project root (.)" : (value ?? "Not recorded");
  function locatedCommand(item) {
    const node = make("div");
    node.append(make("code", item.command));
    if (item.working_directory != null)
      node.append(
        make(
          "p",
          `Working directory: ${directoryLabel(item.working_directory)}`,
          "small muted",
        ),
      );
    return node;
  }
  function commandCard(command) {
    const details = make("details", undefined, "command-record");
    const summary = make("summary");
    summary.append(
      badge(command.status.replaceAll("_", " "), command.status),
      make("code", command.command),
    );
    const body = make("div", undefined, "command-output");
    body.append(
      metadata([
        ["Recorded at", command.timestamp || "Unknown time"],
        ["Exit code", command.exit_code ?? "Not available"],
        ["Working directory", directoryLabel(command.working_directory)],
        [
          "Duration",
          command.duration_seconds != null
            ? formatDuration(command.duration_seconds)
            : "Not recorded",
        ],
        [
          "Classification",
          command.is_verification
            ? "Passing verification"
            : command.is_test
              ? "Test attempt"
              : command.tool || "Command",
        ],
      ]),
    );
    ["stdout", "stderr"].forEach((stream) => {
      if (command[`${stream}_preview`]) {
        body.append(
          make(
            "h3",
            stream +
              (command[`${stream}_truncated`]
                ? " / truncated preview"
                : " / captured preview"),
          ),
          make("pre", command[`${stream}_preview`]),
        );
      }
    });
    if (!command.stdout_preview && !command.stderr_preview)
      body.append(empty("No output preview recorded."));
    if (command.redacted)
      body.append(empty("Best-effort redaction was applied."));
    details.append(summary, body);
    return details;
  }
  function renderCommands(report) {
    const pane = $("view-commands");
    pane.replaceChildren(heading("Command history", 2));
    if (!report.commands) {
      pane.append(
        make(
          "p",
          "This older report has no structured command history. Its chronology and verification list remain available. Export a new report with the updated CLI for per-attempt output.",
          "unsupported-note",
        ),
      );
      return;
    }
    const controls = make("div", undefined, "command-controls");
    const queryLabel = make("label", "Search command or output");
    const query = make("input");
    query.type = "search";
    query.placeholder = "pytest, error, file…";
    queryLabel.append(query);
    const filterLabel = make("label", "Outcome");
    const filter = make("select");
    [
      ["all", "All attempts"],
      ["failed", "Unsuccessful attempts"],
      ["passed", "Passed commands"],
      ["verification", "Passing verification"],
    ].forEach(([value, label]) => {
      const option = make("option", label);
      option.value = value;
      filter.append(option);
    });
    filterLabel.append(filter);
    controls.append(queryLabel, filterLabel);
    const count = make("p", undefined, "command-count");
    count.setAttribute("role", "status");
    count.setAttribute("aria-live", "polite");
    const results = make("div");
    const refresh = () => {
      const search = query.value.trim().toLowerCase();
      const commands = report.commands.filter((command) => {
        const outcome =
          filter.value === "all" ||
          (filter.value === "failed"
            ? command.status !== "passed"
            : filter.value === "verification"
              ? command.is_verification
              : command.status === "passed");
        return (
          outcome &&
          (!search ||
            [
              command.command,
              command.stdout_preview,
              command.stderr_preview,
            ].some((value) => value?.toLowerCase().includes(search)))
        );
      });
      count.textContent = `${commands.length} of ${report.commands.length} recorded attempts`;
      results.replaceChildren(...commands.map(commandCard));
      if (!commands.length)
        results.append(empty("No attempts match these filters."));
    };
    query.addEventListener("input", refresh);
    filter.addEventListener("change", refresh);
    pane.append(controls, count, results);
    refresh();
    pane.append(
      empty(
        "“Passing verification” uses the classification saved in the report.",
      ),
    );
  }
  function renderReport(report, omissions, source) {
    current = report;
    $("clear-report").disabled = false;
    const overview = $("view-overview");
    const header = make("div", undefined, "record-heading");
    header.append(
      make("h2", report.title || "Untitled recorded session"),
      badge(`Reported ${report.evidence.state}`, report.evidence.state),
    );
    const scope = make(
      "div",
      undefined,
      `evidence-scope ${report.evidence.state === "unverified" ? "is-unverified" : ""}`,
    );
    scope.append(
      make(
        "strong",
        /^(the|no) /.test(report.evidence.basis || "")
          ? capital(report.evidence.basis)
          : report.evidence.basis,
      ),
      make(
        "p",
        report.evidence.scope ||
          "Evidence scope was not included. Do not extend this result beyond recorded checks.",
      ),
    );
    const metrics = make("div", undefined, "metrics");
    [
      ["Commands", report.counts?.commands],
      ["Failed attempts", report.counts?.failed_commands],
      ["Changed files", report.changed_files?.length],
      ["Notes", report.counts?.notes],
    ].forEach(([label, value]) => {
      const metric = make("div", undefined, "metric");
      metric.append(make("strong", value ?? "n/a"), make("span", label));
      metrics.append(metric);
    });
    overview.replaceChildren(
      header,
      make(
        "p",
        `${source} · ${report.mode} · ${report.session_id}`,
        "provenance",
      ),
    );
    if (report.one_liner) overview.append(make("p", report.one_liner, "small"));
    overview.append(scope, metrics);
    const recordGrid = make("div", undefined, "overview-grid");
    const sessionInfo = make("section");
    sessionInfo.append(
      heading("Session record"),
      metadata([
        ["Session state", sentence(report.status?.replaceAll("_", " "))],
        ["Project", report.project_root],
        ["Started", report.started_at],
        ["Ended", report.ended_at],
        [
          "Git branch",
          report.git?.is_repo
            ? report.git.branch ||
              (report.git.detached_head ? "Detached HEAD" : "Unknown")
            : report.git
              ? "Not a Git repository"
              : null,
        ],
      ]),
    );
    const checks = make("section");
    checks.append(
      heading("Passing verification"),
      listView(
        report.verification,
        locatedCommand,
        "No passing verification listed.",
      ),
    );
    recordGrid.append(sessionInfo, checks);
    overview.append(recordGrid);
    if (report.observed_error)
      overview.append(
        heading("Observed error"),
        make("pre", report.observed_error, "unsupported-note"),
      );
    if (report.currently_failing?.length)
      overview.append(
        heading("Still failing at the end"),
        listView(
          report.currently_failing,
          (item) => {
            const node = locatedCommand(item);
            node.append(make("span", item.status, "badge badge-fail"));
            return node;
          },
          "",
        ),
      );
    if (report.red_to_green) {
      overview.append(
        heading("Red to green window"),
        metadata([
          ["Same check", report.red_to_green.command],
          [
            "Working directory",
            directoryLabel(report.red_to_green.working_directory),
          ],
          ["Failed at", report.red_to_green.failed_at],
          ["Passed at", report.red_to_green.passed_at],
          [
            "Window",
            report.red_to_green.window_seconds != null
              ? formatDuration(report.red_to_green.window_seconds)
              : null,
          ],
          [
            "Commit boundary",
            report.red_to_green.commit_boundary === undefined
              ? null
              : report.red_to_green.commit_boundary
                ? "Recorded"
                : "None recorded",
          ],
        ]),
        heading("Changed in the window"),
        listView(
          report.red_to_green.changed_files,
          (item) => make("code", item),
          "No file-level changes recorded in the window.",
        ),
        empty(
          "Files changed between these attempts; this does not establish the cause.",
        ),
      );
    }
    overview.append(
      heading("Warnings and missing information"),
      warningView(report, omissions),
    );
    renderCommands(report);
    $("view-changes").replaceChildren(
      heading("Changed files", 2),
      empty(
        report.line_stats
          ? `Recorded line delta: +${report.line_stats.added ?? "unknown"} / −${report.line_stats.deleted ?? "unknown"}.`
          : "Line statistics were not included.",
      ),
      listView(
        report.changed_files,
        (item) => make("code", `${item.status || "?"}  ${item.path}`),
        "No changed files listed.",
      ),
    );
    $("view-notes").replaceChildren(
      heading("Recorded notes", 2),
      empty("Notes written during the session."),
      listView(report.notes, (item) => make("span", item), "No notes listed."),
    );
    const chronology = make("ol", undefined, "chronology");
    (report.timeline || []).forEach((item) => {
      const li = make("li");
      const time = make("time", item.timestamp || "Unknown time");
      li.append(
        time,
        item.status
          ? badge(item.status.replaceAll("_", " "), item.status)
          : make("span", item.kind || "event", "badge"),
        make("p", item.text),
      );
      if (item.working_directory != null)
        li.append(
          make(
            "p",
            `Working directory: ${directoryLabel(item.working_directory)}`,
            "small muted",
          ),
        );
      chronology.append(li);
    });
    $("view-timeline").replaceChildren(
      heading("Chronology", 2),
      empty("Events in recorded order."),
      report.timeline?.length ? chronology : empty("No timeline was included."),
    );
    const evidence = $("view-evidence");
    evidence.replaceChildren(
      heading("Scope and limits", 2),
      scope.cloneNode(true),
      heading("Reproduce and verify"),
    );
    [
      [
        "Reproduce / recorded failure",
        report.reproduce_command,
        report.reproduce_working_directory,
      ],
      [
        "Verify / recorded pass",
        report.verify_command,
        report.verify_working_directory,
      ],
    ].forEach(([label, command, directory]) => {
      if (!command) return;
      const row = make("div", undefined, "command-strip");
      const code = make("code", command);
      const button = make("button", "Copy");
      button.type = "button";
      button.dataset.copy = command;
      button.setAttribute("aria-label", `Copy ${label.toLowerCase()} command`);
      evidence.append(
        make("p", label, "small muted"),
        make("p", `Run from: ${directoryLabel(directory)}`, "small muted"),
        row,
      );
      row.append(code, button);
    });
    if (!report.reproduce_command && !report.verify_command)
      evidence.append(
        empty("No reproduction or verification commands were included."),
      );
    evidence.append(
      empty(
        "Commands are text for inspection. The Explorer does not execute them. A historical pass can appear in an unverified report.",
      ),
      heading("Capture and privacy"),
      metadata([
        ["Capture status", sentence(report.command_capture_status)],
        [
          "Redaction",
          report.redaction_applied === undefined
            ? "Not recorded"
            : report.redaction_applied
              ? "Applied / best effort"
              : "No redaction recorded",
        ],
        ["Root cause", "Not inferred"],
      ]),
      warningView(report, omissions),
    );
    ["commands", "changes", "notes", "timeline"].forEach((key) => {
      const value = report[key === "changes" ? "changed_files" : key];
      $(`count-${key}`).textContent = value?.length ?? "";
    });
    activateOverview();
  }
  function commit(value, source) {
    const { report, omissions } = validateReport(value);
    renderReport(report, omissions, source);
    status(
      `Opened ${source}. ${omissions.length ? `${omissions.length} missing fields are flagged under Scope and limits.` : "Report loaded in this tab’s memory."}`,
    );
  }
  async function importFile(files) {
    const request = ++revision;
    if (files.length !== 1) {
      status(
        "Choose one JSON report at a time. The current report was kept.",
        true,
      );
      return;
    }
    const file = files[0];
    if (!file) return;
    if (file.size > MAX_BYTES) {
      status(
        "The report exceeds the 5 MiB limit. The current report was kept.",
        true,
      );
      return;
    }
    status(`Reading ${file.name} locally…`);
    try {
      const contents = await file.text();
      if (request !== revision) return;
      let value;
      try {
        value = JSON.parse(contents);
      } catch {
        throw new Error(
          "Invalid JSON. Export a DebugBrief JSON report and try again.",
        );
      }
      commit(value, file.name);
      document
        .querySelectorAll("[data-sample]")
        .forEach((button) => button.setAttribute("aria-pressed", "false"));
    } catch (error) {
      if (request === revision)
        status(
          `${error.message} ${current ? "The current report was kept." : "No report loaded."}`,
          true,
        );
    } finally {
      $("report-file").value = "";
    }
  }
  $("choose-report").addEventListener("click", () => $("report-file").click());
  $("report-file").addEventListener("change", (event) => {
    if (event.target.files.length) importFile(event.target.files);
  });
  const drop = $("drop-zone");
  let dragDepth = 0;
  drop.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dragDepth++;
    drop.classList.add("is-dragging");
  });
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  drop.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) {
      dragDepth = 0;
      drop.classList.remove("is-dragging");
    }
  });
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    drop.classList.remove("is-dragging");
    importFile(event.dataTransfer.files);
  });
  // Dropping outside the import target must not navigate away from the report.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
  $("clear-report").addEventListener("click", () => {
    revision++;
    current = null;
    const content = document.querySelector(".explorer-content");
    content.replaceChildren(
      ...initialPanels.map((panel) => panel.cloneNode(true)),
    );
    document
      .querySelectorAll("[data-sample]")
      .forEach((button) => button.setAttribute("aria-pressed", "false"));
    ["commands", "changes", "notes", "timeline"].forEach(
      (key) => ($(`count-${key}`).textContent = ""),
    );
    $("report-file").value = "";
    $("clear-report").disabled = true;
    activateOverview();
    status("Report cleared from this tab. Choose another JSON report.");
    $("choose-report").focus();
  });
  async function loadSample(mode) {
    if (!["pr", "handoff", "incident"].includes(mode)) return;
    const request = ++revision;
    status("Loading the illustrative example…");
    try {
      // Only these fixed bundled files can be fetched. Report content and query
      // parameters can never supply a URL or cause an upload.
      const response = await fetch(`data/sample-${mode}.json`);
      if (!response.ok)
        throw new Error(
          "The example could not load. Serve the site over HTTP, or choose a local JSON report.",
        );
      const data = await response.json();
      if (request !== revision) return;
      commit(data, `illustrative ${mode} fixture`);
      document
        .querySelectorAll("[data-sample]")
        .forEach((button) =>
          button.setAttribute(
            "aria-pressed",
            String(button.dataset.sample === mode),
          ),
        );
    } catch (error) {
      if (request === revision) status(error.message, true);
    }
  }
  document
    .querySelectorAll("[data-sample]")
    .forEach((button) =>
      button.addEventListener("click", () => loadSample(button.dataset.sample)),
    );
  const sample = new URLSearchParams(location.search).get("sample");
  if (sample) loadSample(sample);
})();
