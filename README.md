# scribeling

Records a Windows procedure click by click and writes it up as a step-by-step
guide with annotated screenshots.

It hooks your mouse, screenshots each click, asks UI Automation what you clicked,
draws a highlight around it, and produces a single self-contained `guide.html` you
can email, drop on a file share, or paste into a SharePoint page.

Ships two ways to use it:

- **`Scribeling.exe`** — a standalone recorder with a GUI. No Python needed.
- **A Claude Skill** — installed into Claude Code on Windows, so you can say
  "document how to create an Autopilot profile" and have the recording *and* the
  write-up handled in one pass.

The skill route exists because of a division of labour worth being explicit
about: the recorder is good at capture and bad at writing. It sees one click at a
time and names controls by their accessibility label, which yields captions like
`Click **Convert all targeted devices to Autopilot**` — accurate, and useless as
documentation. A language model is the reverse: good at writing, blind to your
screen. So the recorder captures, and Claude rewrites the captions from the
screenshots, groups steps into stages, and adds prerequisites.

## Quick start — standalone exe

Download `Scribeling.exe` from the
[latest release](../../releases/latest), or build it yourself:

```powershell
git clone https://github.com/thediscovery-cmd/scribeling.git
cd scribeling
.\build.ps1
```

Output is `dist\Scribeling.exe`. Double-click it, fill in a title and
description, press **Start recording**.

| Key | Button | Does |
|-----|--------|------|
| F7  | Pause  | stop and resume capture |
| F8  | Finish | end the recording, go to review |
| F9  | Undo   | drop the last step |
| F10 | Note   | insert a note step |

A small always-on-top HUD lists steps as you make them. Clicks on the HUD are
ignored, so its own buttons never end up in your guide.

Then the step editor opens. For each step you can:

- edit the **Step** caption, add a **Detail** line, and set a **Stage**
- **Delete** it, or tick several and **Delete selected** to clear out misclicks
- move it **↑ / ↓**
- **Merge up** into the step above (captions join, the later screenshot wins)
- swap the screenshot with **Image…**, or drop it with **No image**
- click the screenshot to **Redact** (solid box), **Blur** (pixelate) or
  **Crop** it. Edits are saved to a new file, so **Reset to original** can
  undo them later; only the edited version goes into `guide.html`
- add a blank **+ Step below** for a note or a step the recorder missed

**Undo** (or Ctrl+Z outside a text box) reverses any of these, deletes
included. **Save** writes `steps.json`; **Export guide** also builds the HTML.

To change a guide later, choose **Edit an existing recording…** on the start
screen, or run `Scribeling.exe edit "<session folder>"`.

Output lands in `Documents\scribeling\<timestamp>\`:

```
<session>/
├── steps.json    the source of truth - edit this
├── shots/        step-001.png, step-002.png, ...
└── guide.html    generated output, self-contained
```

`steps.json` is also editable by hand. Change captions, add a `detail`
sentence, group steps with `phase`, add a `prerequisites` list, set
`"hidden": true` on anything you want gone, then:

```powershell
Scribeling.exe rebuild "C:\Users\you\Documents\scribeling\2026-08-15_143002"
```

Captions accept `**bold**` and `` `code` ``. Top-level `author` and `tags` fill
the byline and the product badges; a step with `"action": "navigate"` and a
`url` renders as a clickable link.

## What the guide looks like

`guide.html` is one file with everything inline, so it works from an email
attachment or a file share with no network access. It has:

- **A stage sidebar** built from your `phase` names, with a "2 of 3" counter that
  follows you as you scroll, and up and down buttons to jump between stages.
- **Step cards** with numbered badges you can link to directly
  (`guide.html#step-7`).
- **Click-to-zoom screenshots.** Click again to see the image at full size, and
  press Esc to close.
- **A highlight box and a pointing hand** on each screenshot. The box shows which
  control was clicked, and the hand (with a soft glow) shows exactly where.
- **Light and dark themes.** The guide follows the reader's system setting, and
  the button in the corner overrides it.
- **A byline** with the author, step count, how long the procedure took, and
  the capture date. The author defaults to your Windows display name.
- **Copy-on-click** for anything in `` `code` ``, such as a value to type or a
  group tag.
- **Print-friendly output**: printing drops the sidebar and keeps steps whole.

See [`examples/example-guide.html`](examples/example-guide.html).

## Quick start — Claude Skill

Download `scribeling-guides.skill` from the [latest release](../../releases/latest)
and either click **Save skill** on the file card in Claude, or unzip it by hand:

```powershell
Copy-Item .\scribeling-guides.skill "$env:TEMP\scribeling-guides.zip" -Force
Expand-Archive "$env:TEMP\scribeling-guides.zip" -DestinationPath "$env:USERPROFILE\.claude\skills" -Force
```

Start a fresh Claude Code session — skills load at startup — then describe what
you want documented. Claude sets up its own virtual environment on first use, so
there is nothing else to install.

To package it from source: `powershell -File tools/pack-skill.ps1`

## Why the captions come out the way they do

Every step is named from the accessibility name of what you clicked. Control
types that tell a reader nothing (`Group`, `Pane`, `Text`, `Custom`, `Document`)
are never spoken aloud; only `Edit`, `CheckBox`, `RadioButton`, `ComboBox`,
`TabItem`, `MenuItem`, `Slider` and `Spinner` get named, because those nouns
carry information — "the **Name** field", "the **Yes** option".

The context line above a run of steps appears only when the window changes, so a
guide that stays in one portal reads as one continuous list instead of repeating
the same tab title thirty times. Browser names are stripped from window titles.

Repeat clicks on one target inside 1.6 seconds collapse into a single step,
fingerprinted by element name, control type and bounding box. When UI Automation
returns nothing the fingerprint falls back to the click position, so blind clicks
at different places stay distinct.

## Elevation

A medium-integrity process cannot read the UI Automation tree of an elevated
window. Record anything running as administrator from a non-elevated recorder and
you get screenshots with no element names, and captions reading "Click the
highlighted control".

`build.ps1` therefore manifests the exe as `requireAdministrator` by default — a
high-integrity process can read both high and medium integrity windows, so it
covers everything. Use `.\build.ps1 -NoAdmin` if you would rather not, in which
case the setup screen warns you and offers to restart itself elevated. For the
skill, launch the Claude Code terminal as administrator.

The UAC consent dialog itself runs on an isolated desktop that nothing can
screenshot or hook, at any integrity level. scribeling polls for it and writes an
"Approve the User Account Control prompt" step with no image, so the guide has an
instruction rather than an unexplained jump.

## Known limits

- Electron, Java, and legacy Win32 apps with no accessibility layer give empty
  element names. Screenshots and coordinate-based highlights still work.
- Fields flagged `IsPassword` are never transcribed, but a screenshot of the
  dialog is still taken. **Check your screenshots before sharing a guide** — they
  come from a live desktop and may hold a tenant name, a serial number, or an
  open inbox behind the window you meant to capture.
- Screenshots are taken on mouse-down, so a UI that repaints instantly can be a
  frame behind.
- The exe is unsigned. SmartScreen will warn on first run, and a PyInstaller
  onefile build that installs a global keyboard hook matches a fair number of
  infostealer heuristics — if your EDR quarantines it, swap `--onefile` for
  `--onedir` in `build.ps1`, which trips far fewer detections.

## Layout

```
scribeling/
├── build.ps1                          builds Scribeling.exe
├── requirements.txt
├── examples/example-guide.html        what the output looks like
├── skill/scribeling-guides/           the Claude Skill
│   ├── SKILL.md
│   ├── references/caption-style.md    how captions get rewritten
│   └── scripts/
│       ├── scribeling.py              the recorder (canonical copy)
│       ├── setup.ps1                  venv bootstrap
│       ├── launch.ps1                 detached launch with verification
│       └── inspect_session.py         compact session summary
└── tools/pack-skill.ps1               builds the .skill archive
```

`skill/scribeling-guides/scripts/scribeling.py` is the only copy of the recorder;
`build.ps1` and the skill both point at it.

## Attribution

An independent implementation, written from scratch against the public Windows
UI Automation and screen capture APIs. It is not derived from, affiliated with,
or endorsed by any commercial documentation product, and contains no third-party
code beyond the dependencies listed in `requirements.txt`.

## Licence

MIT
