# Rewriting captions

The recorder writes captions from the accessibility name of whatever was clicked.
That produces something accurate and lifeless: `Click **Convert all targeted
devices to Autopilot**`. It tells the reader what happened, not what to do or why.
Your job is the gap between those two.

## What the recorder cannot know

It sees one click at a time, so it cannot tell the reader:

- **Which of several similar controls.** Three buttons named "Next" are three
  identical captions. Say where it is: "Next, at the bottom of the Basics tab."
- **Why a choice was made.** A toggle set to Yes is a decision. "Set **Convert
  all targeted devices to Autopilot** to Yes so existing devices get the profile
  without being reimaged" is a guide. "Click Yes" is a transcript.
- **What to substitute.** Recorded values are the recorder's own, not the
  reader's. `Test Profile` should become something like
  "type a profile name — the convention here is `Corp-Autopilot-<model>`".
- **What success looks like.** After a submit, say what should appear.
- **What was skipped.** Defaults left untouched are still decisions. A step
  noting "leave the remaining OOBE settings at their defaults" prevents the
  reader wondering whether they missed something.

## Structure

Use `phase` to group steps into stages a reader would recognise — "Create the
profile", "Configure the out-of-box experience", "Assign and verify". Phases
replace the automatic window-title lines, which are a guess at structure. Give
every step a phase or none of them; a half-phased guide reads as a mistake.

Use `prerequisites` for what must be true before step 1: roles and licences
needed, access required, anything to have open or to hand. Readers who cannot
complete step 1 should discover it before they start, not at step 1.

Phases also drive the guide's contents sidebar, which lists each stage and
tracks the reader's position as they scroll. Aim for two to five; a sidebar with
one entry is not shown, and one with twelve is a table of contents nobody reads.

Use `url` on a step to give the reader a link to click instead of an address to
copy. A procedure that starts in a portal should open with one:

```json
{"index": 0, "action": "navigate", "phase": "Find the device",
 "caption": "Open the Intune admin center at",
 "url": "https://intune.microsoft.com/#home"}
```

Only `http` and `https` links are rendered as links; anything else shows as
code. A navigate step needs no `image`. Give it an index not used by any
recorded step.

Use `detail` for one clarifying sentence under an instruction. It is the right
home for a warning, a naming convention, or an explanation of a choice. Do not
put a second instruction there — if the reader must act, it is a step.

## Voice

Imperative, present tense, second person implied: "Select the Windows PC
platform." Not "The Windows PC platform should be selected" and not "Now we'll
select the platform."

Name controls as the reader sees them, in bold: `**Deployment profiles**`. Put
literal values the reader types or looks for in backticks: `` `Corp-Autopilot` ``.

Keep one action per step. If a recorded caption contains two actions because the
recorder merged typing and a click, split it.

Do not narrate the UI's existence — "you will see a blade open on the right" is
what the screenshot is for.

## Merging and dropping

Drop by setting `"hidden": true`; never delete a step, so the mapping back to the
screenshots stays intact.

Worth dropping: clicks that only navigated somewhere already visible in the next
screenshot, accidental clicks on empty space, a click that opened a menu when the
following step shows the menu item being chosen.

Worth keeping even when it looks redundant: anything that changes state, anything
whose screenshot shows the reader what they should be seeing at that moment, and
every `Approve the User Account Control prompt` step.

## Steps with no element name

A caption reading `Click the highlighted control` means UI Automation returned
nothing — usually an elevated window recorded without elevation, or an Electron
or legacy Win32 app. The screenshot still shows a highlight box. Read the
screenshot, work out what the box is around, and write the caption from that. If
it genuinely cannot be determined, say what the reader should look for rather
than leaving the placeholder: "Click the search box at the top of the list."

## Worked example

Recorded:

```
7  Click **Convert all targeted devices to Autopilot**
8  Click **Next**
9  Click **Microsoft Entra joined**
```

Rewritten:

```json
{"index": 7, "phase": "Create the profile",
 "caption": "Set **Convert all targeted devices to Autopilot** to Yes",
 "detail": "This applies the profile to devices already in Intune without reimaging them. Leave it at No if you only want it to affect newly registered hardware."}
{"index": 8, "phase": "Create the profile",
 "caption": "Select **Next** to move to Out-of-box experience"}
{"index": 9, "phase": "Configure the out-of-box experience",
 "caption": "Choose **Microsoft Entra joined** as the join type",
 "detail": "Hybrid join needs line of sight to a domain controller during OOBE, which a remote user will not have."}
```
