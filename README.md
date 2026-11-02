# Dark mode for Burp Suite 1.7.x

Jython extension, toggled at runtime (no restart).

Load: *Extender → Extensions → Add → Python* → `burp_dark_mode.py`
(needs the standalone Jython jar set under *Extender → Options*).

Toggle: **Ctrl+Shift+D**, the **Theme** menu in Burp's menu bar, or the **Dark Mode** tab.
The setting is saved and re-applied on the next load; unloading the extension restores the light UI.

## How it works
- Nothing global is re-initialised and Burp's own custom UI delegates are never replaced.
- Every component's colours/border/UI are snapshotted before changing, so **light mode restores
  Burp's original look exactly** (verified pixel-for-pixel in `tests/harness.py`).
- Colour defaults of the look and feel are swapped for dark ones; only stock Swing components are refreshed.
- Tables and text panes (message editors, advisories) are painted through an invert + hue-rotate
  filter, so hard-coded colours (black text in styled documents, syntax tokens, custom renderers) stay readable.

## Diagnostics
If some area stays light or unreadable, open the **Dark Mode** tab → *Dump UI tree* writes
`~/burp_dark_mode_dump.txt` (component classes, colours, errors) to help fix it.

## Tests (no Burp needed; screenshots go to `tests/out`)

    java -jar jython-standalone-2.7.4.jar tests/harness.py metal tests/out
