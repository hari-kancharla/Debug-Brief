from __future__ import annotations

import base64
import hashlib
from html import escape
from typing import List, Optional

from ..derive import next_step_notes
from ..markdown import normalize_text, plain_markdown
from ..models import FileChange, Session
from .base import (
    ReportContext,
    _fmt_duration,
    build_context,
    report_project_root,
    report_warnings,
    report_working_directory,
)

VALID_HTML_MODES = ("pr", "handoff", "incident")

_STYLE = (
    ":root{color-scheme:light;--paper:#f4f0e7;--raised:#fbf9f3;"
    "--ink:#151b18;--muted:#526059;--rule:#c7cec5;--pass:#3c641e;"
    "--fail:#a63d35;--note:#7d571a;--ink-bg:#0b0e0d;--ink-text:#f1f0e9;"
    "--ink-muted:#a7b0a9;--ink-rule:#34413a;--pass-on-dark:#b9e769}\n"
    "*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);"
    "font:16px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"
    '"Segoe UI",sans-serif}main{width:min(980px,calc(100% - 40px));'
    "margin:0 auto;padding:56px 0 80px}.report-top{display:flex;"
    "justify-content:space-between;gap:20px;padding-bottom:16px;"
    "border-bottom:1px solid var(--ink);font:11px/1.4 ui-monospace,"
    "SFMono-Regular,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase}"
    ".report-top span:last-child{color:var(--muted)}h1,h2{font-family:Georgia,"
    '"Times New Roman",serif;font-weight:400;letter-spacing:-.04em}'
    "h1{max-width:800px;margin:58px 0 18px;font-size:clamp(2.8rem,7vw,5.6rem);"
    "line-height:.95}h2{margin:0 0 18px;font-size:2rem;line-height:1}"
    "h1,h2,.lede,.proof p,.section li,.section p,.report-top span,.footer{min-width:0;"
    "overflow-wrap:anywhere;word-break:break-word}"
    ".lede{max-width:760px;margin:0;color:var(--muted);font-size:1.15rem}"
    ".proof{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;margin:38px 0 54px;"
    "padding:18px;border:1px solid var(--ink);background:var(--raised);"
    "box-shadow:6px 6px 0 var(--ink)}.proof-label{grid-row:span 2;align-self:start;"
    "padding:6px 8px;background:var(--ink);color:var(--pass-on-dark);font:11px ui-monospace,"
    "SFMono-Regular,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase}"
    ".proof p{margin:0;color:var(--muted);font-size:13px}.proof strong{color:var(--ink);"
    "font-weight:600}.section{margin:54px 0}.section>ul,.section>ol{margin:0;padding:0;"
    "list-style:none}.section li{padding:10px 0;border-bottom:1px solid var(--rule);"
    "font-size:14px}.section li:last-child{border-bottom:0}.command{display:block;"
    "margin-top:5px;padding:8px 10px;overflow-wrap:anywhere;background:#e9e4d8;"
    "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.meta{display:flex;"
    "flex-wrap:wrap;gap:8px 22px;margin:0 0 36px;padding:12px 0;"
    "border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);"
    "color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace}"
    ".meta div{display:flex;gap:6px;min-width:0}.meta dt{font-weight:600;color:var(--ink);"
    "flex:0 0 auto}.meta dd{margin:0;min-width:0;overflow-wrap:anywhere;word-break:break-word}"
    ".status{font:11px ui-monospace,"
    "SFMono-Regular,Menlo,monospace;letter-spacing:.06em;text-transform:uppercase}"
    ".status-pass{color:var(--pass)}.status-fail{color:var(--fail)}.timeline{border-left:"
    "1px solid var(--rule);padding-left:22px;min-width:0}.timeline li{position:relative;"
    "min-width:0;padding:9px 0 12px;overflow-wrap:anywhere;word-break:break-word}"
    ".timeline li:before{position:absolute;left:-27px;top:17px;width:9px;height:9px;"
    'border:1px solid var(--ink);border-radius:50%;background:var(--paper);content:""}'
    ".timeline li.command-event:before{background:var(--pass)}.timeline li.warning:before"
    "{border-color:var(--fail)}.timeline .time{display:block;margin-bottom:3px;color:var(--muted);"
    "font:10px ui-monospace,SFMono-Regular,Menlo,monospace}.note{white-space:normal}"
    ".caveat{padding:12px 14px;border-left:3px solid var(--note);color:var(--muted);"
    "background:#f0eadc;font-size:13px}.error{margin:0;padding:14px;overflow-x:auto;"
    "background:var(--ink-bg);color:var(--ink-text);font:12px/1.6 ui-monospace,"
    "SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.empty{"
    "color:var(--muted);font-style:italic}.footer{margin-top:80px;padding-top:18px;"
    "border-top:1px solid var(--ink);color:var(--muted);font-size:12px}.footer strong"
    "{color:var(--ink)}a{color:inherit}@media(max-width:600px){main{width:min(100% - 28px,980px);"
    "padding-top:28px}.report-top{align-items:flex-start;flex-direction:column;gap:5px}"
    "h1{margin-top:42px;font-size:3.2rem}.proof{grid-template-columns:1fr;"
    "box-shadow:4px 4px 0 var(--ink)}"
    ".proof-label{grid-row:auto;justify-self:start}h2{font-size:1.7rem}}@media print{body{"
    "background:#fff}.report-top,.proof{box-shadow:none}.section{break-inside:avoid}}\n"
)

# Keep artifacts independent of the website, fonts, JavaScript, and network.
_STYLE += (
    "main{width:min(1800px,calc(100% - 64px));padding:32px 0 48px}"
    ".section>.timeline{border-left:0;padding:0}"
    ".timeline li{display:grid;grid-template-columns:220px minmax(0,1fr);gap:16px;"
    "align-items:start}.timeline li:before{display:none}"
    ".timeline .time{font-size:11px;line-height:1.6;margin:0;padding-top:2px}"
    ".timeline li.command-failed .time{color:var(--fail)}"
    "h1{margin:32px 0 16px;font-size:clamp(2.4rem,5vw,4.4rem);line-height:1.05}"
    "h2{font-family:inherit;font-size:1.2rem;font-weight:650;letter-spacing:-.02em;"
    "line-height:1.3}.section{margin:30px 0;padding-top:20px;border-top:1px solid var(--rule)}"
    ".proof{margin:24px 0;box-shadow:none;border-left:4px solid var(--pass)}"
    ".proof-unverified{border-left-color:var(--note)}.proof-label{grid-row:span 3}"
    ".meta{line-height:1.7}.status-neutral{color:var(--muted)}"
    ".timeline li.command-failed:before{background:var(--fail)}"
    ".timeline li.command-neutral:before{background:var(--rule)}"
    ".timeline li.warning-event:before{background:var(--note)}"
    "details.record{margin:8px 0;border:1px solid var(--rule);background:var(--raised)}"
    "details.record summary{padding:12px;cursor:pointer;overflow-wrap:anywhere}"
    "details.record summary:focus-visible{outline:2px solid var(--pass);outline-offset:2px}"
    ".record-body{padding:0 14px 14px}.record-body pre{margin:8px 0}"
    ".record-meta{color:var(--muted);font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}"
    ".working-directory{display:block;margin-top:4px;overflow-wrap:anywhere}"
    ".record code{font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}"
    ".footer{margin-top:36px}.error{tab-size:4}"
    "@media(max-width:700px){.timeline li{grid-template-columns:1fr;gap:4px}}"
    "@media(max-width:600px){main{width:calc(100% - 32px)}.section{margin:24px 0}"
    "h1{margin-top:24px}.proof{box-shadow:none}}"
    "@media print{main{width:100%;padding:0}h1{font-size:30pt}.section{break-inside:auto}"
    "h2,summary{break-after:avoid}.error{background:#fff;color:#111;border:1px solid #ccc}"
    "details.record>.record-body{display:block!important}"
    "details.record::details-content{content-visibility:visible!important;block-size:auto!important}"
    ".record{break-inside:avoid}"
    ".proof{border:1px solid #555}.meta{font-size:9pt}}"
)


def _text(value: object) -> str:
    return escape(normalize_text(value), quote=True).replace("\n", "<br>")


def _code(value: object) -> str:
    return f'<code class="command">{escape(normalize_text(value), quote=True)}</code>'


def _cwd(context: ReportContext, invocation_cwd: Optional[str]) -> str:
    directory = report_working_directory(context.session, invocation_cwd)
    if directory is None:
        return ""
    label = "Project root (.)" if directory == "." else directory
    return f'<span class="record-meta working-directory">Working directory: {_text(label)}</span>'


def _plain_summary(value: Optional[str]) -> str:
    if not value:
        return "No command outcome was recorded."
    return _text(plain_markdown(value))


def _section(title: str, content: str, class_name: str = "") -> str:
    classes = f"section {class_name}".strip()
    return f'<section class="{classes}"><h2>{_text(title)}</h2>{content}</section>'


def _list(items: List[str], empty: str = "None recorded.") -> str:
    if not items:
        return f'<p class="empty">{_text(empty)}</p>'
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _status(status: str) -> str:
    text = normalize_text(status).lower()
    css = "status-pass" if text == "passed" else "status-fail"
    return f'<span class="status {css}">{_text(status)}</span>'


def _timeline(context: ReportContext) -> str:
    if not context.timeline:
        return '<p class="empty">No timeline events recorded.</p>'
    items: List[str] = []
    for entry in context.timeline:
        text = entry.plain_text if entry.plain_text is not None else entry.text
        event_class = f"{_text(entry.kind)}-event"
        if entry.kind == "command":
            event_class += " command-" + (
                "passed" if entry.status == "passed" else "failed" if entry.status else "neutral"
            )
        time = f'<span class="time">{_text(entry.timestamp)} · {_text(entry.kind)}</span>'
        directory = (
            f'<span class="record-meta working-directory">Working directory: '
            f'{_text(entry.working_directory)}</span>'
            if entry.working_directory is not None else ""
        )
        items.append(
            f'<li class="{event_class}">{time}'
            f'<span class="event-text">{_text(text)}{directory}</span></li>'
        )
    return '<ol class="timeline">' + "".join(items) + "</ol>"


def _command_history(context: ReportContext) -> str:
    records: List[str] = []
    for record in context.derivation.command_records:
        exit_code = "not available" if record.exit_code is None else str(record.exit_code)
        body = (
            f'<p class="record-meta">{_text(record.timestamp or "Unknown time")} · '
            f"exit {exit_code} · {_text(_fmt_duration(record.duration_seconds))}</p>"
        )
        body += _cwd(context, record.invocation_cwd)
        for label, output, truncated in (
            ("stdout", record.stdout_preview, record.stdout_truncated),
            ("stderr", record.stderr_preview, record.stderr_truncated),
        ):
            if output:
                suffix = " · truncated preview" if truncated else " · captured preview"
                body += (
                    f'<p class="record-meta">{label}{suffix}</p>'
                    f'<pre class="error">{escape(normalize_text(output), quote=True)}</pre>'
                )
        if not record.stdout_preview and not record.stderr_preview:
            body += '<p class="empty">No output preview recorded.</p>'
        records.append(
            '<details class="record"><summary>'
            f"{_status(record.status)} · <code>{escape(record.command)}</code>"
            f'</summary><div class="record-body">{body}</div></details>'
        )
    return "".join(records)


def _verification(context: ReportContext) -> str:
    if not context.verification_commands:
        return '<p class="empty">No verification command passed.</p>'
    items: List[str] = []
    for record in context.verification_commands:
        repeat = (
            f' <span class="status">×{record.verification_count}</span>'
            if record.verification_count > 1 else ""
        )
        items.append(
            f"{_status('passed')} {_code(record.command)}{repeat}"
            + _cwd(context, record.invocation_cwd)
        )
    return _list(items)


def _changed_files(session: Session) -> str:
    changes = session.summary.file_changes or [
        FileChange("M", path) for path in session.summary.modified_files
    ]
    if not changes:
        return '<p class="empty">No files changed during the session.</p>'
    return _list([f"{_text(change.status)} · {_code(change.path)}" for change in changes])


def _notes(context: ReportContext) -> str:
    return _list([_text(text) for _timestamp, text in context.notes], "No notes recorded.")


def _commands(context: ReportContext) -> str:
    return _list(
        [f"{_code(record.command)} {_status(record.status)}" + _cwd(context, record.invocation_cwd)
         for record in context.report_commands]
    )


def _reproduce(context: ReportContext) -> str:
    derivation = context.derivation
    items: List[str] = []
    if derivation.reproduce_command:
        items.append(
            f"Reproduce: {_code(derivation.reproduce_command)}"
            + _cwd(context, derivation.reproduce_invocation_cwd)
        )
    if derivation.verify_command:
        label = "Verify" if derivation.proof_state == "verified" else "Earlier verify"
        items.append(
            f"{label}: {_code(derivation.verify_command)}"
            + _cwd(context, derivation.verify_invocation_cwd)
        )
    return _list(items)


def _failed(context: ReportContext) -> str:
    return _list(
        [
            f"{_code(record.command)} {_status(record.status)}"
            + _cwd(context, record.invocation_cwd)
            for record in context.derivation.ruled_out
        ]
    )


def _current_status(context: ReportContext) -> str:
    failed = len(context.currently_failing)
    if failed:
        noun = "command was" if failed == 1 else "commands were"
        return _text(f"{failed} recorded {noun} still failing at the end.")
    if context.verification_commands:
        return _text("A recorded verification command passed and none is currently failing.")
    return _text("No verification command passed; the current state is unconfirmed.")


def _resolution(context: ReportContext) -> str:
    failed = len(context.currently_failing)
    if failed:
        noun = "command was" if failed == 1 else "commands were"
        return _text(
            f"{failed} recorded {noun} failing at the end; the state is incomplete."
        )
    if context.verification_commands:
        return _text("A recorded verification command passed and none is currently failing.")
    return _text("No verification command passed; the resolution state is unconfirmed.")


def _time_window(session: Session) -> str:
    return (
        f"<p>Start: {_text(session.timestamps.start or 'unknown')}</p>"
        f"<p>End: {_text(session.timestamps.end or 'unknown')}</p>"
    )


def _repo_state(session: Session) -> str:
    if not session.git.is_repo:
        return ""
    branch = session.git.branch or "(unknown)"
    return (
        f"<p>Branch: {_text(branch)}</p><p>Changed files: {len(session.summary.modified_files)}</p>"
    )


def _followups(context: ReportContext) -> str:
    items = [
        f"Resolve the failing command: {_code(record.command)}"
        for record in context.currently_failing
    ]
    if not context.verification_commands:
        items.append("Add a passing verification command to confirm the state.")
    return _list(items, "No follow-up items were derived from the record.")


def _warnings(session: Session, context: ReportContext) -> str:
    items = list(report_warnings(session))
    if context.derivation.redaction_applied:
        items.append("Secret-like values were redacted; redaction is conservative and best effort.")
    if session.summary.command_capture_status != "full":
        items.append(f"Command capture status: {session.summary.command_capture_status}.")
    if not items:
        return ""
    return _list([_text(item) for item in items])


def render_report_html(session: Session, mode: str, include_full_path: bool = False) -> str:
    """Render a self-contained, escaped HTML view of a recorded session."""
    if mode not in VALID_HTML_MODES:
        raise ValueError(
            f"Unknown report mode {mode!r}. Valid modes: {', '.join(VALID_HTML_MODES)}."
        )
    context = build_context(session)
    derivation = context.derivation
    root = report_project_root(session, include_full_path)
    proof_label = derivation.proof_state
    proof_class = "status-pass" if proof_label == "verified" else "status-fail"
    sections: List[str] = []
    if mode == "pr":
        if derivation.red_to_green:
            red_to_green = derivation.red_to_green
            transition = (
                f"<p>{_status('failed')} at {_text(red_to_green.failed_at)}</p>"
                f"<p>{_status('passed')} at {_text(red_to_green.passed_at)}</p>"
                '<p class="caveat">These file changes are correlation, not proven cause.</p>'
            )
            changed = _list(
                [_code(path) for path in red_to_green.changed_files],
                (
                    "A commit boundary was recorded between checks; file-level "
                    "changes were not captured in working-tree snapshots."
                    if red_to_green.commit_boundary
                    else "No files changed in the window."
                ),
            )
            sections.append(_section("Red to green", transition + changed))
        if derivation.reproduce_command or derivation.verify_command:
            sections.append(_section("Reproduce and verify", _reproduce(context)))
        if derivation.observed_error:
            error = f'<pre class="error">{escape(derivation.observed_error)}</pre>'
            sections.append(_section("Observed error", error))
        if context.verification_commands:
            sections.append(_section("Verification", _verification(context)))
        if session.summary.file_changes or session.summary.modified_files:
            sections.append(_section("Changed files", _changed_files(session)))
        if derivation.ruled_out:
            sections.append(_section("Failed attempts", _failed(context)))
        if context.notes:
            sections.append(_section("Notes", _notes(context)))
        if context.timeline:
            sections.append(_section("Timeline", _timeline(context)))
    elif mode == "handoff":
        sections.append(_section("Current status", _current_status(context)))
        if derivation.observed_error:
            error = f'<pre class="error">{escape(derivation.observed_error)}</pre>'
            sections.append(_section("Observed error", error))
        if context.notes:
            sections.append(_section("Findings", _notes(context)))
        if context.timeline:
            sections.append(_section("Timeline", _timeline(context)))
        if context.report_commands:
            sections.append(_section("Commands attempted", _commands(context)))
        if derivation.ruled_out:
            sections.append(_section("Failed attempts", _failed(context)))
        repo_state = _repo_state(session)
        if repo_state:
            sections.append(_section("Repository state", repo_state))
        steps = next_step_notes(session)
        if steps:
            step_items = [_text(step) for step in steps]
            sections.append(_section("Suggested next steps", _list(step_items)))
    else:
        sections.append(_section("Time window", _time_window(session)))
        if context.timeline:
            sections.append(_section("Chronological event timeline", _timeline(context)))
        if derivation.observed_error:
            error = f'<pre class="error">{escape(derivation.observed_error)}</pre>'
            sections.append(_section("Observed error", error))
        sections.append(_section("Resolution / current state", _resolution(context)))
        if context.verification_commands:
            sections.append(_section("Verification", _verification(context)))
        if context.currently_failing or not context.verification_commands:
            sections.append(_section("Follow-up items", _followups(context)))
    if derivation.command_records:
        sections.append(_section("Command output", _command_history(context)))
    warnings = _warnings(session, context)
    if warnings:
        sections.append(_section("Warnings and limitations", warnings))
    scope = (
        "this verifies the recorded check, not the whole issue"
        if derivation.proof_state == "verified"
        else "this describes recorded checks, not the whole issue"
    )
    proof = (
        f'<div class="proof proof-{proof_label}"><span class="proof-label">evidence</span>'
        f'<p><strong class="{proof_class}">{_text(proof_label)}</strong> · '
        f"{_text(derivation.proof_basis)}</p>"
        f"<p>Scope: {scope}.</p>"
        "<p>Root cause is not inferred by DebugBrief.</p></div>"
    )
    metadata_items = [
        ("Project", root),
        ("Status", session.status),
        ("Started", session.timestamps.start or "unknown"),
        ("Ended", session.timestamps.end or "unknown"),
        ("Notes", str(session.summary.notes_count)),
        ("Commands", str(session.summary.commands_count)),
        ("Failed", str(session.summary.failed_commands_count)),
        ("Lines", f"+{session.summary.lines_added} / -{session.summary.lines_deleted}"),
    ]
    if session.git.is_repo:
        metadata_items.extend(
            [
                ("Branch", session.git.branch or "unknown"),
                ("Initial commit", session.git.initial_sha or "n/a"),
                ("Final commit", session.git.final_sha or "n/a"),
            ]
        )
    metadata = (
        '<dl class="meta">'
        + "".join(
            f"<div><dt>{_text(label)}</dt><dd>{_text(value)}</dd></div>"
            for label, value in metadata_items
        )
        + "</dl>"
    )
    footer = (
        '<footer class="footer"><strong>Generated by DebugBrief.</strong> '
        "This report contains explicitly recorded notes, command outcomes, and "
        "Git state when available. "
        "It does not use AI and does not infer root causes, intent, or results.</footer>"
    )
    style_hash = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
    policy = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; "
        f"style-src 'sha256-{style_hash}'; img-src data:"
    )
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        '<link rel="icon" href="data:,">'
        f"<title>{_text(session.title)} · DebugBrief</title>"
        f"<style>{_STYLE}</style></head><body><main>"
        '<header class="report-top"><span>debugbrief / '
        f"{_text(mode)}</span><span>evidence-backed · local record</span></header>"
        f"<h1>{_text(session.title)}</h1>"
        f'<p class="lede">{_plain_summary(derivation.one_liner)}</p>'
        f"{proof}{metadata}" + "".join(sections) + footer + "</main></body></html>\n"
    )


__all__ = ["render_report_html"]
