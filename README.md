# Burp Dark Mode

A complete, runtime-switchable **dark theme for Burp Suite Professional 1.7.x**, built as a
legacy (Jython 2.7) Burp extension.

The whole Burp interface is themed — menu bar, suite tabs, Target / Proxy / Spider / Scanner /
Intruder / Repeater / Sequencer / Decoder / Comparer / Extender / options / Alerts tabs, tables,
trees, lists, dialogs, popup menus, text fields, HTTP message editors (with syntax highlighting)
and the extension's own settings dashboard.

| | |
|---|---|
| **Tested with** | Burp Suite Professional **1.7.37**, Jython **2.7.4** standalone, Oracle **Java 8** (jre1.8.0_361), Windows 10/11 |
| **Burp API** | legacy Extender API (`IBurpExtender`, `ITab`, `IExtensionStateListener`, `IMessageEditor`) |
| **Language** | Python **2.7** (Jython) — no Python 3 syntax |
| **Dependencies** | none (only the Jython standalone jar that Burp already needs) |

---

## Installation

1. Download `jython-standalone-2.7.4.jar`.
2. In Burp: **Extender → Options → Python Environment** → set *Location of Jython standalone JAR file*.
3. **Extender → Extensions → Add** → *Extension type*: **Python** → select `burp_dark_mode.py`.
4. Open the **Dark Mode** suite tab (it is added by the extension) and press **Apply theme**,
   or press **Ctrl + Shift + D**, or use the new **Theme** menu in Burp's menu bar.

The first time the extension loads the interface stays light until you activate the theme
(unless *Enable dark mode automatically on extension startup* was left enabled and a dark
theme was previously saved).

---

## Using the extension

### Keyboard / menu

* **Ctrl + Shift + D** — toggle dark / light (no restart, no reload).
* **Theme → Dark mode** — same toggle from Burp's menu bar.
* **Dark Mode** tab — the full settings dashboard.

### Dashboard

* **Theme settings** — large dark/light switch, current status, theme preview swatches,
  *Apply theme* and *Restore default theme* buttons.
* **Appearance** — dark/light mode selection and a set of accent colours used for highlights,
  focus rings and the dashboard itself.
* **General settings**
  * Enable dark mode automatically on extension startup
  * Remember the last selected theme
  * Apply the theme to newly opened windows and dialogs
  * Enable the Ctrl+Shift+D keyboard shortcut
* **Diagnostics** — live status, component/window counters, last message, *Export diagnostic
  logs* and *Reset extension settings*.

Settings are stored through Burp's extension settings API (`loadExtensionSetting` /
`saveExtensionSetting`), so they persist with the Burp project and are restored on the next
start.

### Switching back

* Press Ctrl+Shift+D (or use the switch / menu).
* Or unload the extension in **Extender → Extensions** — the original Burp appearance is
  restored before the extension is released.

Switching back is verified to be **pixel-for-pixel identical** to the original light UI
(see *Testing* below): every component colour, border, renderer and Burp-internal colour that
was changed is restored from a snapshot.

---

## How it works

Burp 1.7 is a Swing application that ships a custom launcher palette on top of a stock look &
feel (Nimbus by default, selectable in *User options*). A pure colour-override extension cannot
theme it reliably, so this extension uses a layered approach:

1. **Dark look & feel (UIManager / Metal theme).**
   A dark `DefaultMetalTheme` is installed with `UIManager.setLookAndFeel(new MetalLookAndFeel())`.
   Metal is a look & feel Burp 1.7 explicitly supports (its own components test for it), and it
   gives consistent colours for every stock Swing widget: menus, tabs, tables, trees, lists,
   scrollbars, text fields, dialogs and popups. All open windows are then rebuilt with
   `SwingUtilities.updateComponentTreeUI(window)`.

2. **UIManager overrides.**
   Burp writes a handful of light colours into the UIManager *user* defaults table
   (`Panel.background`, `Dialog.background`, `Frame.background`, `OptionPane.background`,
   `FileChooser.background`, `ToolTip.background`, `control`, `Table.alternateRowColor`).
   Those are snapshotted, replaced with dark values, and restored afterwards. Every other
   default lives in the Metal defaults table, which is discarded automatically when the original
   look & feel is restored — so nothing leaks into light mode.

3. **Component-level fixes.**
   Burp paints many components with hard-coded light colours (e.g. its message editor sets
   `Color.WHITE` in the constructor, its tables have custom renderers, its panels use fixed
   greys). A recursive pass over every window:
   * maps light backgrounds to the dark palette and dark text to light text (hue-preserving, so
     meaningful colours such as red/green/blue tokens stay recognisable),
   * sets caret, selection and disabled-text colours on text components,
   * wraps table/list/tree/combo cell renderers so renderers that paint fixed light colours are
     recoloured after they produce their component (Burp's sorting and formatting logic is left
     untouched),
   * replaces Nimbus' gradient table-header renderer with a dark one that keeps the
     ascending/descending sort indicators,
   * installs dark checkbox/radio icons.

4. **Burp internals (guarded, optional).**
   A few colours are painted from static fields rather than Swing properties. The extension
   optionally patches them by reflection:
   * `burp.ncd` — the page/panel background, default text colour, search-highlight colour and
     Burp's orange accent,
   * `burp.c3e` — the HTTP message-editor syntax highlighting palette (blue headers, red
     parameters, green strings, …) so highlighted text stays readable on a dark editor.

   These class/field names are specific to Burp 1.7.x. If they are missing (another build,
   another version) the extension logs a note and continues; everything else keeps working.

5. **Late components.**
   Newly created dialogs, popups, tabs and lazily built Burp panels are themed as they appear:
   an `AWTEventListener` watches hierarchy changes and a light timer picks up new windows. They
   are validated/revalidated after theming so layout is not disturbed.

6. **Reversible by construction.**
   Before anything changes, the extension snapshots the original look & feel, user defaults,
   component colours, renderers, text-component properties and Burp's static colours. Reverting
   restores them in reverse order, re-installs the original look & feel, re-applies the original
   renderers (Synth delegates replace renderers during `updateUI`) and clears all bookkeeping.

Everything UI-related runs on the Swing **Event Dispatch Thread**; the extension never blocks
Burp's UI with long operations, does not re-apply an already active theme, removes its listeners,
timers and menu on unload, and keeps only weak references to components so it does not leak.

---

## Known limitations

* **Web/HTML rendering panes remain light.** Burp's *Render* view (and rendered HTML bodies)
  uses an embedded browser (`lobobrowser`). Web content is deliberately left untouched —
  inverting or recolouring it would break the rendering. HTTP message *editors* (Raw/Params/
  Headers/Hex), tables, trees and all native Swing UI are themed.
* **OS window decorations are not themed.** The window title bar / borders are drawn by the
  operating system. On Windows 10/11 dark mode, they already match; otherwise enable the OS-wide
  dark title bar setting.
* **Burp's saved "look and feel" user option is unchanged.** The extension installs its theme at
  runtime and restores Burp's original look & feel when switched off; it does not rewrite Burp's
  own preference file.
* **Third-party extension tabs.** Components created by other extensions are themed generically
  (colours are mapped from light to dark). A component that paints images or its own gradients
  may keep its own look; it is never broken or replaced.
* **Java 9+ / Burp 2.x (Montoya) are not supported.** The extension targets the legacy Extender
  API and Java 8, which is what Burp 1.7.x ships with.
* The `burp.ncd` / `burp.c3e` patches are Burp **1.7.x**-specific. On an unknown build the
  extension still themes everything else and reports the skipped internals in the diagnostic log.

---

## Diagnostics

* **Dark Mode tab → Export diagnostic logs** writes
  `~/burp_dark_mode_diagnostics.txt` containing:
  * look & feel, active state, palette, window/component counts,
  * the full component tree with class, UI delegate and colours,
  * table/renderer information,
  * the extension's message and error log.

If an area stays light or unreadable, that file is the first thing to check. The same
information is printed to Burp's Extender output.

---

## Testing

### Offline harness (no Burp required)

The repository ships a harness that builds a Burp-like Swing window (Nimbus look & feel,
hard-coded cell renderers, styled message document, tabs, tree, dialog), applies the theme,
opens a new dialog while dark, switches back to light and verifies that the window is restored
**pixel-for-pixel**.

```bash
java -cp jython-standalone-2.7.4.jar org.python.util.jython tests/harness.py tests/out
```

Output:

```
restored vs original: 0 differing pixels
active after revert: False
RESULT: PASS
```

Screenshots (`1_light.png`, `2_dark.png`, `3_dark_dialog.png`, `4_restored.png`) are written to
the output directory.

### In-Burp self test

The extension contains an optional self test, enabled by environment variables, that exercises
the real Burp window and writes screenshots + logs:

```powershell
$env:BURP_DARK_SELFTEST = "1"
$env:BURP_DARK_TEST_DIR = "C:\temp\dark-selftest"
# start Burp normally (the extension will run the sequence)
```

It performs: light baseline → dark (via the keyboard shortcut) → dashboard → new dialog →
Burp message editor with a highlighted request/response → back to light (pixel diff is written
to `probe.log`) → dark again → accent change → extension unload. Artifacts: `00_light_baseline*.png`,
`01_dark*.png`, `04_dashboard_dark*.png`, `05_dialog_dark*.png`, `06_message_editor_dark*.png`,
`02_light*.png`, `07_after_unload*.png`, `probe.log`.

On a stock Burp 1.7.37 the restore diff is **0 pixels** and the log reports `errors=0`.

---

## Project structure

```
burp_dark_mode.py     the extension (single file, load this in Burp)
tests/harness.py      offline Swing harness (pixel-perfect restore check)
tests/out/            harness output (git-ignored)
README.md             this file
```

---

## License

Provided as-is for use with a licensed copy of Burp Suite Professional. Burp Suite is a
trademark of PortSwigger Ltd.; this project is not affiliated with PortSwigger.
