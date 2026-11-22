# -*- coding: utf-8 -*-
"""
Burp Dark Mode
==============

A dark theme for Burp Suite Professional 1.7.x, implemented as a legacy
(Jython 2.7) Burp extension.

Target runtime
--------------
* Burp Suite Professional 1.7.37 (legacy Extender API: IBurpExtender / ITab)
* Jython 2.7 (Python 2.7 syntax)
* Java 8 (the JRE bundled with Burp 1.7.x)

Design in one paragraph
-----------------------
Burp 1.7 uses a stock Swing Look & Feel (Nimbus by default, selectable in
User options) plus a large amount of custom-painted Swing components.  This
extension installs a *dark Metal theme* through the UIManager, updates every
open window with SwingUtilities.updateComponentTreeUI(), and then fixes up the
components Burp paints with hard-coded light colours (message editors, tables,
custom panels, syntax-highlighting palette).  Every colour it overwrites is
snapshotted first, so switching back (or unloading the extension) restores the
original Look & Feel and colours exactly.  No pixel inversion, no rendering
hacks.
"""

try:  # pragma: no cover - outside Burp (unit tests / harness)
    from burp import IBurpExtender, IExtensionStateListener, ITab
except ImportError:  # pragma: no cover
    class IBurpExtender(object):
        pass

    class IExtensionStateListener(object):
        pass

    class ITab(object):
        pass

import os
import sys
import time
import traceback

from java.awt import (AWTEvent, BasicStroke, BorderLayout, Color, Component, Container, Cursor,
                      Dimension, Font, Graphics, Graphics2D, GridBagConstraints, GridBagLayout,
                      Insets, KeyboardFocusManager, KeyEventDispatcher, RenderingHints,
                      Toolkit, Window)
from java.awt.event import AWTEventListener, ActionListener, HierarchyEvent, KeyEvent, MouseAdapter
from java.awt.image import BufferedImage
from java.io import File
from java.lang import (Boolean, Number, Object, Runnable, String, System, Thread,
                       Throwable)
from java.lang.reflect import Field as ReflectField, Modifier
from java.util import ArrayList, WeakHashMap
from javax.imageio import ImageIO
from javax.swing import (BorderFactory, Box, BoxLayout, ButtonGroup, JButton, JCheckBox,
                         JCheckBoxMenuItem, JColorChooser, JComponent, JDialog, JFrame, JLabel,
                         JList, JMenu, JMenuItem, JPanel, JPopupMenu, JRadioButton, JScrollPane,
                         JTabbedPane, JTable, JTextArea, JTextPane, JToggleButton, JTree,
                         RootPaneContainer, SwingConstants, SwingUtilities,
                         Timer, UIManager)
from javax.swing import Icon
from javax.swing.text import JTextComponent
from javax.swing.border import EmptyBorder, LineBorder, TitledBorder
from javax.swing.plaf import ColorUIResource
from javax.swing.plaf.metal import DefaultMetalTheme, MetalLookAndFeel, OceanTheme
from javax.swing.table import DefaultTableCellRenderer, TableCellRenderer
from javax.swing.tree import DefaultTreeCellRenderer

# ---------------------------------------------------------------------------
# Extension metadata
# ---------------------------------------------------------------------------
EXT_NAME = "Burp Dark Mode"
SETTING_PREFIX = "burp_dark_mode."

SETTING_THEME = SETTING_PREFIX + "theme"                 # "dark" | "light"
SETTING_REMEMBER = SETTING_PREFIX + "remember"           # "1" | "0"
SETTING_AUTO = SETTING_PREFIX + "auto_apply"              # "1" | "0"
SETTING_NEW_WINDOWS = SETTING_PREFIX + "theme_new_windows"  # "1" | "0"
SETTING_HOTKEY = SETTING_PREFIX + "hotkey"                # "1" | "0"
SETTING_ACCENT = SETTING_PREFIX + "accent"                # "#RRGGBB"

HOTKEY_TEXT = "Ctrl+Shift+D"

SETTING_KEYS = {
    "theme": SETTING_THEME,
    "remember": SETTING_REMEMBER,
    "auto_apply": SETTING_AUTO,
    "new_windows": SETTING_NEW_WINDOWS,
    "hotkey": SETTING_HOTKEY,
    "accent": SETTING_ACCENT,
}


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
def rgb(value):
    """0xRRGGBB -> java.awt.Color"""
    return Color((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def parse_hex(text, fallback):
    try:
        text = text.strip()
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            return rgb(int(text, 16))
    except (Exception, Throwable):
        pass
    return fallback


def to_hex(color):
    try:
        return "#%02X%02X%02X" % (color.getRed(), color.getGreen(), color.getBlue())
    except (Exception, Throwable):
        return "#000000"


class Palette(object):
    """The colours used by the theme.  Values follow the VS Code palette."""

    def __init__(self, accent=None):
        self.accent = accent or rgb(0x00A8E8)
        self.bg_main = rgb(0x1E1E1E)
        self.bg_secondary = rgb(0x252526)
        self.bg_panel = rgb(0x2D2D30)
        self.bg_input = rgb(0x181818)
        self.fg = rgb(0xD4D4D4)
        self.fg_dim = rgb(0xA0A0A0)
        self.fg_disabled = rgb(0x6E6E70)
        self.border = rgb(0x3E3E42)
        self.sel_bg = rgb(0x264F78)
        self.sel_fg = rgb(0xFFFFFF)
        self.success = rgb(0x4CAF50)
        self.warning = rgb(0xFFB74D)
        self.error = rgb(0xF44336)
        self.highlight = rgb(0x4A3C12)      # search-match background
        self.shadow = rgb(0x111111)
        self.tooltip_bg = rgb(0x2D2D30)


# ---------------------------------------------------------------------------
# Small Swing helpers
# ---------------------------------------------------------------------------
class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        self.fn()


class _Action(ActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self.fn(event)


def on_edt(fn):
    """Run *fn* on the Swing event dispatch thread (asynchronously)."""
    if SwingUtilities.isEventDispatchThread():
        fn()
    else:
        SwingUtilities.invokeLater(_Run(fn))


def run_edt_and_wait(fn):
    """Run *fn* on Burp's EDT and wait for it (invokeLater + polling; Jython
    threads can live in a different AppContext, where invokeAndWait would wait
    on an event queue that is never pumped).  Returns fn's return value."""
    box = {"done": False, "error": None, "value": None}

    def task():
        try:
            box["value"] = fn()
        except (Exception, Throwable):
            box["error"] = traceback.format_exc()
        finally:
            box["done"] = True

    on_edt(task)
    deadline = time.time() + 30.0
    while not box["done"] and time.time() < deadline:
        time.sleep(0.05)
    if box["error"]:
        raise RuntimeError(box["error"])
    return box["value"]


def luminance(color):
    """Perceived luminance 0.0 .. 1.0"""
    if color is None:
        return 0.5
    try:
        return (0.2126 * color.getRed() + 0.7152 * color.getGreen()
                + 0.0722 * color.getBlue()) / 255.0
    except (Exception, Throwable):
        return 0.5


def saturation(color):
    try:
        r, g, b = color.getRed() / 255.0, color.getGreen() / 255.0, color.getBlue() / 255.0
        mx, mn = max(r, g, b), min(r, g, b)
        return 0.0 if mx == 0.0 else (mx - mn) / mx
    except (Exception, Throwable):
        return 0.0


def plain(color, alpha=None):
    """Plain (non-UIResource) copy of a colour; DerivedColor copies its current RGB."""
    if color is None:
        return None
    try:
        a = color.getAlpha() if alpha is None else alpha
        return Color(color.getRed(), color.getGreen(), color.getBlue(), a)
    except (Exception, Throwable):
        return None


def hsb_color(hue, sat, brightness, alpha):
    value = Color.HSBtoRGB(hue, max(0.0, min(1.0, sat)), max(0.0, min(1.0, brightness)))
    return Color((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF, alpha)


def mix(c1, c2, amount):
    """Linear blend: amount 0 == c1, 1 == c2."""
    try:
        return Color(int(c1.getRed() + (c2.getRed() - c1.getRed()) * amount),
                     int(c1.getGreen() + (c2.getGreen() - c1.getGreen()) * amount),
                     int(c1.getBlue() + (c2.getBlue() - c1.getBlue()) * amount))
    except (Exception, Throwable):
        return c1


# ---------------------------------------------------------------------------
# Colour mapping (light UI colour -> dark theme colour)
# ---------------------------------------------------------------------------
class ColorMapper(object):
    def __init__(self, palette):
        self.p = palette

    def background(self, color, input_like=False):
        """Map a light background to a dark one.  Dark colours are kept."""
        if color is None:
            return None
        p = self.p
        lum = luminance(color)
        sat = saturation(color)
        alpha = color.getAlpha()

        if lum <= 0.42:
            return color                       # already dark
        if sat > 0.22:                          # tinted (selection, severity, ...)
            if lum > 0.6:
                # keep the hue, drop the brightness so light tints become dark tints
                hue = Color.RGBtoHSB(color.getRed(), color.getGreen(), color.getBlue(), None)[0]
                return hsb_color(hue, min(0.85, sat * 0.9), 0.20 + (1.0 - lum) * 0.15, alpha)
            return color
        # neutral light greys / whites
        if input_like or lum >= 0.965:
            return plain(p.bg_input, alpha)
        if lum >= 0.90:
            return plain(p.bg_main, alpha)
        if lum >= 0.75:
            return plain(p.bg_secondary, alpha)
        # mid grey -> panel, slightly scaled
        return plain(Color(int(0x2D + (lum - 0.5) * 20),
                           int(0x2D + (lum - 0.5) * 20),
                           int(0x30 + (lum - 0.5) * 20)), alpha)

    def foreground(self, color):
        """Map dark text to light text; saturated colours keep their hue."""
        if color is None:
            return None
        p = self.p
        lum = luminance(color)
        sat = saturation(color)
        alpha = color.getAlpha()

        if lum >= 0.45:
            return color                       # already light enough
        if sat > 0.25:
            # a dark accent (dark blue, dark red, ...) -> readable version of it
            hue = Color.RGBtoHSB(color.getRed(), color.getGreen(), color.getBlue(), None)[0]
            return hsb_color(hue, min(0.75, sat), 0.62 + (0.45 - lum) * 0.6, alpha)
        if lum >= 0.22:
            return plain(p.fg_dim, alpha)
        return plain(p.fg, alpha)



# ---------------------------------------------------------------------------
# Dark Metal theme
# ---------------------------------------------------------------------------
class DarkMetalTheme(DefaultMetalTheme):
    """A dark, flat-ish Metal theme (Metal is Burp 1.7's supported L&F family)."""

    def __init__(self, palette):
        DefaultMetalTheme.__init__(self)
        self.p = palette

    def getName(self):
        return "Burp Dark"

    # Metal derives every control colour from these.  Swapping the light/dark
    # ends of the scale is what turns the whole widget set dark.
    def getPrimary1(self):
        return ColorUIResource(self.p.border)

    def getPrimary2(self):
        return ColorUIResource(mix(self.p.bg_panel, self.p.sel_bg, 0.55))

    def getPrimary3(self):
        return ColorUIResource(self.p.sel_bg)

    def getSecondary1(self):
        return ColorUIResource(self.p.bg_panel)

    def getSecondary2(self):
        return ColorUIResource(self.p.bg_secondary)

    def getSecondary3(self):
        return ColorUIResource(self.p.bg_main)

    def getBlack(self):
        return ColorUIResource(self.p.fg)

    def getWhite(self):
        return ColorUIResource(self.p.bg_input)

    def getControlTextColor(self):
        return ColorUIResource(self.p.fg)

    def getInactiveControlTextColor(self):
        return ColorUIResource(self.p.fg_disabled)

    def getInactiveSystemTextColor(self):
        return ColorUIResource(self.p.fg_dim)

    def getMenuDisabledForeground(self):
        return ColorUIResource(self.p.fg_disabled)

    def getSystemTextColor(self):
        return ColorUIResource(self.p.fg)

    def getUserTextColor(self):
        return ColorUIResource(self.p.fg)


# ---------------------------------------------------------------------------
# Burp internals adapter
# ---------------------------------------------------------------------------
class BurpInternals(object):
    """
    Handles the handful of colours Burp 1.7 paints from static fields instead
    of Swing properties:

    * burp.ncd.B  - the page/panel background (#FBFBFB)
    * burp.ncd.C  - the default text colour (black)
    * burp.ncd.A  - the focused-line/subtle background (#F0F0F0)
    * burp.ncd.p  - the search-match background (light orange)
    * burp.ncd.t/l/r - the Burp orange accent
    * burp.c3e.a[].d - the HTTP message editor syntax-highlighting palette

    Everything is optional: if a field is missing (other 1.7.x build), the
    extension logs it and carries on.
    """

    COLOR_FIELDS = (
        # (field, mapper method, extra)
        ("B", "background", False),
        ("C", "foreground", False),
        ("A", "background", False),
        ("p", "highlight", False),
        ("t", "accent", False),
        ("l", "accent", False),
        ("r", "accent", False),
        ("v", "foreground", False),
        ("a", "background", False),
        ("b", "alternate", False),
    )

    def __init__(self, mapper, log, error=None):
        self.mapper = mapper
        self.log = log
        self.error = error or log
        self.ncd = None
        self.c3e = None
        self.saved = []          # (Field, original value)
        self.saved_c3e = []      # (style object, attribute, original)
        self.saved_flags = []
        self.saved_modifiers = []  # (Field, original modifiers)
        self._tried = False

    def _make_writable(self, field):
        """Remove the final modifier so a static field can be reassigned (Java 8).

        This must happen *before* the field is first read or written: the JVM
        builds and caches a FieldAccessor on first use, and that accessor keeps
        the final flag it saw at that moment.
        """
        try:
            current = field.getModifiers()
            if (current & Modifier.FINAL) == 0:
                return True
            modifiers_field = ReflectField.getDeclaredField("modifiers")
            modifiers_field.setAccessible(True)
            modifiers_field.setInt(field, current & ~Modifier.FINAL)
            self.saved_modifiers.append((field, current))
            return True
        except (Exception, Throwable):
            try:
                self.error("modifier patch failed: %s" % str(sys.exc_info()[1]))
            except (Exception, Throwable):
                pass
            return False

    def _prepare(self, field):
        try:
            field.setAccessible(True)
        except (Exception, Throwable):
            pass
        return self._make_writable(field)

    def _read_static(self, field):
        if not self._prepare(field):
            return None
        return field.get(None)

    def _set_static(self, field, value):
        self._prepare(field)
        field.set(None, value)

    # -- loading ------------------------------------------------------------
    def _classes(self):
        if self._tried:
            return
        self._tried = True
        try:
            from burp import ncd
            self.ncd = ncd
        except (Exception, Throwable):
            self.log("burp.ncd not available: " + self._short_exc())
        try:
            from burp import c3e
            self.c3e = c3e
        except (Exception, Throwable):
            self.log("burp.c3e not available: " + self._short_exc())

    def _short_exc(self):
        try:
            return "%s: %s" % (sys.exc_info()[0].__name__, sys.exc_info()[1])
        except (Exception, Throwable):
            return "error"

    # -- patch --------------------------------------------------------------
    def apply(self):
        self._classes()
        self._patch_ncd_colors()
        self._patch_ncd_flags()
        self.patch_syntax()

    def _patch_ncd_colors(self):
        if self.ncd is None or self.saved:
            return
        p = self.mapper.p
        special = {
            "p": p.highlight,
            "t": p.accent,
            "l": p.accent,
            "r": p.accent,
            "b": p.bg_secondary,
        }
        for name, kind, unused in self.COLOR_FIELDS:
            try:
                field = self.ncd.getDeclaredField(name)
            except (Exception, Throwable):
                continue
            try:
                old = self._read_static(field)
            except (Exception, Throwable):
                continue
            if not isinstance(old, Color):
                continue
            if name in special:
                new = special[name]
            elif kind == "foreground":
                new = self.mapper.foreground(old)
            else:
                new = self.mapper.background(old)
            if new is None:
                continue
            try:
                self._set_static(field, plain(new))
                self.saved.append((field, old))
            except (Exception, Throwable):
                self.error("could not patch burp.ncd.%s: %s" % (name, self._short_exc()))

    def _patch_ncd_flags(self):
        """ncd.i tells Burp "we are running under Nimbus"; we are not."""
        if self.ncd is None:
            return
        for name, value in (("i", False),):
            try:
                field = self.ncd.getDeclaredField(name)
                old = self._read_static(field)
                if old != value:
                    self._set_static(field, value)
                    self.saved_flags.append((field, old))
            except (Exception, Throwable):
                pass

    def patch_syntax(self):
        """Map the HTTP message-editor syntax palette (burp.c3e)."""
        if self.c3e is None:
            return
        try:
            styles_field = self.c3e.getDeclaredField("a")
            styles_field.setAccessible(True)
            styles = styles_field.get(None)
        except (Exception, Throwable):
            return
        if styles is None:
            return
        if not self.saved_c3e:
            # the base colour is reused whenever the palette is rebuilt
            try:
                field = self.c3e.getDeclaredField("c")
                old = self._read_static(field)
                if isinstance(old, Color):
                    self._set_static(field, plain(self.mapper.foreground(old)))
                    self.saved_c3e.append(("c", field, old))
            except (Exception, Throwable):
                pass
        seen = set()
        for item in styles:
            if item is None:
                continue
            try:
                old = item.d
            except (Exception, Throwable):
                continue
            if not isinstance(old, Color):
                continue
            key = id(item)
            if key in seen:
                continue
            seen.add(key)
            try:
                item.d = plain(self.mapper.foreground(old))
            except (Exception, Throwable):
                continue
            # remember the very first value only
            found = False
            for entry in self.saved_c3e:
                if entry[0] == "style" and entry[1] is item:
                    found = True
                    break
            if not found:
                self.saved_c3e.append(("style", item, old))

    def revert(self):
        for entry in reversed(self.saved_c3e):
            try:
                if entry[0] == "c":
                    self._set_static(entry[1], entry[2])
                else:
                    entry[1].d = entry[2]
            except (Exception, Throwable):
                pass
        self.saved_c3e = []
        for field, old in reversed(self.saved):
            try:
                self._set_static(field, old)
            except (Exception, Throwable):
                pass
        self.saved = []
        for field, old in reversed(self.saved_flags):
            try:
                self._set_static(field, old)
            except (Exception, Throwable):
                pass
        self.saved_flags = []
        for field, modifiers in reversed(self.saved_modifiers):
            try:
                modifiers_field = ReflectField.getDeclaredField("modifiers")
                modifiers_field.setAccessible(True)
                modifiers_field.setInt(field, modifiers)
            except (Exception, Throwable):
                pass
        self.saved_modifiers = []


# ---------------------------------------------------------------------------
# Renderer wrappers
# ---------------------------------------------------------------------------
class RendererWrapper(TableCellRenderer):
    """Wraps a table cell renderer and repaints its output in dark colours."""

    def __init__(self, inner, themer):
        self.inner = inner
        self.themer = themer

    def getTableCellRendererComponent(self, table, value, selected, focus, row, column):
        component = self.inner.getTableCellRendererComponent(table, value, selected, focus, row, column)
        if component is not None:
            self.themer.decorate_renderer(component, selected, table)
        return component


class _TriangleIcon(Icon):
    """Small sort-order indicator for table headers."""

    def __init__(self, ascending, color):
        self.ascending = ascending
        self.color = color

    def getIconWidth(self):
        return 9

    def getIconHeight(self):
        return 7

    def paintIcon(self, component, graphics, x, y):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            g2.setColor(self.color)
            if self.ascending:
                g2.fillPolygon([x, x + 8, x + 4], [y + 7, y + 7, y], 3)
            else:
                g2.fillPolygon([x, x + 8, x + 4], [y, y, y + 7], 3)
        finally:
            g2.dispose()


class DarkHeaderRenderer(DefaultTableCellRenderer):
    """Table-header renderer that always paints with the dark palette and keeps
    the ascending/descending sort indicator that Nimbus would otherwise draw."""

    def __init__(self, themer):
        DefaultTableCellRenderer.__init__(self)
        self.themer = themer
        self.setOpaque(True)
        self.setHorizontalAlignment(SwingConstants.LEFT)
        self.setBorder(EmptyBorder(0, 5, 0, 5))
        self.setIconTextGap(6)
        self._ascending = _TriangleIcon(True, themer.palette.fg)
        self._descending = _TriangleIcon(False, themer.palette.fg)

    def getTableCellRendererComponent(self, table, value, selected, focus, row, column):
        palette = self.themer.palette
        self.setHorizontalAlignment(SwingConstants.LEFT)
        self.setText("" if value is None else str(value))
        self.setBackground(palette.bg_panel)
        self.setForeground(palette.fg)
        self._ascending.color = palette.fg
        self._descending.color = palette.fg
        try:
            self.setFont(table.getFont())
        except (Exception, Throwable):
            pass
        self.setIcon(self._sort_icon(table, column))
        return self

    def _sort_icon(self, table, column):
        try:
            sorter = table.getRowSorter()
            if sorter is None:
                return None
            keys = sorter.getSortKeys()
            if keys is None:
                return None
            model_column = table.convertColumnIndexToModel(column)
            for index in range(keys.size()):
                key = keys.get(index)
                if key.getColumn() != model_column:
                    continue
                order = str(key.getSortOrder())
                if "ASCENDING" in order:
                    return self._ascending
                if "DESCENDING" in order:
                    return self._descending
                return None
        except (Exception, Throwable):
            return None
        return None


class ListRendererWrapper(object):
    def __init__(self, inner, themer):
        self.inner = inner
        self.themer = themer

    def getListCellRendererComponent(self, listbox, value, index, selected, focus):
        component = self.inner.getListCellRendererComponent(listbox, value, index, selected, focus)
        if component is not None:
            self.themer.decorate_renderer(component, selected, listbox)
        return component


class TreeRendererWrapper(object):
    def __init__(self, inner, themer):
        self.inner = inner
        self.themer = themer

    def getTreeCellRendererComponent(self, tree, value, selected, expanded, leaf, row, focus):
        component = self.inner.getTreeCellRendererComponent(tree, value, selected, expanded, leaf, row, focus)
        if component is not None:
            self.themer.decorate_renderer(component, selected, tree)
        return component


# ---------------------------------------------------------------------------
# Theme engine
# ---------------------------------------------------------------------------
class ThemeEngine(object):
    """
    Installs / removes the dark theme.

    Every change is reversible: the original Look & Feel, UIManager values,
    component colours, renderers and Burp static colours are snapshotted.
    """

    def __init__(self, palette=None, log=None, on_change=None):
        self.palette = palette or Palette()
        self.mapper = ColorMapper(self.palette)
        self.log_sink = log
        self.on_change = on_change
        self.errors = []
        self.messages = []
        self.active = False
        self._snap = WeakHashMap()          # component -> original state
        self._laf_original = None
        self._metal_theme_original = None
        self._user_defaults = {}            # key -> original value (sentinel for "absent")
        self._themed_windows = []           # strong refs are fine: windows are global
        self._hierarchy_listener = None
        self._late_timer = None
        self._dark_defaults = None
        self._tracked = WeakHashMap()     # tables/trees/lists whose renderers we wrap
        self._internals = BurpInternals(self.mapper, self.log, self.error)
        self._selftest_sink = None
        self.late_enabled = True

    # -- logging ------------------------------------------------------------
    def log(self, message):
        try:
            if len(self.messages) < 300:
                self.messages.append(message)
            if self.log_sink is not None:
                self.log_sink(message)
        except (Exception, Throwable):
            pass

    def error(self, message):
        try:
            if len(self.errors) < 100:
                self.errors.append(message)
        except (Exception, Throwable):
            pass
        self.log(message)

    def _error(self, where):
        try:
            lines = [line.strip() for line in traceback.format_exc().strip().splitlines()
                     if line.strip()]
            self.error("%s: %s" % (where, " <- ".join(lines[-3:])))
        except (Exception, Throwable):
            self.error(where)

    # -- public API ---------------------------------------------------------
    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self.active:
            return
        if SwingUtilities.isEventDispatchThread():
            self._switch(enabled)
        else:
            SwingUtilities.invokeLater(_Run(lambda: self._switch(enabled)))

    def toggle(self):
        self.set_enabled(not self.active)

    def update_palette(self, palette):
        """Change the accent colour (re-applies the theme if it is active)."""
        def run():
            was_active = self.active
            callback = self.on_change
            self.on_change = None          # avoid saving a transient "light" theme
            try:
                if was_active:
                    self.set_enabled(False)
                self.palette = palette
                self.mapper = ColorMapper(palette)
                self._internals.mapper = self.mapper
                if was_active:
                    self.set_enabled(True)
            finally:
                self.on_change = callback
            if callback is not None:
                try:
                    callback(self.active)
                except (Exception, Throwable):
                    self._error("palette change")
        on_edt(run)

    def _switch(self, enabled):
        started = time.time()
        focus_manager = KeyboardFocusManager.getCurrentKeyboardFocusManager()
        focus_owner = None
        try:
            focus_owner = focus_manager.getFocusOwner()
        except (Exception, Throwable):
            pass
        if self._selftest_sink is not None:
            try:
                self._selftest_sink("switch on thread=%s edt=%s" % (
                    Thread.currentThread().getName(), SwingUtilities.isEventDispatchThread()))
            except (Exception, Throwable):
                pass
        try:
            if enabled:
                self._apply()
            else:
                self._revert()
        except (Exception, Throwable):
            self._error("theme switch")
        finally:
            self._refresh_windows()
        if focus_owner is not None:
            try:
                if focus_owner.isShowing():
                    focus_owner.requestFocusInWindow()
            except (Exception, Throwable):
                pass
        self.log("%s applied in %.0f ms" % ("dark theme" if enabled else "light theme",
                                            (time.time() - started) * 1000))
        if self.on_change is not None:
            try:
                self.on_change(self.active)
            except (Exception, Throwable):
                self._error("on_change")

    # -- apply --------------------------------------------------------------
    def _apply(self):
        if self.active:
            return
        windows = self._visible_windows()
        self._themed_windows = windows

        # remember the user-level defaults Burp installed *before* switching
        self._user_defaults = {}
        for key in self.USER_KEYS:
            try:
                self._user_defaults[key] = UIManager.get(key)
            except (Exception, Throwable):
                self._user_defaults[key] = None

        # 1. snapshot every component before anything changes
        for window in windows:
            self._snapshot_tree(window)

        # 2. install the dark Look & Feel
        self._install_look_and_feel()

        # 3. rebuild the component trees with the new delegates
        for window in windows:
            try:
                SwingUtilities.updateComponentTreeUI(window)
            except (Exception, Throwable):
                self._error("updateComponentTreeUI")

        # 4. dark UIManager overrides (also covers Burp's own keys)
        self._install_defaults()

        # 5. component-level fixes (Burp's hard-coded colours)
        for window in windows:
            self._theme_tree(window)

        # 6. Burp's static colours + syntax highlighting palette
        self._internals.apply()

        # 7. watch for new windows/dialogs/components
        self._install_hooks()
        self.active = True
        if self._selftest_sink is not None:
            self._selftest_sink("apply finished on thread=%s" % Thread.currentThread().getName())

    def _revert(self):
        if not self.active:
            return
        self.active = False
        self._remove_hooks()
        self._internals.revert()
        self._restore_components()
        self._restore_look_and_feel()
        self._restore_renderers()
        self._snap.clear()
        self._tracked.clear()
        self._themed_windows = []
        self._dark_defaults = None

    # -- windows ------------------------------------------------------------
    @staticmethod
    def _visible_windows():
        out = []
        try:
            for window in Window.getWindows():
                try:
                    if window.isShowing():
                        out.append(window)
                except (Exception, Throwable):
                    pass
        except (Exception, Throwable):
            pass
        return out

    def _refresh_windows(self):
        for window in self._visible_windows():
            try:
                window.invalidate()
                window.validate()
                window.repaint()
            except (Exception, Throwable):
                pass

    # -- Look & Feel --------------------------------------------------------
    def _install_look_and_feel(self):
        current = UIManager.getLookAndFeel()
        self._laf_original = current
        try:
            self._metal_theme_original = MetalLookAndFeel.getCurrentTheme()
        except (Exception, Throwable):
            self._metal_theme_original = None

        MetalLookAndFeel.setCurrentTheme(DarkMetalTheme(self.palette))
        laf = MetalLookAndFeel()
        try:
            laf.initialize()
        except (Exception, Throwable):
            pass
        UIManager.setLookAndFeel(laf)
        self._dark_defaults = UIManager.getLookAndFeelDefaults()
        self._put_raw("CheckBox.icon", _CheckIcon(self.palette, False))
        self._put_raw("RadioButton.icon", _CheckIcon(self.palette, True))

    def _restore_look_and_feel(self):
        # restore the user defaults we added/overwrote
        defaults = UIManager.getDefaults()
        for key, old in self._user_defaults.items():
            try:
                if old is None:
                    defaults.remove(key)
                else:
                    UIManager.put(key, old)
            except (Exception, Throwable):
                pass
        self._user_defaults = {}

        original = self._laf_original
        if original is not None:
            try:
                UIManager.setLookAndFeel(original)
            except (Exception, Throwable):
                self._error("restore look and feel")
        try:
            if self._metal_theme_original is not None:
                MetalLookAndFeel.setCurrentTheme(self._metal_theme_original)
        except (Exception, Throwable):
            pass
        for window in self._visible_windows():
            try:
                SwingUtilities.updateComponentTreeUI(window)
            except (Exception, Throwable):
                self._error("restore component tree")

    # -- UIManager defaults -------------------------------------------------
    # Burp itself writes these keys into the UIManager *user* defaults table
    # (burp.ncd), where they would shadow the look & feel.  They are the only
    # keys we have to restore explicitly; everything else lives in the Metal
    # defaults table, which is discarded when the original L&F is restored.
    USER_KEYS = frozenset([
        "control", "Panel.background", "Dialog.background", "Frame.background",
        "OptionPane.background", "FileChooser.background", "ToolTip.background",
        "Table.alternateRowColor",
    ])

    def _put(self, key, color):
        """Put a colour into the Metal defaults table (discarded on restore)."""
        try:
            defaults = UIManager.getLookAndFeelDefaults()
            defaults.put(key, ColorUIResource(color))
        except (Exception, Throwable):
            pass

    def _put_raw(self, key, value):
        """Put a non-colour value (icon, border, ...) into the Metal defaults table."""
        try:
            UIManager.getLookAndFeelDefaults().put(key, value)
        except (Exception, Throwable):
            pass

    def _put_user(self, key, color):
        """Override a key that Burp put into the user defaults table."""
        try:
            if key not in self._user_defaults:
                self._user_defaults[key] = UIManager.get(key)
            UIManager.put(key, ColorUIResource(color))
        except (Exception, Throwable):
            pass

    def _install_defaults(self):
        p = self.palette
        ui = {
            "control": p.bg_main,
            "info": p.bg_main,
            "menu": p.bg_main,
            "Panel.background": p.bg_main,
            "Dialog.background": p.bg_main,
            "Frame.background": p.bg_main,
            "Window.background": p.bg_main,
            "OptionPane.background": p.bg_main,
            "FileChooser.background": p.bg_main,
            "RootPane.background": p.bg_main,
            "ToolTip.background": p.tooltip_bg,
            "ToolTip.foreground": p.fg,
            "ToolTip.border": p.border,
            "Table.background": p.bg_input,
            "Table.foreground": p.fg,
            "Table.gridColor": p.border,
            "Table.selectionBackground": p.sel_bg,
            "Table.selectionForeground": p.sel_fg,
            "Table.focusCellBackground": p.sel_bg,
            "Table.focusCellForeground": p.sel_fg,
            "Table.alternateRowColor": p.bg_secondary,
            "TableHeader.background": p.bg_panel,
            "TableHeader.foreground": p.fg,
            "TableHeader.selectionBackground": p.bg_panel,
            "TableHeader.selectionForeground": p.fg,
            "TableHeader.gridColor": p.border,
            "TextArea.background": p.bg_input,
            "TextArea.foreground": p.fg,
            "TextArea.selectionBackground": p.sel_bg,
            "TextArea.selectionForeground": p.sel_fg,
            "TextArea.caretForeground": p.fg,
            "TextArea.inactiveForeground": p.fg_dim,
            "TextField.background": p.bg_input,
            "TextField.foreground": p.fg,
            "TextField.selectionBackground": p.sel_bg,
            "TextField.selectionForeground": p.sel_fg,
            "TextField.caretForeground": p.fg,
            "TextField.inactiveForeground": p.fg_dim,
            "TextPane.background": p.bg_input,
            "TextPane.foreground": p.fg,
            "TextPane.selectionBackground": p.sel_bg,
            "TextPane.selectionForeground": p.sel_fg,
            "TextPane.caretForeground": p.fg,
            "EditorPane.background": p.bg_input,
            "EditorPane.foreground": p.fg,
            "EditorPane.selectionBackground": p.sel_bg,
            "EditorPane.selectionForeground": p.sel_fg,
            "EditorPane.caretForeground": p.fg,
            "FormattedTextField.background": p.bg_input,
            "FormattedTextField.foreground": p.fg,
            "PasswordField.background": p.bg_input,
            "PasswordField.foreground": p.fg,
            "List.background": p.bg_input,
            "List.foreground": p.fg,
            "List.selectionBackground": p.sel_bg,
            "List.selectionForeground": p.sel_fg,
            "Tree.background": p.bg_input,
            "Tree.foreground": p.fg,
            "Tree.selectionBackground": p.sel_bg,
            "Tree.selectionForeground": p.sel_fg,
            "Tree.textBackground": p.bg_input,
            "Tree.textForeground": p.fg,
            "Tree.line": p.border,
            "Tree.hash": p.border,
            "ScrollPane.background": p.bg_main,
            "Viewport.background": p.bg_main,
            "SplitPane.background": p.bg_main,
            "TabbedPane.background": p.bg_main,
            "TabbedPane.foreground": p.fg,
            "TabbedPane.selected": p.bg_panel,
            "TabbedPane.selectHighlight": p.bg_panel,
            "TabbedPane.highlight": p.bg_panel,
            "TabbedPane.darkShadow": p.border,
            "TabbedPane.shadow": p.border,
            "TabbedPane.focus": p.accent,
            "Menu.background": p.bg_main,
            "Menu.foreground": p.fg,
            "Menu.selectionBackground": p.sel_bg,
            "Menu.selectionForeground": p.sel_fg,
            "MenuBar.background": p.bg_main,
            "MenuBar.foreground": p.fg,
            "MenuBar.highlight": p.sel_bg,
            "MenuItem.background": p.bg_main,
            "MenuItem.foreground": p.fg,
            "MenuItem.selectionBackground": p.sel_bg,
            "MenuItem.selectionForeground": p.sel_fg,
            "PopupMenu.background": p.bg_panel,
            "PopupMenu.foreground": p.fg,
            "ComboBox.background": p.bg_panel,
            "ComboBox.foreground": p.fg,
            "ComboBox.selectionBackground": p.sel_bg,
            "ComboBox.selectionForeground": p.sel_fg,
            "Button.background": p.bg_panel,
            "Button.foreground": p.fg,
            "Button.select": p.bg_secondary,
            "CheckBox.background": p.bg_main,
            "CheckBox.foreground": p.fg,
            "RadioButton.background": p.bg_main,
            "RadioButton.foreground": p.fg,
            "Label.background": p.bg_main,
            "Label.foreground": p.fg,
            "Separator.background": p.bg_main,
            "Separator.foreground": p.border,
            "Separator.shadow": p.border,
            "Separator.highlight": p.bg_panel,
            "ToolBar.background": p.bg_panel,
            "ToolBar.foreground": p.fg,
            "ProgressBar.background": p.bg_input,
            "ProgressBar.foreground": p.accent,
            "Slider.background": p.bg_main,
            "Slider.foreground": p.fg,
            "Spinner.background": p.bg_input,
            "Spinner.foreground": p.fg,
            "ScrollBar.background": p.bg_main,
            "ScrollBar.foreground": p.bg_panel,
            "ScrollBar.thumb": p.bg_panel,
            "ScrollBar.thumbDarkShadow": p.border,
            "ScrollBar.thumbHighlight": p.border,
            "ScrollBar.thumbShadow": p.shadow,
            "ScrollBar.track": p.bg_main,
            "ScrollBar.trackHighlight": p.bg_secondary,
            "OptionPane.messageForeground": p.fg,
            "ColorChooser.background": p.bg_main,
            "ColorChooser.foreground": p.fg,
            "ColorChooser.swatchesDefaultRecentColor": p.bg_panel,
            "ColorChooser.rgbRedText": p.fg,
            "ColorChooser.rgbGreenText": p.fg,
            "ColorChooser.rgbBlueText": p.fg,
            "ColorChooser.rgbValueText": p.fg,
            "ColorChooser.rgbValueBackground": p.bg_input,
        }
        for key, value in ui.items():
            if value is None:
                continue
            if key in self.USER_KEYS:
                self._put_user(key, value)
            else:
                self._put(key, value)

        # Everything else that is still light (Metal + Burp leftovers) is mapped
        # generically, so unknown components cannot stay white.
        try:
            self._remap_laf_defaults()
        except (Exception, Throwable):
            self._error("remap laf defaults")

    def _remap_laf_defaults(self):
        defaults = UIManager.getLookAndFeelDefaults()
        keys = defaults.keys()
        mapped = []
        while keys.hasMoreElements():
            key = keys.nextElement()
            try:
                value = defaults.get(key)
            except (Exception, Throwable):
                continue
            if not isinstance(value, Color):
                continue
            name = str(key)
            try:
                if name.endswith(".selectionBackground") or name.endswith(".selectionForeground") \
                        or name.endswith(".focusCellBackground"):
                    continue
                if isinstance(value, ColorUIResource) and luminance(value) > 0.9:
                    if name.endswith("background") or name.endswith(".control") or name == "control":
                        mapped.append((key, ColorUIResource(self.mapper.background(value, input_like=("Text" in name or "Editor" in name)))))
                    elif name.endswith("foreground") or name.endswith(".text"):
                        mapped.append((key, ColorUIResource(self.mapper.foreground(value))))
            except (Exception, Throwable):
                continue
        for key, value in mapped:
            try:
                defaults.put(key, value)
            except (Exception, Throwable):
                pass

    # -- component snapshots ------------------------------------------------
    def _state(self, component):
        try:
            state = self._snap.get(component)
        except (Exception, Throwable):
            state = None
        if state is None:
            state = {"text": {}, "table": {}, "renderers": [], "lists": [], "trees": [], "combo": []}
            try:
                state["bg"] = (component.isBackgroundSet(), component.getBackground())
            except (Exception, Throwable):
                state["bg"] = (False, None)
            try:
                state["fg"] = (component.isForegroundSet(), component.getForeground())
            except (Exception, Throwable):
                state["fg"] = (False, None)
            try:
                self._snap.put(component, state)
            except (Exception, Throwable):
                pass
        return state

    def _snapshot_tree(self, root):
        for component in self._walk(root):
            self._state(component)
            self._snapshot_extra(component)

    def _snapshot_extra(self, component):
        state = self._state(component)
        try:
            if isinstance(component, JTextComponent) and not state["text"]:
                for key, getter in (("caret", "getCaretColor"),
                                    ("selection", "getSelectionColor"),
                                    ("selectedText", "getSelectedTextColor"),
                                    ("disabled", "getDisabledTextColor")):
                    try:
                        state["text"][key] = getattr(component, getter)()
                    except (Exception, Throwable):
                        pass
            elif isinstance(component, JTable) and not state["table"]:
                header = component.getTableHeader()
                state["table"] = {
                    "bg": (component.isBackgroundSet(), component.getBackground()),
                    "fg": (component.isForegroundSet(), component.getForeground()),
                    "grid": component.getGridColor(),
                    "selbg": (component.isBackgroundSet(), component.getSelectionBackground()),
                    "selfg": component.getSelectionForeground(),
                    "header_bg": (header.isBackgroundSet(), header.getBackground()) if header else (False, None),
                    "header_fg": (header.isForegroundSet(), header.getForeground()) if header else (False, None),
                    "header_renderer": header.getDefaultRenderer() if header else None,
                }
                if not state["renderers"]:
                    # Remember the application's own renderers *before* the look &
                    # feel switch, otherwise updateUI() replaces them first.
                    classes = [Object, String, Number, Boolean, java_util_Date(),
                               javax_swing_ImageIcon()]
                    try:
                        for index in range(component.getColumnCount()):
                            try:
                                classes.append(component.getColumnClass(index))
                            except (Exception, Throwable):
                                pass
                    except (Exception, Throwable):
                        pass
                    for cls in classes:
                        if cls is None:
                            continue
                        try:
                            renderer = component.getDefaultRenderer(cls)
                            if renderer is not None:
                                state["renderers"].append(("default", cls, renderer))
                        except (Exception, Throwable):
                            pass
                    try:
                        columns = component.getColumnModel()
                        for index in range(columns.getColumnCount()):
                            column = columns.getColumn(index)
                            renderer = column.getCellRenderer()
                            if renderer is not None:
                                state["renderers"].append(("column", index, renderer))
                            header_renderer = column.getHeaderRenderer()
                            if header_renderer is not None:
                                state["renderers"].append(("header", index, header_renderer))
                    except (Exception, Throwable):
                        pass
            elif isinstance(component, JList) and not state["lists"]:
                state["lists"] = [component.getCellRenderer()]
            elif isinstance(component, JTree) and not state["trees"]:
                renderer = component.getCellRenderer()
                state["trees"] = [renderer]
                if isinstance(renderer, DefaultTreeCellRenderer):
                    state["tree_props"] = {
                        "bg": renderer.getBackgroundNonSelectionColor(),
                        "bg_sel": renderer.getBackgroundSelectionColor(),
                        "fg": renderer.getTextNonSelectionColor(),
                        "fg_sel": renderer.getTextSelectionColor(),
                        "border": renderer.getBorderSelectionColor(),
                    }
        except (Exception, Throwable):
            self._error("snapshot " + component.__class__.__name__)

    # -- tree walking -------------------------------------------------------
    def _walk(self, root):
        out = []
        stack = [root]
        guard = 0
        while stack and guard < 60000:
            guard += 1
            component = stack.pop()
            out.append(component)
            if isinstance(component, JMenu):
                try:
                    stack.append(component.getPopupMenu())
                except (Exception, Throwable):
                    pass
            if isinstance(component, JTree):
                try:
                    renderer = component.getCellRenderer()
                    if isinstance(renderer, DefaultTreeCellRenderer):
                        stack.append(renderer)
                except (Exception, Throwable):
                    pass
            if isinstance(component, Container):
                try:
                    children = component.getComponents()
                except (Exception, Throwable):
                    children = []
                for child in reversed(children):
                    stack.append(child)
        return out

    def _theme_tree(self, root):
        for component in self._walk(root):
            try:
                self._theme_component(component)
            except (Exception, Throwable):
                self._error("theme " + component.__class__.__name__)

    # -- per-component theming ---------------------------------------------
    def _theme_component(self, component):
        p = self.palette
        mapper = self.mapper
        name = component.__class__.__name__
        state = self._state(component)
        self._snapshot_extra(component)

        if isinstance(component, JColorChooser):
            return  # the colour chooser has its own themed panels; leave it alone

        # ---- tables -------------------------------------------------------
        if isinstance(component, JTable):
            self._tracked.put(component, True)
            self._theme_table(component, state)
            return

        # ---- trees / lists ------------------------------------------------
        if isinstance(component, JTree):
            self._tracked.put(component, True)
            self._set_bg_fg(component, state, p.bg_input, p.fg)
            self._theme_tree_renderer(component, state)
            return
        if isinstance(component, JList):
            self._tracked.put(component, True)
            self._set_bg_fg(component, state, p.bg_input, p.fg)
            component.setSelectionBackground(p.sel_bg)
            component.setSelectionForeground(p.sel_fg)
            self._theme_list_renderer(component, state)
            return

        # ---- text components ---------------------------------------------
        if isinstance(component, JTextComponent):
            self._set_bg_fg(component, state, p.bg_input, p.fg)
            try:
                component.setCaretColor(p.fg)
            except (Exception, Throwable):
                pass
            try:
                component.setSelectionColor(p.sel_bg)
                component.setSelectedTextColor(p.sel_fg)
            except (Exception, Throwable):
                pass
            try:
                # Burp uses disabled text fields as tab labels; keep them readable
                if name in ("c4h",):
                    component.setDisabledTextColor(p.fg)
                else:
                    component.setDisabledTextColor(p.fg_disabled)
            except (Exception, Throwable):
                pass
            self._theme_document(component, state)
            self._theme_html(component, state)
            if state['text'].get('html') is True:
                self._tracked.put(component, True)
            return

        # ---- menus / tooltips: Metal handles them -------------------------
        if isinstance(component, (JMenu, JPopupMenu, JMenuItem)):
            return

        # ---- Burp's message editor: input background ----------------------
        bg_color = None
        fg_color = None
        if name in ("vzd",):
            bg_color = p.bg_input
            fg_color = p.fg
        elif name in ("p2h",):
            bg_color = p.bg_main
            fg_color = p.fg
        self._set_bg_fg(component, state, bg_color, fg_color)

        # ---- custom JComponents painted by Burp ---------------------------
        if isinstance(component, JComponent):
            try:
                bg = component.getBackground()
                if bg is not None and luminance(bg) > 0.5:
                    component.setBackground(mapper.background(bg, input_like=False))
            except (Exception, Throwable):
                pass
            try:
                fg = component.getForeground()
                if fg is not None and luminance(fg) < 0.45:
                    component.setForeground(mapper.foreground(fg))
            except (Exception, Throwable):
                pass
            border = None
            try:
                border = component.getBorder()
            except (Exception, Throwable):
                pass
            if isinstance(border, TitledBorder):
                try:
                    border.setTitleColor(p.fg_dim)
                except (Exception, Throwable):
                    pass

        # message editor: keep the syntax palette in sync
        if name in ("p2h", "vzd"):
            self._internals.patch_syntax()

    def _set_bg_fg(self, component, state, bg, fg):
        p = self.palette
        mapper = self.mapper
        try:
            current_bg = component.getBackground()
            if bg is not None and (current_bg is None or luminance(current_bg) > 0.42):
                try:
                    component.setBackground(bg)
                except (Exception, Throwable):
                    pass
            elif current_bg is not None and luminance(current_bg) > 0.5:
                component.setBackground(mapper.background(current_bg))
        except (Exception, Throwable):
            pass
        try:
            current_fg = component.getForeground()
            if fg is not None and (current_fg is None or luminance(current_fg) < 0.5):
                component.setForeground(fg)
            elif current_fg is not None and luminance(current_fg) < 0.45:
                component.setForeground(mapper.foreground(current_fg))
        except (Exception, Throwable):
            pass

    # -- tables -------------------------------------------------------------
    def _theme_table(self, table, state):
        p = self.palette
        mapper = self.mapper
        try:
            if table.getBackground() is None or luminance(table.getBackground()) > 0.45:
                table.setBackground(p.bg_input)
            if table.getForeground() is None or luminance(table.getForeground()) < 0.45:
                table.setForeground(p.fg)
            table.setGridColor(p.border)
            table.setSelectionBackground(p.sel_bg)
            table.setSelectionForeground(p.sel_fg)
        except (Exception, Throwable):
            self._error("table colours")

        header = table.getTableHeader()
        if header is not None:
            try:
                if header.getBackground() is None or luminance(header.getBackground()) > 0.45:
                    header.setBackground(p.bg_panel)
                if header.getForeground() is None or luminance(header.getForeground()) < 0.45:
                    header.setForeground(p.fg)
                self._wrap_header_renderer(header, state)
            except (Exception, Throwable):
                self._error("table header")
        # wrap the renderers recorded before the look & feel switch
        for entry in list(state.get("renderers", [])):
            try:
                if entry[0] == "default":
                    if not isinstance(entry[2], RendererWrapper):
                        table.setDefaultRenderer(entry[1], RendererWrapper(entry[2], self))
                elif entry[0] == "column":
                    if not isinstance(entry[2], RendererWrapper):
                        table.getColumnModel().getColumn(entry[1]).setCellRenderer(
                            RendererWrapper(entry[2], self))
                elif entry[0] == "header":
                    if not isinstance(entry[2], RendererWrapper):
                        table.getColumnModel().getColumn(entry[1]).setHeaderRenderer(
                            RendererWrapper(entry[2], self))
            except (Exception, Throwable):
                pass

    def _wrap_header_renderer(self, header, state):
        try:
            if "header_renderer" not in state["table"]:
                state["table"]["header_renderer"] = header.getDefaultRenderer()
            current = header.getDefaultRenderer()
            if isinstance(current, DarkHeaderRenderer):
                return
            header.setDefaultRenderer(DarkHeaderRenderer(self))
        except (Exception, Throwable):
            self._error("header renderer")

    # -- lists/trees renderers ---------------------------------------------
    def _theme_list_renderer(self, listbox, state):
        try:
            renderer = state["lists"][0] if state.get("lists") else listbox.getCellRenderer()
            if renderer is None or isinstance(renderer, ListRendererWrapper):
                return
            listbox.setCellRenderer(ListRendererWrapper(renderer, self))
        except (Exception, Throwable):
            pass

    def _theme_tree_renderer(self, tree, state):
        try:
            renderer = state["trees"][0] if state.get("trees") else tree.getCellRenderer()
            if renderer is None or isinstance(renderer, TreeRendererWrapper):
                return
            tree.setCellRenderer(TreeRendererWrapper(renderer, self))
        except (Exception, Throwable):
            pass
        if isinstance(renderer, DefaultTreeCellRenderer):
            try:
                renderer.setBackgroundNonSelectionColor(self.palette.bg_input)
                renderer.setBackgroundSelectionColor(self.palette.sel_bg)
                renderer.setTextNonSelectionColor(self.palette.fg)
                renderer.setTextSelectionColor(self.palette.sel_fg)
                renderer.setBorderSelectionColor(self.palette.sel_bg)
            except (Exception, Throwable):
                pass

    # -- renderer decoration ------------------------------------------------
    def decorate_renderer(self, component, selected, parent):
        p = self.palette
        try:
            if selected:
                component.setBackground(p.sel_bg)
                component.setForeground(p.sel_fg)
                return
            bg = component.getBackground()
            if bg is not None and luminance(bg) > 0.45:
                component.setBackground(self.mapper.background(bg))
            fg = component.getForeground()
            if fg is not None and luminance(fg) < 0.5:
                component.setForeground(self.mapper.foreground(fg))
        except (Exception, Throwable):
            pass

    # -- styled documents ---------------------------------------------------
    def _theme_document(self, component, state):
        if not isinstance(component, (JTextPane,)):
            return
        if "doc" in state["text"]:
            return
        try:
            document = component.getStyledDocument()
            style = document.getStyle("default")
            if style is None:
                state["text"]["doc"] = "none"
                return
            from javax.swing.text import StyleConstants
            current = style.getAttribute(StyleConstants.Foreground)
            state["text"]["doc"] = current
            if isinstance(current, Color):
                StyleConstants.setForeground(style, self.mapper.foreground(current))
        except (Exception, Throwable):
            state["text"]["doc"] = "error"

    def _theme_html(self, component, state):
        """Burp's advisory/browser panes render HTML with their own (dark) text
        colours.  Add dark CSS rules to the document's stylesheet so the text is
        readable; the rules are overridden with light ones when reverting."""
        if "html" in state["text"]:
            return
        try:
            if not hasattr(component, "getContentType"):
                return
            if component.getContentType() != "text/html":
                return
            document = component.getDocument()
            if not hasattr(document, "getStyleSheet"):
                return
            sheet = document.getStyleSheet()
            if sheet is None:
                return
            self._add_html_dark_rules(sheet)
            state["text"]["html"] = True
            state["text"]["html_doc"] = document
        except (Exception, Throwable):
            state["text"]["html"] = "error"

    def _add_html_dark_rules(self, sheet):
        p = self.palette
        sheet.addRule("body { color: %s; background-color: %s; }" % (
            to_hex(p.fg), to_hex(p.bg_input)))
        for selector in ("p", "li", "td", "th", "div", "span", "pre", "blockquote",
                         "b", "strong", "i", "em", "h1", "h2", "h3", "h4"):
            sheet.addRule("%s { color: %s; }" % (selector, to_hex(p.fg)))
        sheet.addRule("a { color: %s; }" % to_hex(p.accent))
        sheet.addRule("hr { color: %s; }" % to_hex(p.border))

    def _refresh_html(self, component, state):
        """Re-apply the dark HTML rules if Burp replaced the document."""
        try:
            if state['text'].get('html') is not True:
                return
            document = component.getDocument()
            if document is state['text'].get('html_doc'):
                return
            if not hasattr(document, "getStyleSheet"):
                return
            sheet = document.getStyleSheet()
            if sheet is None:
                return
            self._add_html_dark_rules(sheet)
            state["text"]["html_doc"] = document
        except (Exception, Throwable):
            pass

    # -- hooks --------------------------------------------------------------
    def _install_hooks(self):
        if self._hierarchy_listener is None:
            self._hierarchy_listener = _HierarchyListener(self)
            try:
                Toolkit.getDefaultToolkit().addAWTEventListener(
                    self._hierarchy_listener, AWTEvent.HIERARCHY_EVENT_MASK)
            except (Exception, Throwable):
                self._error("hierarchy listener")
        # a light safety net: new windows created after the theme was applied
        if self._late_timer is None:
            self._late_timer = Timer(1200, _Action(lambda e: self._late_tick()))
            self._late_timer.setRepeats(True)
            self._late_timer.start()

    def _remove_hooks(self):
        if self._hierarchy_listener is not None:
            try:
                Toolkit.getDefaultToolkit().removeAWTEventListener(self._hierarchy_listener)
            except (Exception, Throwable):
                pass
            self._hierarchy_listener = None
        if self._late_timer is not None:
            try:
                self._late_timer.stop()
            except (Exception, Throwable):
                pass
            self._late_timer = None

    def _late_tick(self):
        if not self.active:
            return
        try:
            if self.late_enabled:
                for window in self._visible_windows():
                    if window not in self._themed_windows:
                        self._themed_windows.append(window)
                        self._snapshot_tree(window)
                        SwingUtilities.updateComponentTreeUI(window)
                        self._theme_tree(window)
                        self._validate(window)
            self._maintain_renderers()
        except (Exception, Throwable):
            self._error("late window")

    # -- renderer maintenance ----------------------------------------------
    def _maintain_renderers(self):
        """Burp installs many cell renderers lazily (when rows arrive) and can
        replace them later.  Re-wrap anything that is no longer ours."""
        for component in ArrayList(self._tracked.keySet()):
            state = self._snap.get(component)
            if state is None:
                continue
            try:
                if isinstance(component, JTable):
                    self._refresh_table_renderers(component, state)
                elif isinstance(component, JTree):
                    self._refresh_tree_renderer(component, state)
                elif isinstance(component, JList):
                    self._refresh_list_renderer(component, state)
                elif isinstance(component, JTextComponent):
                    self._refresh_html(component, state)
            except (Exception, Throwable):
                self._error("refresh renderers")

    def _record_renderer(self, state, kind, key, original):
        entries = state.setdefault("renderers", [])
        for index, entry in enumerate(entries):
            if entry[0] == kind and entry[1] == key:
                entries[index] = (kind, key, original)
                return
        entries.append((kind, key, original))

    def _refresh_table_renderers(self, table, state):
        palette = self.palette
        try:
            if table.getBackground() is None or luminance(table.getBackground()) > 0.45:
                table.setBackground(palette.bg_input)
            if table.getForeground() is None or luminance(table.getForeground()) < 0.45:
                table.setForeground(palette.fg)
            table.setGridColor(palette.border)
            table.setSelectionBackground(palette.sel_bg)
            table.setSelectionForeground(palette.sel_fg)
        except (Exception, Throwable):
            pass

        classes = [Object, String, Number, Boolean, java_util_Date(), javax_swing_ImageIcon()]
        try:
            for index in range(table.getColumnCount()):
                try:
                    classes.append(table.getColumnClass(index))
                except (Exception, Throwable):
                    pass
        except (Exception, Throwable):
            pass
        for cls in classes:
            if cls is None:
                continue
            try:
                current = table.getDefaultRenderer(cls)
                if current is None or isinstance(current, RendererWrapper):
                    continue
                table.setDefaultRenderer(cls, RendererWrapper(current, self))
                self._record_renderer(state, "default", cls, current)
            except (Exception, Throwable):
                pass

        try:
            columns = table.getColumnModel()
            for index in range(columns.getColumnCount()):
                column = columns.getColumn(index)
                try:
                    current = column.getCellRenderer()
                    if current is not None and not isinstance(current, RendererWrapper):
                        column.setCellRenderer(RendererWrapper(current, self))
                        self._record_renderer(state, "column", index, current)
                except (Exception, Throwable):
                    pass
                try:
                    current = column.getHeaderRenderer()
                    if current is not None and not isinstance(current, RendererWrapper):
                        column.setHeaderRenderer(RendererWrapper(current, self))
                        self._record_renderer(state, "header", index, current)
                except (Exception, Throwable):
                    pass
        except (Exception, Throwable):
            pass

        header = table.getTableHeader()
        if header is not None:
            try:
                if header.getBackground() is None or luminance(header.getBackground()) > 0.45:
                    header.setBackground(palette.bg_panel)
                if header.getForeground() is None or luminance(header.getForeground()) < 0.45:
                    header.setForeground(palette.fg)
                current = header.getDefaultRenderer()
                if not isinstance(current, DarkHeaderRenderer):
                    state["table"]["header_renderer"] = current
                    header.setDefaultRenderer(DarkHeaderRenderer(self))
            except (Exception, Throwable):
                pass

    def _refresh_tree_renderer(self, tree, state):
        try:
            current = tree.getCellRenderer()
            if current is None or isinstance(current, TreeRendererWrapper):
                return
            tree.setCellRenderer(TreeRendererWrapper(current, self))
            if state.get("trees"):
                state["trees"][0] = current
            else:
                state["trees"] = [current]
            if isinstance(current, DefaultTreeCellRenderer):
                if not state.get("tree_props"):
                    state["tree_props"] = {
                        "bg": current.getBackgroundNonSelectionColor(),
                        "bg_sel": current.getBackgroundSelectionColor(),
                        "fg": current.getTextNonSelectionColor(),
                        "fg_sel": current.getTextSelectionColor(),
                        "border": current.getBorderSelectionColor(),
                    }
                current.setBackgroundNonSelectionColor(self.palette.bg_input)
                current.setBackgroundSelectionColor(self.palette.sel_bg)
                current.setTextNonSelectionColor(self.palette.fg)
                current.setTextSelectionColor(self.palette.sel_fg)
                current.setBorderSelectionColor(self.palette.sel_bg)
            tree.repaint()
        except (Exception, Throwable):
            pass

    def _refresh_list_renderer(self, listbox, state):
        try:
            current = listbox.getCellRenderer()
            if current is None or isinstance(current, ListRendererWrapper):
                return
            listbox.setCellRenderer(ListRendererWrapper(current, self))
            if state.get("lists"):
                state["lists"][0] = current
            else:
                state["lists"] = [current]
            listbox.repaint()
        except (Exception, Throwable):
            pass

    @staticmethod
    def _validate(component):
        try:
            component.invalidate()
            component.validate()
            component.repaint()
        except (Exception, Throwable):
            pass

    def theme_late_component(self, component):
        if not self.active or not self.late_enabled:
            return
        try:
            if isinstance(component, Window):
                if component not in self._themed_windows:
                    self._themed_windows.append(component)
                self._snapshot_tree(component)
                try:
                    SwingUtilities.updateComponentTreeUI(component)
                except (Exception, Throwable):
                    pass
                self._theme_tree(component)
                self._validate(component)
                return
            if isinstance(component, Component):
                self._snapshot_tree(component)
                self._theme_tree(component)
                try:
                    component.revalidate()
                except (Exception, Throwable):
                    pass
        except (Exception, Throwable):
            self._error("late component")

    # -- restore ------------------------------------------------------------
    def _lighten(self, color):
        """If a saved colour is one of *our* dark palette values it was recorded
        while the theme was active (component created during dark mode), so let
        the restored look & feel paint the component instead of keeping it dark."""
        if color is None:
            return None
        try:
            if isinstance(color, ColorUIResource):
                return color
            value = (color.getRed(), color.getGreen(), color.getBlue())
            for name in ("bg_main", "bg_secondary", "bg_panel", "bg_input", "fg", "fg_dim",
                         "fg_disabled", "border", "sel_bg", "sel_fg", "highlight",
                         "tooltip_bg", "shadow"):
                candidate = getattr(self.palette, name, None)
                if candidate is not None and \
                        (candidate.getRed(), candidate.getGreen(), candidate.getBlue()) == value:
                    return None
        except (Exception, Throwable):
            pass
        return color

    def _restore_components(self):
        p = self.palette
        components = ArrayList(self._snap.keySet())
        for component in components:
            state = self._snap.get(component)
            if state is None:
                continue
            try:
                # renderers first
                for entry in state.get("renderers", []):
                    try:
                        if entry[0] == "default":
                            current = component.getDefaultRenderer(entry[1])
                            if isinstance(current, RendererWrapper):
                                component.setDefaultRenderer(entry[1], entry[2])
                        elif entry[0] == "column":
                            column = component.getColumnModel().getColumn(entry[1])
                            if isinstance(column.getCellRenderer(), RendererWrapper):
                                column.setCellRenderer(entry[2])
                        elif entry[0] == "header":
                            column = component.getColumnModel().getColumn(entry[1])
                            if isinstance(column.getHeaderRenderer(), RendererWrapper):
                                column.setHeaderRenderer(entry[2])
                    except (Exception, Throwable):
                        pass
                if isinstance(component, JTable) and state.get("table"):
                    table_state = state["table"]
                    header = component.getTableHeader()
                    if header is not None and table_state.get("header_renderer") is not None:
                        try:
                            header.setDefaultRenderer(table_state["header_renderer"])
                        except (Exception, Throwable):
                            pass
                if isinstance(component, JList) and state.get("lists"):
                    try:
                        current = component.getCellRenderer()
                        if isinstance(current, ListRendererWrapper):
                            component.setCellRenderer(state["lists"][0])
                    except (Exception, Throwable):
                        pass
                if isinstance(component, JTree) and state.get("trees"):
                    try:
                        current = component.getCellRenderer()
                        if isinstance(current, TreeRendererWrapper):
                            component.setCellRenderer(state["trees"][0])
                    except (Exception, Throwable):
                        pass
                    props = state.get("tree_props")
                    if props:
                        renderer = state["trees"][0]
                        try:
                            renderer.setBackgroundNonSelectionColor(props["bg"])
                            renderer.setBackgroundSelectionColor(props["bg_sel"])
                            renderer.setTextNonSelectionColor(props["fg"])
                            renderer.setTextSelectionColor(props["fg_sel"])
                            renderer.setBorderSelectionColor(props["border"])
                        except (Exception, Throwable):
                            pass

                # colours
                was_set, old = state.get("bg", (False, None))
                try:
                    component.setBackground(self._lighten(old) if was_set else None)
                except (Exception, Throwable):
                    pass
                was_set, old = state.get("fg", (False, None))
                try:
                    component.setForeground(self._lighten(old) if was_set else None)
                except (Exception, Throwable):
                    pass

                if isinstance(component, JTextComponent):
                    for key, setter in (("caret", "setCaretColor"),
                                        ("selection", "setSelectionColor"),
                                        ("selectedText", "setSelectedTextColor"),
                                        ("disabled", "setDisabledTextColor")):
                        if key in state["text"]:
                            try:
                                getattr(component, setter)(state["text"][key])
                            except (Exception, Throwable):
                                pass
                    if "doc" in state["text"] and state["text"]["doc"] not in ("none", "error"):
                        try:
                            from javax.swing.text import StyleConstants
                            style = component.getStyledDocument().getStyle("default")
                            if isinstance(state["text"]["doc"], Color):
                                StyleConstants.setForeground(style, state["text"]["doc"])
                            else:
                                style.removeAttribute(StyleConstants.Foreground)
                        except (Exception, Throwable):
                            pass
                    if "html" in state["text"]:
                        try:
                            if state["text"]["html"]:
                                document = component.getDocument()
                                sheet = document.getStyleSheet()
                                if sheet is not None:
                                    sheet.addRule("body { color: #000000; background-color: #FFFFFF; }")
                                    for selector in ("p", "li", "td", "th", "div", "span", "pre",
                                                     "blockquote", "b", "strong", "i", "em",
                                                     "h1", "h2", "h3", "h4"):
                                        sheet.addRule("%s { color: #000000; }" % selector)
                                    sheet.addRule("a { color: #0000EE; }")
                                    sheet.addRule("hr { color: #808080; }")
                        except (Exception, Throwable):
                            pass

                if isinstance(component, JTable) and state.get("table"):
                    table_state = state["table"]
                    if not table_state["bg"][0]:
                        try:
                            component.setBackground(None)
                        except (Exception, Throwable):
                            pass
                    else:
                        try:
                            component.setBackground(table_state["bg"][1])
                        except (Exception, Throwable):
                            pass
                    try:
                        component.setGridColor(table_state["grid"])
                    except (Exception, Throwable):
                        pass
                    try:
                        component.setSelectionBackground(table_state["selbg"][1])
                        component.setSelectionForeground(table_state["selfg"])
                    except (Exception, Throwable):
                        pass
            except (Exception, Throwable):
                self._error("restore " + component.__class__.__name__)

    def _restore_renderers(self):
        """Re-install the original renderers after the look & feel was restored
        (Synth UI delegates install their own default renderers on updateUI)."""
        for component in ArrayList(self._snap.keySet()):
            state = self._snap.get(component)
            if state is None:
                continue
            try:
                for entry in state.get("renderers", []):
                    try:
                        if entry[0] == "default":
                            component.setDefaultRenderer(entry[1], entry[2])
                        elif entry[0] == "column":
                            component.getColumnModel().getColumn(entry[1]).setCellRenderer(entry[2])
                        elif entry[0] == "header":
                            component.getColumnModel().getColumn(entry[1]).setHeaderRenderer(entry[2])
                    except (Exception, Throwable):
                        pass
                if isinstance(component, JTable) and state.get("table"):
                    header = component.getTableHeader()
                    original = state["table"].get("header_renderer")
                    if header is not None and original is not None:
                        header.setDefaultRenderer(original)
                if isinstance(component, JList) and state.get("lists"):
                    component.setCellRenderer(state["lists"][0])
                if isinstance(component, JTree) and state.get("trees"):
                    component.setCellRenderer(state["trees"][0])
            except (Exception, Throwable):
                pass

    # -- diagnostics --------------------------------------------------------
    def debug_tables(self):
        lines = []
        for window in self._visible_windows():
            for component in self._walk(window):
                if not isinstance(component, JTable):
                    continue
                try:
                    header = component.getTableHeader()
                    lines.append("table %s rows=%d cols=%d header=%s bg=%s renderer=%s" % (
                        component.__class__.__name__, component.getRowCount(),
                        component.getColumnCount(),
                        header.__class__.__name__ if header else "none",
                        to_hex(header.getBackground()) if header else "-",
                        header.getDefaultRenderer().__class__.__name__ if header else "-"))
                    for index in range(min(3, component.getColumnCount())):
                        column = component.getColumnModel().getColumn(index)
                        lines.append("  col %d cell=%s header=%s" % (
                            index,
                            column.getCellRenderer().__class__.__name__
                            if column.getCellRenderer() is not None else "default",
                            column.getHeaderRenderer().__class__.__name__
                            if column.getHeaderRenderer() is not None else "default"))
                    if header is not None:
                        renderer = header.getDefaultRenderer()
                        sample = renderer.getTableCellRendererComponent(
                            component, "sample", False, False, -1, 0)
                        if sample is not None:
                            lines.append("  header sample %s bg=%s fg=%s" % (
                                sample.__class__.__name__, to_hex(sample.getBackground()),
                                to_hex(sample.getForeground())))
                except (Exception, Throwable):
                    lines.append("table debug failed: " + str(sys.exc_info()[1]))
        return lines

    def diagnostics(self):
        lines = ["Burp Dark Mode diagnostics",
                 "look and feel: %s" % _laf_name(),
                 "theme active: %s" % self.active,
                 "palette accent: %s" % to_hex(self.palette.accent),
                 "windows: %d" % len(self._visible_windows()),
                 "components tracked: %d" % self._snap.size(),
                 "ui defaults overridden: %d" % len(self._user_defaults)]
        lines.append("")
        lines.append("--- component tree ---")
        for window in self._visible_windows():
            try:
                lines.append("window %s (%s)" % (window.__class__.__name__, window))
                self._dump_tree(window, lines, 1)
            except (Exception, Throwable):
                lines.append("window dump failed: " + traceback.format_exc())
        lines.append("")
        lines.append("--- tables ---")
        try:
            lines.extend(self.debug_tables()[:160])
        except (Exception, Throwable):
            lines.append("table dump failed")
        lines.append("")
        lines.append("--- log ---")
        lines.extend(self.messages[-120:])
        lines.append("")
        lines.append("--- errors ---")
        lines.extend(self.errors[-40:])
        return "\n".join(lines)

    def _dump_tree(self, root, lines, depth):
        if len(lines) > 20000:
            return
        try:
            bg = root.getBackground()
            fg = root.getForeground()
            lines.append("%s%s ui=%s bg=%s fg=%s" % (
                "  " * depth, root.__class__.__name__, _ui_name(root),
                to_hex(bg) if bg is not None else "-",
                to_hex(fg) if fg is not None else "-"))
        except (Exception, Throwable):
            pass
        children = []
        if isinstance(root, JMenu):
            try:
                children.append(root.getPopupMenu())
            except (Exception, Throwable):
                pass
        if isinstance(root, Container):
            try:
                children.extend(root.getComponents())
            except (Exception, Throwable):
                pass
        for child in children:
            self._dump_tree(child, lines, depth + 1)




class _HierarchyListener(AWTEventListener):
    """Themes components that appear after dark mode was switched on."""

    def __init__(self, engine):
        self.engine = engine

    def eventDispatched(self, event):
        try:
            if not self.engine.active or not self.engine.late_enabled:
                return
            if not isinstance(event, HierarchyEvent):
                return
            flags = event.getChangeFlags()
            if not (flags & (HierarchyEvent.PARENT_CHANGED | HierarchyEvent.SHOWING_CHANGED
                             | HierarchyEvent.DISPLAYABILITY_CHANGED)):
                return
            component = event.getComponent()
            if component is None:
                return
            try:
                if self.engine._snap.containsKey(component):
                    return
            except (Exception, Throwable):
                pass
            self.engine.theme_late_component(component)
        except (Exception, Throwable):
            pass



def _laf_name():
    try:
        return UIManager.getLookAndFeel().getName()
    except (Exception, Throwable):
        return "?"


def _ui_name(component):
    try:
        if isinstance(component, JComponent) and component.getUI() is not None:
            return component.getUI().__class__.__name__
    except (Exception, Throwable):
        pass
    return "-"


# Java classes used in renderer selection (kept lazy / version-safe)
def java_util_Date():
    try:
        from java.util import Date
        return Date
    except (Exception, Throwable):
        return None


def javax_swing_ImageIcon():
    try:
        from javax.swing import ImageIcon
        return ImageIcon
    except (Exception, Throwable):
        return None


# ---------------------------------------------------------------------------
# Dashboard widgets
# ---------------------------------------------------------------------------
class _HoverListener(MouseAdapter):
    def __init__(self, widget):
        self.widget = widget

    def mouseEntered(self, event):
        self.widget._hover = True
        self.widget.repaint()

    def mouseExited(self, event):
        self.widget._hover = False
        self.widget.repaint()

    def mousePressed(self, event):
        self.widget._pressed = True
        self.widget.repaint()

    def mouseReleased(self, event):
        self.widget._pressed = False
        self.widget.repaint()


class FlatButton(JButton):
    """A small, flat, rounded button that works in both light and dark mode."""

    def __init__(self, text, on_click=None, primary=False):
        JButton.__init__(self, text)
        self._primary = primary
        self._hover = False
        self._pressed = False
        self._bg = rgb(0x2D2D30)
        self._hover_bg = rgb(0x3A3A3D)
        self._border = rgb(0x3E3E42)
        self._text = rgb(0xD4D4D4)
        self.setForeground(self._text)
        self.setOpaque(False)
        self.setContentAreaFilled(False)
        self.setBorderPainted(False)
        self.setFocusPainted(False)
        self.setBorder(EmptyBorder(7, 14, 7, 14))
        self.setCursor(Cursor.getPredefinedCursor(Cursor.HAND_CURSOR))
        self.addMouseListener(_HoverListener(self))
        if on_click is not None:
            self.addActionListener(_Action(on_click))

    def set_palette(self, bg, hover_bg, border, text):
        self._bg = bg
        self._hover_bg = hover_bg
        self._border = border
        self._text = text
        self.setForeground(text)
        self.repaint()

    def paintComponent(self, graphics):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            color = self._bg
            if self._pressed:
                color = mix(self._hover_bg, self._bg, 0.4)
            elif self._hover:
                color = self._hover_bg
            g2.setColor(color)
            g2.fillRoundRect(0, 0, self.getWidth() - 1, self.getHeight() - 1, 8, 8)
            if not self._primary:
                g2.setColor(self._border)
                g2.drawRoundRect(0, 0, self.getWidth() - 1, self.getHeight() - 1, 8, 8)
            g2.setColor(self._text)
        finally:
            g2.dispose()
        self.super__paintComponent(graphics)


class Switch(JToggleButton):
    """Modern iOS-style switch."""

    def __init__(self):
        JToggleButton.__init__(self)
        self.setOpaque(False)
        self.setContentAreaFilled(False)
        self.setBorderPainted(False)
        self.setFocusPainted(False)
        self.setPreferredSize(Dimension(58, 30))
        self.setMinimumSize(Dimension(58, 30))
        self.setMaximumSize(Dimension(58, 30))
        self.setCursor(Cursor.getPredefinedCursor(Cursor.HAND_CURSOR))
        self._on_color = rgb(0x00A8E8)
        self._off_color = rgb(0x555558)
        self._knob = rgb(0xFFFFFF)

    def set_palette(self, on_color, off_color, knob):
        self._on_color = on_color
        self._off_color = off_color
        self._knob = knob
        self.repaint()

    def paintComponent(self, graphics):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            width, height = self.getWidth(), self.getHeight()
            radius = height
            selected = self.getModel().isSelected()
            g2.setColor(self._on_color if selected else self._off_color)
            g2.fillRoundRect(0, 0, width - 1, height - 1, radius, radius)
            diameter = height - 8
            y = 4
            x = (width - diameter - 4) if selected else 4
            g2.setColor(mix(self._on_color, rgb(0x000000), 0.25) if selected
                        else mix(self._off_color, rgb(0x000000), 0.25))
            g2.fillOval(x, y + 1, diameter, diameter)
            g2.setColor(self._knob)
            g2.fillOval(x, y, diameter, diameter)
        finally:
            g2.dispose()


class Pill(JLabel):
    def __init__(self, text):
        JLabel.__init__(self, text)
        self.setOpaque(False)
        self.setBorder(EmptyBorder(4, 10, 4, 10))
        self._bg = rgb(0x2D2D30)
        self._fg = rgb(0xD4D4D4)
        self.setHorizontalAlignment(SwingConstants.CENTER)

    def set_palette(self, bg, fg):
        self._bg = bg
        self._fg = fg
        self.setForeground(fg)
        self.repaint()

    def paintComponent(self, graphics):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            g2.setColor(self._bg)
            g2.fillRoundRect(0, 0, self.getWidth() - 1, self.getHeight() - 1,
                             self.getHeight(), self.getHeight())
        finally:
            g2.dispose()
        self.super__paintComponent(graphics)


class Swatch(JButton):
    def __init__(self, color, on_click):
        JButton.__init__(self)
        self.color = color
        self._selected = False
        self.setOpaque(False)
        self.setContentAreaFilled(False)
        self.setBorderPainted(False)
        self.setFocusPainted(False)
        self.setPreferredSize(Dimension(26, 26))
        self.setMinimumSize(Dimension(26, 26))
        self.setMaximumSize(Dimension(26, 26))
        self.setCursor(Cursor.getPredefinedCursor(Cursor.HAND_CURSOR))
        self.setToolTipText(to_hex(color))
        self.addActionListener(_Action(lambda e: on_click(color)))

    def set_selected(self, selected):
        self._selected = selected
        self.repaint()

    def paintComponent(self, graphics):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            if self._selected:
                g2.setColor(mix(self.color, rgb(0xFFFFFF), 0.55))
                g2.fillRoundRect(0, 0, self.getWidth() - 1, self.getHeight() - 1, 8, 8)
            g2.setColor(self.color)
            g2.fillRoundRect(3, 3, self.getWidth() - 7, self.getHeight() - 7, 6, 6)
        finally:
            g2.dispose()


class _CheckIcon(Icon):
    """Dark checkbox / radio button icon (Metal's own icons are barely visible
    on a dark background)."""

    SIZE = 14

    def __init__(self, palette, radio=False):
        self.palette = palette
        self.radio = radio

    def getIconWidth(self):
        return self.SIZE

    def getIconHeight(self):
        return self.SIZE

    def paintIcon(self, component, graphics, x, y):
        palette = self.palette
        selected = False
        enabled = True
        try:
            if component is not None:
                selected = component.getModel().isSelected()
                enabled = component.isEnabled()
        except (Exception, Throwable):
            pass
        border = palette.border if enabled else palette.fg_disabled
        fill = palette.bg_input if enabled else palette.bg_main
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            size = self.SIZE
            if self.radio:
                g2.setColor(border)
                g2.fillOval(x, y, size - 1, size - 1)
                g2.setColor(fill)
                g2.fillOval(x + 1, y + 1, size - 3, size - 3)
                if selected:
                    g2.setColor(palette.accent if enabled else palette.fg_disabled)
                    g2.fillOval(x + 4, y + 4, size - 8, size - 8)
            else:
                g2.setColor(border)
                g2.fillRoundRect(x, y, size - 1, size - 1, 5, 5)
                g2.setColor(fill)
                g2.fillRoundRect(x + 1, y + 1, size - 3, size - 3, 4, 4)
                if selected:
                    g2.setColor(palette.accent if enabled else palette.fg_disabled)
                    g2.fillRoundRect(x + 1, y + 1, size - 3, size - 3, 4, 4)
                    g2.setColor(rgb(0xFFFFFF) if enabled else palette.bg_panel)
                    g2.setStroke(BasicStroke(2.0, BasicStroke.CAP_ROUND,
                                             BasicStroke.JOIN_ROUND))
                    g2.drawLine(x + 4, y + 7, x + 6, y + 9)
                    g2.drawLine(x + 6, y + 9, x + 10, y + 4)
        finally:
            g2.dispose()


class ColoredSwatch(JPanel):
    def __init__(self, color, label):
        JPanel.__init__(self)
        self.color = color
        self.setPreferredSize(Dimension(52, 26))
        self.setMinimumSize(Dimension(52, 26))
        self.setMaximumSize(Dimension(52, 26))
        self.setOpaque(False)
        self.setToolTipText("%s  %s" % (label, to_hex(color)))

    def paintComponent(self, graphics):
        g2 = graphics.create()
        try:
            g2.setRenderingHint(RenderingHints.KEY_ANTIALIASING,
                                RenderingHints.VALUE_ANTIALIAS_ON)
            g2.setColor(mix(self.color, rgb(0xFFFFFF), 0.25))
            g2.fillRoundRect(0, 0, self.getWidth() - 1, self.getHeight() - 1, 7, 7)
            g2.setColor(self.color)
            g2.fillRoundRect(1, 1, self.getWidth() - 3, self.getHeight() - 3, 6, 6)
        finally:
            g2.dispose()


# ---------------------------------------------------------------------------
# Settings dashboard
# ---------------------------------------------------------------------------
DARK_UI = {
    "root": rgb(0x1E1E1E),
    "card": rgb(0x252526),
    "card_border": rgb(0x3E3E42),
    "text": rgb(0xD4D4D4),
    "dim": rgb(0xA0A0A0),
    "accent": rgb(0x00A8E8),
    "button": rgb(0x2D2D30),
    "button_hover": rgb(0x3A3A3D),
    "button_border": rgb(0x3E3E42),
    "field": rgb(0x181818),
    "success": rgb(0x4CAF50),
    "warning": rgb(0xFFB74D),
    "error": rgb(0xF44336),
    "divider": rgb(0x333335),
}

LIGHT_UI = {
    "root": rgb(0xF3F3F3),
    "card": rgb(0xFFFFFF),
    "card_border": rgb(0xD6D6D6),
    "text": rgb(0x1F1F1F),
    "dim": rgb(0x5F5F5F),
    "accent": rgb(0x0078D4),
    "button": rgb(0xFFFFFF),
    "button_hover": rgb(0xE8E8E8),
    "button_border": rgb(0xC8C8C8),
    "field": rgb(0xFFFFFF),
    "success": rgb(0x2E7D32),
    "warning": rgb(0xE65100),
    "error": rgb(0xC62828),
    "divider": rgb(0xE4E4E4),
}


class SettingsPanel(JPanel):
    ACCENTS = [rgb(0x00A8E8), rgb(0x2ECC71), rgb(0xC586C0), rgb(0xFF8A3D),
               rgb(0xE74C3C), rgb(0x3D8BFD)]

    def __init__(self, extender):
        JPanel.__init__(self)
        self.extender = extender
        self._dark = False
        self._base_font = self.getFont()
        self.setLayout(BorderLayout())
        self._build()
        self.apply_ui(False)

    # -- construction -------------------------------------------------------
    def _font(self, size, bold=False):
        style = Font.BOLD if bold else Font.PLAIN
        return Font(self._base_font.getFamily(), style, size)

    def _label(self, text, size=12, bold=False, dim=False):
        label = JLabel(text)
        label.setFont(self._font(size, bold))
        self._labels.append((label, dim))
        return label

    def _build(self):
        self._labels = []
        self._cards = []
        self.setBackground(DARK_UI["root"])

        header = JPanel(BorderLayout())
        header.setOpaque(False)
        header.setBorder(EmptyBorder(20, 22, 12, 22))

        titles = JPanel()
        titles.setLayout(BoxLayout(titles, BoxLayout.Y_AXIS))
        titles.setOpaque(False)
        self.title = JLabel("Burp Dark Mode")
        self.title.setFont(self._font(20, True))
        self.subtitle = JLabel("Customize the appearance of Burp Suite.")
        self.subtitle.setFont(self._font(12))
        titles.add(self.title)
        titles.add(Box.createRigidArea(Dimension(0, 4)))
        titles.add(self.subtitle)
        header.add(titles, BorderLayout.WEST)

        self.status_pill = Pill("Light theme")
        self.status_pill.setFont(self._font(11, True))
        right = JPanel()
        right.setLayout(BoxLayout(right, BoxLayout.X_AXIS))
        right.setOpaque(False)
        right.add(self.status_pill)
        header.add(right, BorderLayout.EAST)

        content = JPanel(GridBagLayout())
        content.setOpaque(False)
        content.setBorder(EmptyBorder(0, 22, 22, 22))
        self._content = content
        self._grid_row = 0

        self._add_block(self._section("THEME"))
        self._add_block(self._theme_card())
        self._add_block(self._section("APPEARANCE"), 16)
        self._add_block(self._appearance_card())
        self._add_block(self._section("GENERAL SETTINGS"), 16)
        self._add_block(self._general_card())
        self._add_block(self._section("DIAGNOSTICS"), 16)
        self._add_block(self._diagnostics_card())

        self.footer = JLabel("Burp Dark Mode 2.0  |  Legacy Extender API  |  Jython 2.7")
        self.footer.setFont(self._font(10))
        self._add_block(self.footer, 12)

        filler = JPanel()
        filler.setOpaque(False)
        constraints = GridBagConstraints()
        constraints.gridx = 0
        constraints.gridy = self._grid_row
        constraints.weightx = 1.0
        constraints.weighty = 1.0
        constraints.fill = GridBagConstraints.BOTH
        content.add(filler, constraints)

        scroll = JScrollPane(content)
        scroll.setBorder(None)
        scroll.setOpaque(False)
        scroll.getViewport().setOpaque(False)
        scroll.getVerticalScrollBar().setUnitIncrement(18)
        scroll.setHorizontalScrollBarPolicy(JScrollPane.HORIZONTAL_SCROLLBAR_NEVER)

        self.add(header, BorderLayout.NORTH)
        self.add(scroll, BorderLayout.CENTER)

    def _add_block(self, component, gap=0):
        constraints = GridBagConstraints()
        constraints.gridx = 0
        constraints.gridy = self._grid_row
        self._grid_row += 1
        constraints.weightx = 1.0
        constraints.weighty = 0.0
        constraints.fill = GridBagConstraints.HORIZONTAL
        constraints.anchor = GridBagConstraints.NORTHWEST
        constraints.insets = Insets(gap, 0, 0, 0)
        self._content.add(component, constraints)

    def _section(self, text):
        wrapper = JPanel(BorderLayout())
        wrapper.setOpaque(False)
        wrapper.setBorder(EmptyBorder(0, 2, 5, 2))
        label = JLabel(text)
        label.setFont(self._font(10, True))
        self._section_labels = getattr(self, "_section_labels", [])
        self._section_labels.append(label)
        wrapper.add(label, BorderLayout.WEST)
        wrapper.setAlignmentX(Component.LEFT_ALIGNMENT)
        wrapper.setMaximumSize(Dimension(2000, 22))
        return wrapper

    def _card(self):
        card = JPanel()
        card.setLayout(BoxLayout(card, BoxLayout.Y_AXIS))
        card.setOpaque(True)
        card.setBorder(BorderFactory.createCompoundBorder(
            LineBorder(DARK_UI["card_border"], 1),
            EmptyBorder(14, 16, 14, 16)))
        card.setAlignmentX(Component.LEFT_ALIGNMENT)
        self._cards.append(card)
        return card

    def _row(self, height=0):
        row = JPanel()
        row.setLayout(BoxLayout(row, BoxLayout.X_AXIS))
        row.setOpaque(False)
        row.setAlignmentX(Component.LEFT_ALIGNMENT)
        if height:
            row.setMaximumSize(Dimension(2000, height))
        return row

    def _theme_card(self):
        card = self._card()

        row = self._row(44)
        self.switch = Switch()
        self.switch.addActionListener(_Action(lambda e: self._on_switch()))
        label_box = JPanel()
        label_box.setLayout(BoxLayout(label_box, BoxLayout.Y_AXIS))
        label_box.setOpaque(False)
        self.switch_title = self._label("Dark theme", 13, True)
        self.switch_hint = self._label("Switch the whole Burp interface between dark and light.",
                                       11, dim=True)
        label_box.add(self.switch_title)
        label_box.add(Box.createRigidArea(Dimension(0, 2)))
        label_box.add(self.switch_hint)
        row.add(self.switch)
        row.add(Box.createRigidArea(Dimension(14, 0)))
        row.add(label_box)
        row.add(Box.createHorizontalGlue())
        self.theme_state = self._label("Inactive", 11, True)
        row.add(self.theme_state)
        card.add(row)
        card.add(Box.createRigidArea(Dimension(0, 12)))

        buttons = self._row(36)
        self.apply_button = FlatButton("Apply theme", lambda e: self.extender.apply_theme(True), True)
        self.restore_button = FlatButton("Restore default theme", lambda e: self.extender.apply_theme(False))
        self.apply_button.setFont(self._font(11, True))
        self.restore_button.setFont(self._font(11))
        buttons.add(self.apply_button)
        buttons.add(Box.createRigidArea(Dimension(8, 0)))
        buttons.add(self.restore_button)
        buttons.add(Box.createHorizontalGlue())
        card.add(buttons)
        card.add(Box.createRigidArea(Dimension(0, 14)))

        divider = JPanel()
        divider.setOpaque(True)
        divider.setMaximumSize(Dimension(2000, 1))
        divider.setPreferredSize(Dimension(10, 1))
        divider.setAlignmentX(Component.LEFT_ALIGNMENT)
        self._dividers = getattr(self, "_dividers", [])
        self._dividers.append(divider)
        card.add(divider)
        card.add(Box.createRigidArea(Dimension(0, 12)))

        preview = self._row(30)
        preview.add(self._label("Theme preview", 11, True))
        preview.add(Box.createRigidArea(Dimension(12, 0)))
        for color, name in ((rgb(0x1E1E1E), "background"),
                            (rgb(0x252526), "secondary"),
                            (rgb(0x2D2D30), "panel"),
                            (rgb(0x181818), "input"),
                            (rgb(0x264F78), "selection"),
                            (rgb(0x00A8E8), "accent")):
            preview.add(ColoredSwatch(color, name))
            preview.add(Box.createRigidArea(Dimension(6, 0)))
        preview.add(Box.createHorizontalGlue())
        card.add(preview)
        return card

    def _appearance_card(self):
        card = self._card()

        mode = self._row(30)
        mode.add(self._label("Theme mode", 12))
        mode.add(Box.createRigidArea(Dimension(16, 0)))
        self.dark_radio = JRadioButton("Dark theme", True)
        self.light_radio = JRadioButton("Light theme", False)
        group = ButtonGroup()
        group.add(self.dark_radio)
        group.add(self.light_radio)
        self.dark_radio.addActionListener(_Action(lambda e: self.extender.apply_theme(True)))
        self.light_radio.addActionListener(_Action(lambda e: self.extender.apply_theme(False)))
        self._radios = [self.dark_radio, self.light_radio]
        mode.add(self.dark_radio)
        mode.add(Box.createRigidArea(Dimension(10, 0)))
        mode.add(self.light_radio)
        mode.add(Box.createHorizontalGlue())
        card.add(mode)
        card.add(Box.createRigidArea(Dimension(0, 12)))

        accent = self._row(32)
        accent.add(self._label("Accent color", 12))
        accent.add(Box.createRigidArea(Dimension(16, 0)))
        self.swatches = []
        for color in self.ACCENTS:
            swatch = Swatch(color, self.extender.set_accent)
            self.swatches.append(swatch)
            accent.add(swatch)
            accent.add(Box.createRigidArea(Dimension(6, 0)))
        accent.add(Box.createHorizontalGlue())
        card.add(accent)
        card.add(Box.createRigidArea(Dimension(0, 10)))
        self.accent_hint = self._label("Accent colors are used for highlights and focus rings.",
                                       11, dim=True)
        self.accent_hint.setAlignmentX(Component.LEFT_ALIGNMENT)
        card.add(self.accent_hint)
        return card

    def _general_card(self):
        card = self._card()
        self.cb_auto = JCheckBox("Enable dark mode automatically on extension startup")
        self.cb_remember = JCheckBox("Remember the last selected theme")
        self.cb_windows = JCheckBox("Apply theme to newly opened windows and dialogs")
        self.cb_hotkey = JCheckBox("Enable keyboard shortcut  %s" % HOTKEY_TEXT)
        self._checks = [self.cb_auto, self.cb_remember, self.cb_windows, self.cb_hotkey]
        self.cb_auto.addActionListener(_Action(
            lambda e: self.extender.set_option("auto_apply", self.cb_auto.isSelected())))
        self.cb_remember.addActionListener(_Action(
            lambda e: self.extender.set_option("remember", self.cb_remember.isSelected())))
        self.cb_windows.addActionListener(_Action(
            lambda e: self.extender.set_option("new_windows", self.cb_windows.isSelected())))
        self.cb_hotkey.addActionListener(_Action(
            lambda e: self.extender.set_option("hotkey", self.cb_hotkey.isSelected())))
        for box in self._checks:
            box.setFont(self._font(12))
            box.setOpaque(False)
            box.setAlignmentX(Component.LEFT_ALIGNMENT)
            card.add(box)
            card.add(Box.createRigidArea(Dimension(0, 6)))
        return card

    def _diagnostics_card(self):
        card = self._card()
        self.diag_status = self._label("Theme application status: inactive", 12)
        self.diag_detail = self._label("No errors reported.", 11, dim=True)
        self.diag_errors = self._label("", 11, dim=True)
        self.diag_status.setAlignmentX(Component.LEFT_ALIGNMENT)
        self.diag_detail.setAlignmentX(Component.LEFT_ALIGNMENT)
        self.diag_errors.setAlignmentX(Component.LEFT_ALIGNMENT)
        card.add(self.diag_status)
        card.add(Box.createRigidArea(Dimension(0, 4)))
        card.add(self.diag_detail)
        card.add(Box.createRigidArea(Dimension(0, 2)))
        card.add(self.diag_errors)
        card.add(Box.createRigidArea(Dimension(0, 12)))

        buttons = self._row(36)
        self.export_button = FlatButton("Export diagnostic logs", lambda e: self.extender.export_diagnostics())
        self.reset_button = FlatButton("Reset extension settings", lambda e: self.extender.reset_settings())
        self.export_button.setFont(self._font(11))
        self.reset_button.setFont(self._font(11))
        buttons.add(self.export_button)
        buttons.add(Box.createRigidArea(Dimension(8, 0)))
        buttons.add(self.reset_button)
        buttons.add(Box.createHorizontalGlue())
        card.add(buttons)
        return card

    # -- behaviour ----------------------------------------------------------
    def _on_switch(self):
        self.extender.apply_theme(self.switch.isSelected())

    def refresh(self):
        active = self.extender.engine.active
        self._dark = active
        if active:
            self.switch.setSelected(True)
            self.theme_state.setText("ACTIVE")
            self.status_pill.setText("Dark theme active")
            self.diag_status.setText("Theme application status: dark theme applied")
        else:
            self.switch.setSelected(False)
            self.theme_state.setText("Inactive")
            self.status_pill.setText("Light theme")
            self.diag_status.setText("Theme application status: light theme (default)")

        self.dark_radio.setSelected(active)
        self.light_radio.setSelected(not active)

        engine = self.extender.engine
        windows = len(engine._visible_windows())
        tracked = engine._snap.size()
        self.diag_detail.setText("%d window(s) themed  |  %d component(s) tracked  |  Burp L&F: %s"
                                 % (windows, tracked, _laf_name()))
        if engine.errors:
            text = engine.errors[-1]
            if len(text) > 120:
                text = text[:117] + "..."
            self.diag_errors.setText("Last message: " + text)
        else:
            self.diag_errors.setText("No errors reported.")

        # settings
        self.cb_auto.setSelected(self.extender.option("auto_apply"))
        self.cb_remember.setSelected(self.extender.option("remember"))
        self.cb_windows.setSelected(self.extender.option("new_windows"))
        self.cb_hotkey.setSelected(self.extender.option("hotkey"))

        accent = self.extender.engine.palette.accent
        for swatch in self.swatches:
            swatch.set_selected(to_hex(swatch.color).upper() == to_hex(accent).upper())

        self.apply_ui(active)

    def set_status_message(self, text):
        self.diag_errors.setText(text)

    def apply_ui(self, dark):
        ui = DARK_UI if dark else LIGHT_UI
        self._dark = dark
        self.setBackground(ui["root"])
        self.title.setForeground(ui["text"])
        self.subtitle.setForeground(ui["dim"])
        self.footer.setForeground(ui["dim"])
        self.status_pill.set_palette(ui["accent"] if dark else rgb(0xE2E2E2),
                                     rgb(0xFFFFFF) if dark else ui["text"])

        for label in getattr(self, "_section_labels", []):
            label.setForeground(ui["accent"])

        for label, dim in self._labels:
            if label is self.title or label is self.subtitle or label is self.footer:
                continue
            label.setForeground(ui["dim"] if dim else ui["text"])

        for card in self._cards:
            card.setBackground(ui["card"])
            card.setBorder(BorderFactory.createCompoundBorder(
                LineBorder(ui["card_border"], 1), EmptyBorder(14, 16, 14, 16)))

        for divider in getattr(self, "_dividers", []):
            divider.setBackground(ui["divider"])

        for button in (self.apply_button, self.restore_button, self.export_button, self.reset_button):
            button.set_palette(ui["button"], ui["button_hover"], ui["button_border"], ui["text"])
            button.repaint()
        self.apply_button.set_palette(ui["accent"], mix(ui["accent"], rgb(0xFFFFFF), 0.12),
                                      ui["accent"], rgb(0xFFFFFF))

        for box in self._checks:
            box.setForeground(ui["text"])
            box.setBackground(ui["card"])
        for radio in self._radios:
            radio.setForeground(ui["text"])
            radio.setBackground(ui["card"])
            radio.setOpaque(False)

        self.switch.set_palette(ui["accent"], rgb(0x555558) if dark else rgb(0xC5C5C5),
                                rgb(0xFFFFFF))
        self.theme_state.setForeground(ui["success"] if dark else ui["dim"])
        self.diag_status.setForeground(ui["success"] if dark else ui["text"])
        self.diag_detail.setForeground(ui["dim"])
        self.diag_errors.setForeground(ui["dim"])
        self.switch_title.setForeground(ui["text"])
        self.switch_hint.setForeground(ui["dim"])
        self.accent_hint.setForeground(ui["dim"])
        self.repaint()


# ---------------------------------------------------------------------------
# Keyboard shortcut
# ---------------------------------------------------------------------------
class HotKeyDispatcher(KeyEventDispatcher):
    def __init__(self, callback):
        self.callback = callback
        self._last = 0.0
        self.enabled = True

    def dispatchKeyEvent(self, event):
        try:
            if not self.enabled:
                return False
            if event.getID() != KeyEvent.KEY_PRESSED:
                return False
            if event.getKeyCode() != KeyEvent.VK_D:
                return False
            if not (event.isControlDown() and event.isShiftDown()):
                return False
            if event.isAltDown() or event.isMetaDown():
                return False
            now = time.time()
            if now - self._last < 0.35:      # key auto-repeat guard
                return True
            self._last = now
            self.callback()
            return True
        except (Exception, Throwable):
            return False


# ---------------------------------------------------------------------------
# Burp entry point
# ---------------------------------------------------------------------------
class BurpExtender(IBurpExtender, IExtensionStateListener, ITab):
    def __init__(self):
        self.callbacks = None
        self.engine = None
        self.panel = None
        self.hotkey = None
        self.menu_items = []
        self.settings = {}
        self._unloaded = False
        self._selftest_step = 0
        self._selftest_timer = None
        self._selftest_dir = None
        self._test_dialog = None
        self._editor_dialog = None
        self._request_editor = None
        self._response_editor = None

    # -- Burp entry point ---------------------------------------------------
    def registerExtenderCallbacks(self, callbacks):
        self.callbacks = callbacks
        callbacks.setExtensionName(EXT_NAME)
        callbacks.registerExtensionStateListener(self)
        self._load_settings()

        accent = parse_hex(self.settings.get("accent", ""), None)
        self.engine = ThemeEngine(palette=Palette(accent), log=self._log,
                                  on_change=self._theme_changed)
        self.engine.late_enabled = self.option("new_windows")
        self.panel = SettingsPanel(self)
        callbacks.addSuiteTab(self)

        self.hotkey = HotKeyDispatcher(self.toggle_theme)
        self.hotkey.enabled = self.option("hotkey")
        KeyboardFocusManager.getCurrentKeyboardFocusManager().addKeyEventDispatcher(self.hotkey)

        self._log("loaded (%s)" % sys.version.replace("\n", " "))
        self.panel.refresh()

        # menu + auto-apply have to wait until Burp's main window exists
        on_edt(self._install_menu)
        if self.option("auto_apply") and self.settings.get("theme") == "dark":
            timer = Timer(900, _Action(lambda e: self._auto_start()))
            timer.setRepeats(False)
            timer.start()

        if os.environ.get("BURP_DARK_SELFTEST") == "1":
            self._start_selftest()

    def _auto_start(self):
        if self._unloaded:
            return
        if self.option("auto_apply") and self.settings.get("theme") == "dark":
            self.apply_theme(True)

    # -- ITab ---------------------------------------------------------------
    def getTabCaption(self):
        return "Dark Mode"

    def getUiComponent(self):
        return self.panel

    # -- settings -----------------------------------------------------------
    def _load_settings(self):
        keys = ("theme", "remember", "auto_apply", "new_windows", "hotkey", "accent")
        for key in keys:
            try:
                value = self.callbacks.loadExtensionSetting(SETTING_KEYS[key])
            except (Exception, Throwable):
                value = None
            self.settings[key] = value

    def option(self, key):
        default = "1"
        if key in ("theme", "accent"):
            default = ""
        raw = self.settings.get(key)
        if raw is None or raw == "":
            raw = default
        return raw == "1"

    def set_option(self, key, value):
        if key == "remember":
            self.settings["remember"] = "1" if value else "0"
            self._save("remember", self.settings["remember"])
            if value:
                self._save("theme", "dark" if self.engine.active else "light")
            return
        if key == "hotkey":
            self.settings["hotkey"] = "1" if value else "0"
            self._save("hotkey", self.settings["hotkey"])
            if self.hotkey is not None:
                self.hotkey.enabled = bool(value)
            return
        if key == "new_windows":
            self.settings["new_windows"] = "1" if value else "0"
            self._save("new_windows", self.settings["new_windows"])
            if self.engine is not None:
                self.engine.late_enabled = bool(value)
            return
        if key == "auto_apply":
            self.settings["auto_apply"] = "1" if value else "0"
            self._save("auto_apply", self.settings["auto_apply"])
            return

    def _save(self, key, value):
        self.settings[key] = value
        try:
            self.callbacks.saveExtensionSetting(SETTING_KEYS.get(key, SETTING_PREFIX + key), value)
        except (Exception, Throwable):
            self._log("could not save setting " + key)

    # -- theme actions ------------------------------------------------------
    def toggle_theme(self):
        self.apply_theme(not self.engine.active)

    def apply_theme(self, dark):
        if self._unloaded:
            return
        self.engine.set_enabled(bool(dark))

    def set_accent(self, color):
        self._save("accent", to_hex(color).upper())
        self.engine.update_palette(Palette(color))
        self._log("accent colour set to " + to_hex(color))
        if self.panel is not None:
            self.panel.refresh()

    def _theme_changed(self, active):
        if self._selftest_dir:
            self._selftest_log("theme changed: active=%s errors=%d" % (active, len(self.engine.errors)))
        if self.option("remember"):
            self._save("theme", "dark" if active else "light")
        for bar, menu, item in self.menu_items:
            try:
                item.setSelected(active)
            except (Exception, Throwable):
                pass
        if self.panel is not None:
            try:
                self.panel.refresh()
            except (Exception, Throwable):
                pass

    # -- menu ---------------------------------------------------------------
    def _install_menu(self):
        if self._unloaded:
            return
        frame = self._burp_frame()
        if frame is None:
            t = Timer(700, _Action(lambda e: self._install_menu()))
            t.setRepeats(False)
            t.start()
            return
        bar = frame.getJMenuBar()
        if bar is None:
            return
        menu = JMenu("Theme")
        item = JCheckBoxMenuItem("Dark mode  (%s)" % HOTKEY_TEXT)
        item.setSelected(self.engine.active)
        item.addActionListener(_Action(lambda e: self.apply_theme(item.isSelected())))
        menu.add(item)
        bar.add(menu)
        bar.revalidate()
        bar.repaint()
        self.menu_items.append((bar, menu, item))

    @staticmethod
    def _burp_frame():
        try:
            for frame in JFrame.getFrames():
                title = frame.getTitle() or ""
                if title.startswith("Burp Suite") and frame.isShowing():
                    return frame
        except (Exception, Throwable):
            pass
        return None

    def _remove_menu(self):
        for bar, menu, item in self.menu_items:
            try:
                bar.remove(menu)
                bar.revalidate()
                bar.repaint()
            except (Exception, Throwable):
                pass
        self.menu_items = []

    # -- diagnostics --------------------------------------------------------
    def _log(self, message):
        try:
            System.out.println("[%s] %s" % (EXT_NAME, message))
        except (Exception, Throwable):
            pass
        try:
            if self.panel is not None:
                text = message[:160]
                on_edt(lambda: self.panel.set_status_message(text))
        except (Exception, Throwable):
            pass

    def export_diagnostics(self):
        path = os.path.join(os.path.expanduser("~"), "burp_dark_mode_diagnostics.txt")

        def work():
            try:
                text = run_edt_and_wait(lambda: self.engine.diagnostics())
                handle = open(path, "w")
                try:
                    handle.write(text)
                finally:
                    handle.close()
                self._log("diagnostics written to " + path)
            except (Exception, Throwable):
                self._log("could not export diagnostics: " + str(sys.exc_info()[1]))

        thread = Thread(_Run(work))
        thread.setName("DarkModeDiagnostics")
        thread.setDaemon(True)
        thread.start()

    def reset_settings(self):
        for key in ("theme", "remember", "auto_apply", "new_windows", "hotkey", "accent"):
            try:
                self.callbacks.saveExtensionSetting(SETTING_KEYS.get(key, SETTING_PREFIX + key), "")
            except (Exception, Throwable):
                pass
        self.settings = {"theme": "", "remember": "", "auto_apply": "", "new_windows": "",
                         "hotkey": "", "accent": ""}
        self.apply_theme(False)
        if self.hotkey is not None:
            self.hotkey.enabled = True
        self.engine.late_enabled = self.option("new_windows")
        self.engine.palette = Palette()
        self.engine.mapper = ColorMapper(self.engine.palette)
        self._log("settings reset to defaults")
        if self.panel is not None:
            self.panel.refresh()

    # -- unload -------------------------------------------------------------
    def extensionUnloaded(self):
        self._unloaded = True

        def undo():
            try:
                if self._selftest_timer is not None:
                    self._selftest_timer.stop()
            except (Exception, Throwable):
                pass
            try:
                KeyboardFocusManager.getCurrentKeyboardFocusManager().removeKeyEventDispatcher(self.hotkey)
            except (Exception, Throwable):
                pass
            self.hotkey = None
            self._remove_menu()
            try:
                self.engine.on_change = None
                self.engine.set_enabled(False)
            except (Exception, Throwable):
                pass
            self._log("unloaded")

        on_edt(undo)

    # -- self test (development aid) ---------------------------------------
    def _start_selftest(self):
        self._selftest_dir = os.environ.get("BURP_DARK_TEST_DIR") or os.path.join(
            os.path.expanduser("~"), "burp_dark_mode_selftest")
        try:
            if not os.path.isdir(self._selftest_dir):
                os.makedirs(self._selftest_dir)
        except (Exception, Throwable):
            pass
        self._selftest_log("selftest starting")
        self.engine._selftest_sink = self._selftest_log
        self._selftest_log("load thread=%s group=%s edt=%s" % (
            Thread.currentThread().getName(),
            Thread.currentThread().getThreadGroup().getName(),
            SwingUtilities.isEventDispatchThread()))
        runner = Thread(_Run(self._selftest_run))
        runner.setName("DarkModeSelfTest")
        runner.setDaemon(True)
        runner.start()
        watchdog = Thread(_Run(self._selftest_watchdog))
        watchdog.setName("DarkModeWatchdog")
        watchdog.setDaemon(True)
        watchdog.start()

    def _select_own_tab(self):
        try:
            parent = self.panel.getParent()
            while parent is not None and not isinstance(parent, JTabbedPane):
                parent = parent.getParent()
            if parent is not None:
                parent.setSelectedComponent(self.panel)
        except (Exception, Throwable):
            self._selftest_log("select tab failed: " + traceback.format_exc())

    def _selftest_run(self):
        try:
            time.sleep(2.0)
            self._selftest_log("step: main tab, force light")
            run_edt_and_wait(self._select_main_tab)
            run_edt_and_wait(lambda: self.apply_theme(False))
            time.sleep(1.5)

            self._selftest_log("step: capture light baseline")
            run_edt_and_wait(lambda: self._capture("00_light_baseline"))

            self._selftest_log("step: apply dark (hotkey)")
            run_edt_and_wait(self._press_hotkey)
            self._selftest_log("dark active=%s errors=%s" % (self.engine.active, str(self.engine.errors)))
            time.sleep(2.0)
            self._selftest_log("step: capture dark")
            run_edt_and_wait(lambda: self._capture("01_dark"))

            self._selftest_log("step: show dashboard while dark")
            run_edt_and_wait(self._select_own_tab)
            time.sleep(1.0)
            run_edt_and_wait(lambda: self._capture("04_dashboard_dark"))
            run_edt_and_wait(self._select_main_tab)
            time.sleep(1.0)

            self._selftest_log("step: open a new dialog while dark")
            run_edt_and_wait(self._open_test_dialog)
            time.sleep(2.5)
            run_edt_and_wait(lambda: self._capture("05_dialog_dark"))
            run_edt_and_wait(self._close_test_dialog)
            time.sleep(1.0)

            self._selftest_log("step: open message editor while dark")
            run_edt_and_wait(self._open_editor_dialog)
            time.sleep(3.0)
            run_edt_and_wait(lambda: self._capture("06_message_editor_dark"))
            run_edt_and_wait(self._close_editor_dialog)
            time.sleep(1.0)

            self._selftest_log("step: apply light (hotkey)")
            run_edt_and_wait(self._press_hotkey)
            self._selftest_log("light active=%s errors=%s" % (self.engine.active, str(self.engine.errors)))
            time.sleep(2.0)
            self._selftest_log("step: capture light")
            run_edt_and_wait(lambda: self._capture("02_light"))
            self._selftest_log("pixel diff baseline vs restored: %s"
                               % self._pixel_diff("00_light_baseline", "02_light"))

            self._selftest_log("step: dashboard while light")
            run_edt_and_wait(self._select_own_tab)
            time.sleep(1.0)
            run_edt_and_wait(lambda: self._capture("08_dashboard_light"))
            run_edt_and_wait(self._select_main_tab)
            time.sleep(1.0)

            self._selftest_log("step: apply dark again (hotkey)")
            run_edt_and_wait(self._press_hotkey)
            self._selftest_log("dark2 active=%s errors=%s" % (self.engine.active, str(self.engine.errors)))
            time.sleep(2.0)
            self._selftest_log("step: capture dark again")
            run_edt_and_wait(lambda: self._capture("03_dark_again"))

            self._selftest_log("step: change accent colour")
            run_edt_and_wait(lambda: self.set_accent(rgb(0x9B59B6)))
            time.sleep(2.5)
            self._selftest_log("accent active=%s errors=%s" % (self.engine.active,
                                                               str(self.engine.errors)))
            run_edt_and_wait(lambda: self._capture("09_accent"))
            run_edt_and_wait(lambda: self.set_accent(rgb(0x00A8E8)))
            time.sleep(2.0)

            self._selftest_log("step: export diagnostics")
            self.export_diagnostics()
            time.sleep(2.5)

            self._selftest_log("step: unload extension")
            self.extensionUnloaded()
            time.sleep(2.0)
            run_edt_and_wait(lambda: self._capture("07_after_unload"))
            self._selftest_log("done")
        except (Exception, Throwable):
            self._selftest_log("selftest failure: " + traceback.format_exc())
        try:
            handle = open(os.path.join(self._selftest_dir, "done.txt"), "w")
            try:
                handle.write("finished\n")
            finally:
                handle.close()
        except (Exception, Throwable):
            pass

    def _select_main_tab(self):
        try:
            parent = self.panel.getParent()
            while parent is not None and not isinstance(parent, JTabbedPane):
                parent = parent.getParent()
            if parent is not None:
                parent.setSelectedIndex(0)
        except (Exception, Throwable):
            self._selftest_log("select main tab failed: " + traceback.format_exc())

    def _press_hotkey(self):
        try:
            event = KeyEvent(self.panel, KeyEvent.KEY_PRESSED, System.currentTimeMillis(),
                             KeyEvent.CTRL_DOWN_MASK | KeyEvent.SHIFT_DOWN_MASK,
                             KeyEvent.VK_D, 'D')
            self.hotkey.dispatchKeyEvent(event)
        except (Exception, Throwable):
            self._selftest_log("hotkey failed: " + traceback.format_exc())

    def _open_test_dialog(self):
        try:
            frame = self._burp_frame()
            dialog = JDialog(frame, "Dark Mode Test Dialog", False)
            dialog.setLayout(BorderLayout(0, 6))
            table = JTable([["one", "two"], ["three", "four"]], ["Column A", "Column B"])
            table_scroll = JScrollPane(table)
            table_scroll.setPreferredSize(Dimension(340, 80))
            text = JTextArea("A dialog opened after dark mode was applied.\\nSecond line.")
            dialog.add(table_scroll, BorderLayout.NORTH)
            dialog.add(JScrollPane(text), BorderLayout.CENTER)
            dialog.add(JLabel("  Late dialog label"), BorderLayout.SOUTH)
            dialog.setSize(360, 220)
            dialog.setLocationRelativeTo(frame)
            dialog.setVisible(True)
            self._test_dialog = dialog
        except (Exception, Throwable):
            self._selftest_log("dialog failed: " + traceback.format_exc())

    def _close_test_dialog(self):
        try:
            if self._test_dialog is not None:
                self._test_dialog.dispose()
                self._test_dialog = None
        except (Exception, Throwable):
            pass

    def _open_editor_dialog(self):
        """Opens Burp's own message editor with a highlighted request/response."""
        try:
            frame = self._burp_frame()
            helpers = self.callbacks.getHelpers()
            dialog = JDialog(frame, "Message editor test", False)
            dialog.setLayout(BorderLayout())
            tabs = JTabbedPane()
            request = ("GET /login?user=admin&pass=secret HTTP/1.1\r\n"
                       "Host: example.com\r\n"
                       "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                       "Cookie: session=abc123; theme=dark\r\n"
                       "Accept: text/html,application/xhtml+xml\r\n"
                       "\r\n")
            response = ("HTTP/1.1 200 OK\r\n"
                        "Date: Sun, 15 Nov 2026 10:00:00 GMT\r\n"
                        "Content-Type: text/html; charset=UTF-8\r\n"
                        "Set-Cookie: session=abc123; HttpOnly\r\n"
                        "\r\n"
                        "<html><body><h1>Hello</h1></body></html>\r\n")
            self._request_editor = self.callbacks.createMessageEditor(None, True)
            self._request_editor.setMessage(helpers.stringToBytes(request), True)
            tabs.addTab("Request", self._request_editor.getComponent())
            self._response_editor = self.callbacks.createMessageEditor(None, True)
            self._response_editor.setMessage(helpers.stringToBytes(response), False)
            tabs.addTab("Response", self._response_editor.getComponent())
            dialog.add(tabs, BorderLayout.CENTER)
            dialog.setSize(620, 340)
            dialog.setLocationRelativeTo(frame)
            dialog.setVisible(True)
            self._editor_dialog = dialog
        except (Exception, Throwable):
            self._selftest_log("editor dialog failed: " + traceback.format_exc())

    def _close_editor_dialog(self):
        try:
            if self._editor_dialog is not None:
                self._editor_dialog.dispose()
                self._editor_dialog = None
        except (Exception, Throwable):
            pass

    def _pixel_diff(self, first, second):
        try:
            directory = self._selftest_dir
            names = sorted([name for name in os.listdir(directory)
                            if name.startswith(first) and name.endswith(".png")])
            if not names:
                return "no baseline image"
            after_name = names[0].replace(first, second, 1)
            a = ImageIO.read(File(os.path.join(directory, names[0])))
            b = ImageIO.read(File(os.path.join(directory, after_name)))
            if a is None or b is None:
                return "image missing"
            if a.getWidth() != b.getWidth() or a.getHeight() != b.getHeight():
                return "size differs (%dx%d vs %dx%d)" % (a.getWidth(), a.getHeight(),
                                                          b.getWidth(), b.getHeight())
            differing = 0
            for x in range(0, a.getWidth(), 2):
                for y in range(0, a.getHeight(), 2):
                    if a.getRGB(x, y) != b.getRGB(x, y):
                        differing += 1
            total = (a.getWidth() / 2) * (a.getHeight() / 2)
            return "%d / %d sampled pixels differ (%.2f%%)" % (
                differing, total, 100.0 * differing / max(1, total))
        except (Exception, Throwable):
            return "diff failed: " + traceback.format_exc()

    def _selftest_watchdog(self):
        """Dumps thread stacks periodically, to diagnose EDT hangs."""
        while True:
            try:
                time.sleep(6)
                self._selftest_log("heartbeat")
                traces = Thread.getAllStackTraces()
                handle = open(os.path.join(self._selftest_dir, "threads.txt"), "w")
                try:
                    for thread in traces.keySet():
                        try:
                            name = thread.getName()
                            state = thread.getState()
                        except (Exception, Throwable):
                            continue
                        handle.write("thread: %s state=%s edt=%s\n"
                                     % (name, state, name.startswith("AWT-EventQueue")))
                        if name.startswith("AWT-EventQueue") or "self-test" in name:
                            for element in traces.get(thread):
                                handle.write("  at %s\n" % element)
                finally:
                    handle.close()
            except (Exception, Throwable):
                pass

    def _selftest_log(self, text):
        try:
            handle = open(os.path.join(self._selftest_dir, "probe.log"), "a")
            try:
                handle.write(text + "\n")
            finally:
                handle.close()
        except (Exception, Throwable):
            pass

    def _capture(self, label):
        try:
            text = self.engine.diagnostics()
            handle = open(os.path.join(self._selftest_dir, label + ".txt"), "w")
            try:
                handle.write(text)
            finally:
                handle.close()
        except (Exception, Throwable):
            self._selftest_log("dump failed: " + traceback.format_exc())
        index = 0
        for window in Window.getWindows():
            try:
                if not window.isShowing():
                    continue
                if isinstance(window, RootPaneContainer):
                    root = window.getRootPane()
                else:
                    root = window
                if root is None or root.getWidth() <= 0 or root.getHeight() <= 0:
                    continue
                image = BufferedImage(root.getWidth(), root.getHeight(), BufferedImage.TYPE_INT_RGB)
                graphics = image.createGraphics()
                root.paint(graphics)
                graphics.dispose()
                name = "%s_%02d_%s.png" % (label, index, window.__class__.__name__)
                ImageIO.write(image, "png", File(os.path.join(self._selftest_dir, name)))
                index += 1
            except (Exception, Throwable):
                self._selftest_log("capture failed: " + traceback.format_exc())























