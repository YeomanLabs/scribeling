---
name: scribeling-guides
description: Record a Windows click-by-click procedure and turn it into a polished step-by-step HTML guide with annotated screenshots. Use this skill whenever the user wants to document, write up, capture, or produce a walkthrough, SOP, runbook, KB article, how-to, or training doc for anything done in a Windows GUI - the Intune admin center, Entra, a portal, a desktop app, a settings dialog. Also use it when the user has already recorded a session and wants the captions improved or the guide rebuilt, or says anything like "document this process", "make a guide for how to...", "screenshot each step", or mentions scribeling by name. Requires Windows.
---

# scribeling-guides

Produces step-by-step procedure guides for Windows GUI work. A bundled recorder
captures each click with a screenshot and asks UI Automation what was clicked;
you then rewrite the mechanical captions into something a colleague can follow
and re-render the guide.

The division of labour matters. The recorder is good at capture and bad at
writing — it sees one click at a time and names things by their accessibility
label. You are good at writing and cannot see the screen. So: it records, you
write.

**Windows only.** The recorder installs a global input hook and screenshots the
local desktop, so it needs to run on the machine the user is sitting at. If this
session is not on Windows, say so plainly rather than attempting a workaround.

## Which task is this?

- **"Document how to do X"** — the user has not recorded anything. Start at
  Recording.
- **"Fix up / rebuild / improve this guide"**, or a session folder already
  exists — skip to Rewriting.

## Recording

**1. Prepare the environment** (idempotent, run it every time):

```powershell
powershell -ExecutionPolicy Bypass -File <skill>/scripts/setup.ps1
```

It prints `PYTHON=<path>` on the last line and writes the same path to
`%LOCALAPPDATA%\scribeling\python.txt`, which every later command reads. If it
reports no Python, relay its install instructions and stop rather than trying to
find an interpreter yourself.

You can skip this step if you are going straight to recording — `launch.ps1` runs
setup itself on first use.

**2. Check elevation if it matters.** A medium-integrity process cannot read the
UI Automation tree of an elevated window — captions come out as "Click the
highlighted control". If the procedure involves anything run as administrator
(Computer Management, Registry Editor, an elevated console, most MMC snap-ins),
tell the user to restart the terminal as administrator before recording.
Otherwise carry on; ordinary portals and apps are fine unelevated.

**3. Launch the recorder** using the bundled launcher. Pass the title and
description from what the user asked for, so they are not retyping their own
request:

```powershell
powershell -ExecutionPolicy Bypass -File <skill>/scripts/launch.ps1 -Title "Create an Autopilot deployment profile" -Description "For the deskside team, when staging a new Windows PC."
```

Use this script rather than calling `Start-Process` yourself. PowerShell's
`-ArgumentList` joins array elements with spaces without re-quoting them, so a
multi-word title passed as an array member arrives at Python as several separate
arguments and argparse rejects the tail. Because a detached process discards
stderr, the result is a silent failure: no window, no error, nothing to explain
it. `launch.ps1` builds one correctly quoted command line, bootstraps the
environment on first run, and captures stderr to a file so a failure is visible.

**Check the last line before telling the user anything.** `WINDOW=OK` means the
window exists and the process ID and window title are printed above it.
`WINDOW=FAILED` means it did not come up, and the script has already printed the
captured stderr or the foreground command to reproduce it. Diagnose from that
output. Never announce that the recorder is open without `WINDOW=OK` — the user
cannot see the window that is not there, and telling them to press Start
recording sends them looking for it.

Once it is up, tell the user, in your own words: press Start recording, perform
the procedure, press F8 or Finish when done, fix anything obviously wrong on the
review screen, then press Export. Say that you will improve the captions
afterwards, so they should not labour over them.

Hotkeys during recording: F7 pause, F8 finish, F9 drop the last step, F10 insert
a blank note.

**4. Wait for the user** to say they are finished. Do not poll the filesystem in
a loop.

## Rewriting

**1. Inspect the session** — never read `guide.html` directly. It embeds every
screenshot as base64 and routinely exceeds 500 KB, so it will flood your context
with nothing you can use:

```powershell
& (Get-Content "$env:LOCALAPPDATA\scribeling\python.txt" -Raw).Trim() <skill>/scripts/inspect_session.py --latest
```

`setup.ps1` writes the interpreter path to that marker file. Read it from there
rather than invoking bare `python`, which frequently does not exist on Windows -
either nothing is installed under that name, or it resolves to the Microsoft
Store alias stub, which is not an interpreter.

Pass a session folder instead of `--latest` if the user chose a non-default
output location. It prints one line per step, the windows involved, and the
problems worth fixing.

**2. Look at the screenshots.** Read `<session>/shots/step-NNN.png` for any step
whose caption is unclear, and for every step flagged as having no element name —
the magenta highlight box shows what was clicked. This is the step that turns a
transcript into a guide, and skipping it produces a guide indistinguishable from
what the recorder wrote by itself.

**3. Rewrite `<session>/steps.json`.** Read `references/caption-style.md` before
starting; it covers what to write, what to merge, what to drop, and the voice to
use. In short: keep `index` and `image` untouched, rewrite `caption`, add `phase`
to group steps into recognisable stages, add `detail` for a clarifying sentence,
add a `prerequisites` list at the top level, set `"hidden": true` rather than
deleting anything. Set top-level `author` and `tags` (product names, e.g.
`["Microsoft", "Intune"]`) if the user gave them. When the procedure starts in a
browser, insert a first step with `"action": "navigate"` and the page's `url`;
it renders as a clickable link, and needs no screenshot.

**4. Re-render:**

```powershell
& (Get-Content "$env:LOCALAPPDATA\scribeling\python.txt" -Raw).Trim() <skill>/scripts/scribeling.py rebuild "<session>"
```

If the user wants to adjust anything themselves afterwards — delete, reorder
or merge steps, swap a screenshot — point them at the step editor rather than
the JSON:

```powershell
& (Get-Content "$env:LOCALAPPDATA\scribeling\python.txt" -Raw).Trim() <skill>/scripts/scribeling.py edit "<session>"
```

**5. Tell the user what you changed** — how many steps you merged or dropped, the
phases you introduced, and anything you could not resolve from the screenshots
and want them to confirm. Then point them at `<session>/guide.html`.

## Before handing it over

Screenshots come from a live session, so they may contain a tenant name, a device
serial, an email address, or an open window that has nothing to do with the
procedure. If you spot something in a screenshot that should not leave the
company, say so specifically rather than issuing a general warning — the user
cannot act on "check your screenshots".

Fields the recorder flags as passwords are never transcribed, but a screenshot of
the dialog is still taken.

## Session layout

```
<session>/
├── steps.json    the source of truth - edit this
├── shots/        step-001.png, step-002.png, ...
└── guide.html    generated output, self-contained
```

Default location is `Documents\scribeling\<timestamp>\`.

## If something fails silently

A detached Windows GUI that dies on startup produces nothing at all — no window,
no message, no exit code you will see. When any launch or export appears to do
nothing, do not relaunch and hope. Re-run the same thing in the foreground so the
error has somewhere to go:

```powershell
& (Get-Content "$env:LOCALAPPDATA\scribeling\python.txt" -Raw).Trim() <skill>/scripts/scribeling.py gui
```

Tell the user what the error actually was. A second silent attempt costs them
another round trip and teaches them nothing.

## Known limits

- Electron, Java, and legacy Win32 apps with no accessibility layer give empty
  element names. Screenshots and highlights still work; read them.
- The UAC consent dialog runs on an isolated desktop nothing can capture. The
  recorder detects it and writes an "Approve the User Account Control prompt"
  step with no image. Keep those steps.
- Screenshots are taken on mouse-down, so a UI that repaints instantly can be a
  frame behind.
