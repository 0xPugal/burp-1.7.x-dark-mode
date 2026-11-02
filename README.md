# Dark mode for Burp Suite 1.7.x

Jython extension, toggled at runtime (no restart).

Load: *Extender → Extensions → Add → Python* → `burp_dark_mode.py`
(needs the standalone Jython jar set under *Extender → Options*).

Toggle: **Ctrl+Shift+D**, the **Theme** menu in Burp's menu bar, or the **Dark Mode** tab.
The setting is saved and re-applied on the next load; unloading the extension restores the light UI.

Tests (no Burp needed; writes before/after screenshots to `tests/out`):

    java -jar jython-standalone-2.7.4.jar tests/harness.py metal tests/out
    java -jar jython-standalone-2.7.4.jar tests/harness.py nimbus tests/out
