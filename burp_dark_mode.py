# -*- coding: utf-8 -*-
# Dark mode for Burp Suite 1.7.x (Jython 2.7 extension).
#
# Toggle at runtime, no restart: Ctrl+Shift+D, the "Theme" menu in Burp's menu
# bar, or the "Dark Mode" suite tab. The choice is saved with the extension and
# re-applied on the next start. Unloading the extension restores the light UI.
#
# How it works: a dark Metal theme / Nimbus palette / generic UIManager colors
# are installed and the L&F is refreshed, then every component that still has a
# light explicit colour is remapped (neutral colours are inverted, saturated
# ones such as Burp's orange or issue colours are kept and just toned down).
# Originals are remembered per component so switching back is exact.

try:
    from burp import IBurpExtender, IExtensionStateListener, ITab
except ImportError:  # lets tests/ import this file outside Burp
    class IBurpExtender(object): pass
    class IExtensionStateListener(object): pass
    class ITab(object): pass

from java.awt import (Color, Container, KeyboardFocusManager, KeyEventDispatcher, Toolkit,
                      AWTEvent, Window, BorderLayout, FlowLayout)
from java.awt.event import ActionListener, AWTEventListener, HierarchyEvent, KeyEvent
from java.lang import Runnable
from java.util import ArrayList, WeakHashMap
from javax.swing import (JButton, JCheckBoxMenuItem, JColorChooser, JLabel, JList, JMenu,
                         JMenuBar, JPanel, JTable, JTree, SwingUtilities, Timer, UIManager)
from javax.swing.border import CompoundBorder, LineBorder, TitledBorder
from javax.swing.plaf import ColorUIResource
from javax.swing.plaf.metal import DefaultMetalTheme, MetalLookAndFeel, OceanTheme
from javax.swing.text import JTextComponent
from javax.swing.tree import DefaultTreeCellRenderer

EXT_NAME = "Dark Mode"
SETTING_KEY = "dark_mode"

# Palette (also used for the Metal theme and the Nimbus keys).
BG = Color(0x2b, 0x2b, 0x2b)
BG_TEXT = Color(0x1e, 0x1e, 0x1e)
FG = Color(0xd4, 0xd4, 0xd4)
FG_DISABLED = Color(0x77, 0x77, 0x77)
SEL_BG = Color(0x36, 0x58, 0x80)
BORDER = Color(0x55, 0x55, 0x55)


# ----------------------------------------------------------------------------
# Colour mapping
# ----------------------------------------------------------------------------
def _hsb(c):
    return Color.RGBtoHSB(c.getRed(), c.getGreen(), c.getBlue(), None)


def _make(h, s, b, like):
    rgb = Color.HSBtoRGB(h, max(0.0, min(1.0, s)), max(0.0, min(1.0, b)))
    out = Color((rgb >> 16) & 0xff, (rgb >> 8) & 0xff, rgb & 0xff, like.getAlpha())
    return ColorUIResource(out) if isinstance(like, ColorUIResource) else out


def _is_res(c):
    return isinstance(c, ColorUIResource)


def xf_bg(c):
    h, s, b = _hsb(c)
    if s < 0.25:                                  # neutral: invert into dark range
        if _is_res(c) and b < 0.5:
            return c                              # already dark (from the dark theme)
        return _make(h, s, 0.11 + (1.0 - b) * 0.25, c)
    if b > 0.6:                                   # light tinted: darken, keep hue
        return _make(h, s, b * 0.55, c)
    return c


def xf_fg(c):
    h, s, b = _hsb(c)
    if s < 0.25:
        if _is_res(c) and b > 0.5:
            return c
        return _make(h, s, 0.86 - b * 0.45, c)
    if b < 0.55:                                  # dark saturated (links etc.): lift
        return _make(h, s, 0.55 + b * 0.4, c)
    return c


def xf_border(c):
    h, s, b = _hsb(c)
    if s < 0.25:
        return _make(h, s, 0.18 + (1.0 - b) * 0.4, c)
    return xf_bg(c)


def xf_sel(c):
    h, s, b = _hsb(c)
    if b < 0.45 and _is_res(c):
        return c
    return _make(h, max(s, 0.5), 0.5, c)


_XF = {"bg": xf_bg, "fg": xf_fg, "border": xf_border, "sel": xf_sel}

# (component class, [(property, kind)]) for colours beyond background/foreground.
_EXTRA = [
    (JTextComponent, [("CaretColor", "fg"), ("SelectionColor", "sel"),
                      ("SelectedTextColor", "fg"), ("DisabledTextColor", "fg")]),
    (JTable, [("GridColor", "border"), ("SelectionBackground", "sel"),
              ("SelectionForeground", "fg")]),
    (JList, [("SelectionBackground", "sel"), ("SelectionForeground", "fg")]),
    (DefaultTreeCellRenderer, [("BackgroundNonSelectionColor", "bg"),
                               ("BackgroundSelectionColor", "sel"),
                               ("TextNonSelectionColor", "fg"), ("TextSelectionColor", "fg"),
                               ("BorderSelectionColor", "border")]),
]


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        self.fn()


class _Action(ActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, e):
        self.fn()


# ----------------------------------------------------------------------------
# Dark Metal theme (used when Burp runs on the Metal look and feel)
# ----------------------------------------------------------------------------
class DarkMetalTheme(DefaultMetalTheme):
    def getName(self):
        return "Burp Dark"

    def getPrimary1(self):
        return ColorUIResource(0x8a, 0x8f, 0x98)

    def getPrimary2(self):
        return ColorUIResource(0x4b, 0x6e, 0xaf)

    def getPrimary3(self):
        return ColorUIResource(SEL_BG)

    def getSecondary1(self):
        return ColorUIResource(BORDER)

    def getSecondary2(self):
        return ColorUIResource(0x3c, 0x3f, 0x41)

    def getSecondary3(self):
        return ColorUIResource(BG)

    def getBlack(self):
        return ColorUIResource(FG)

    def getWhite(self):
        return ColorUIResource(BG_TEXT)

    def getInactiveControlTextColor(self):
        return ColorUIResource(FG_DISABLED)

    def getMenuDisabledForeground(self):
        return ColorUIResource(FG_DISABLED)


# ----------------------------------------------------------------------------
# Controller
# ----------------------------------------------------------------------------
class DarkModeController(object):
    def __init__(self, on_change=None):
        self.enabled = False
        self.on_change = on_change
        self.saved = WeakHashMap()        # component -> original state (weak: no leaks)
        self.put_keys = []                # UIManager keys we set, removed on switch back
        self.old_theme = None
        self.listener = None
        self.titles = []

    # -- public --------------------------------------------------------------
    def set_enabled(self, on):
        if SwingUtilities.isEventDispatchThread():
            self._switch(on)
        else:
            SwingUtilities.invokeLater(_Run(lambda: self._switch(on)))

    def toggle(self):
        self.set_enabled(not self.enabled)

    # -- switching -----------------------------------------------------------
    def _switch(self, on):
        if on == self.enabled:
            return
        try:
            if on:
                self._enable()
            else:
                self._disable()
        finally:
            for w in Window.getWindows():
                w.repaint()
        if self.on_change:
            self.on_change(self.enabled)

    def _snapshot_titles(self, c):
        # updateUI rewrites theme-default TitledBorder colours, so grab them beforehand.
        b = getattr(c, "getBorder", lambda: None)()
        while b is not None:
            if isinstance(b, TitledBorder):
                self.titles.append((b, b.getTitleColor()))
            b = b.getOutsideBorder() if isinstance(b, CompoundBorder) else None
        if isinstance(c, Container):
            for ch in c.getComponents():
                self._snapshot_titles(ch)

    def _enable(self):
        self.titles = []
        for w in Window.getWindows():
            self._snapshot_titles(w)
        self._uimanager_dark()
        self._refresh_laf()
        self.enabled = True
        for w in Window.getWindows():
            self.theme_tree(w)
        self.listener = _HierarchyListener(self)
        Toolkit.getDefaultToolkit().addAWTEventListener(self.listener, AWTEvent.HIERARCHY_EVENT_MASK)

    def _disable(self):
        if self.listener is not None:
            Toolkit.getDefaultToolkit().removeAWTEventListener(self.listener)
            self.listener = None
        self.enabled = False
        self._uimanager_light()
        self._restore_components()
        self._refresh_laf()

    # -- UIManager / L&F -----------------------------------------------------
    def _laf_name(self):
        return UIManager.getLookAndFeel().getName()

    def _uimanager_dark(self):
        name = self._laf_name()
        if name == "Metal":
            try:
                self.old_theme = MetalLookAndFeel.getCurrentTheme()
            except Exception:
                self.old_theme = OceanTheme()
            MetalLookAndFeel.setCurrentTheme(DarkMetalTheme())
        elif name == "Nimbus":
            for k, v in (("control", BG), ("info", BG), ("nimbusBase", Color(0x14, 0x16, 0x18)),
                         ("nimbusBlueGrey", Color(0x2e, 0x30, 0x32)),
                         ("nimbusLightBackground", BG_TEXT), ("text", FG),
                         ("nimbusSelectionBackground", SEL_BG), ("nimbusSelection", SEL_BG),
                         ("nimbusFocus", Color(0x4b, 0x6e, 0xaf)), ("nimbusDisabledText", FG_DISABLED),
                         ("nimbusBorder", BORDER), ("menu", BG), ("textForeground", FG),
                         ("textBackground", SEL_BG), ("textHighlight", SEL_BG),
                         ("Table.background", BG_TEXT), ("Table.alternateRowColor", Color(0x25, 0x25, 0x25))):
                self._put(k, ColorUIResource(v))
        else:
            for k in list(UIManager.getDefaults().keySet()):
                v = UIManager.get(k)
                if not isinstance(v, Color):
                    continue
                n = str(k).lower()
                if "selectionbackground" in n or n.endswith("selectionbackground"):
                    kind = "sel"
                elif n.endswith("foreground") or n.endswith("text") or "textforeground" in n:
                    kind = "fg"
                else:
                    kind = "bg"
                self._put(k, ColorUIResource(_XF[kind](v)))

    def _uimanager_light(self):
        for k in self.put_keys:
            UIManager.put(k, None)
        self.put_keys = []
        if self.old_theme is not None:
            MetalLookAndFeel.setCurrentTheme(self.old_theme)
            self.old_theme = None

    def _put(self, key, value):
        UIManager.put(key, value)
        self.put_keys.append(key)

    def _refresh_laf(self):
        # A fresh L&F instance re-reads theme/defaults; then rebuild every UI delegate.
        cls = UIManager.getLookAndFeel().__class__
        UIManager.setLookAndFeel(cls.__module__ + "." + cls.__name__)
        for w in Window.getWindows():
            SwingUtilities.updateComponentTreeUI(w)

    # -- per-component theming ----------------------------------------------
    def theme_tree(self, c):
        if isinstance(c, JColorChooser):
            return
        self.theme_one(c)
        if isinstance(c, JMenu):
            self.theme_tree(c.getPopupMenu())
        if isinstance(c, JTree):
            r = c.getCellRenderer()
            if isinstance(r, DefaultTreeCellRenderer):
                self._extras(r, self._state(r))
        if isinstance(c, Container):
            for ch in c.getComponents():
                self.theme_tree(ch)

    def _state(self, c):
        st = self.saved.get(c)
        if st is None:
            st = {"extra": [], "tb": [], "border": None}
            self.saved.put(c, st)
        return st

    def theme_one(self, c):
        if self.saved.containsKey(c):
            return
        st = self._state(c)
        try:
            bg = c.getBackground()
            if bg is not None:
                st["bg"] = (c.isBackgroundSet(), bg)
                nb = xf_bg(bg)
                if nb is not bg:
                    c.setBackground(nb)
            fg = c.getForeground()
            if fg is not None:
                st["fg"] = (c.isForegroundSet(), fg)
                nf = xf_fg(fg)
                if nf is not fg:
                    c.setForeground(nf)
            self._extras(c, st)
            getb = getattr(c, "getBorder", None)
            if getb is not None:
                ob = getb()
                nb2 = self._border(ob, st)
                if nb2 is not ob:
                    st["border"] = (ob,)
                    c.setBorder(nb2)
        except Exception:
            pass                                    # never break Burp over one odd component

    def _extras(self, c, st):
        for cls, props in _EXTRA:
            if isinstance(c, cls):
                for prop, kind in props:
                    try:
                        old = getattr(c, "get" + prop)()
                        if old is None:
                            continue
                        new = _XF[kind](old)
                        if new is not old:
                            st["extra"].append(("set" + prop, old))
                            getattr(c, "set" + prop)(new)
                    except Exception:
                        pass

    def _border(self, b, st):
        if isinstance(b, TitledBorder):
            tc = b.getTitleColor()
            inner = b.getBorder()
            st["tb"].append((b, tc, inner))
            if tc is not None:
                b.setTitleColor(xf_fg(tc))
            ni = self._border(inner, st)
            if ni is not inner:
                b.setBorder(ni)
            return b
        if isinstance(b, LineBorder):
            return LineBorder(xf_border(b.getLineColor()), b.getThickness(), b.getRoundedCorners())
        if isinstance(b, CompoundBorder):
            o = self._border(b.getOutsideBorder(), st)
            i = self._border(b.getInsideBorder(), st)
            if o is not b.getOutsideBorder() or i is not b.getInsideBorder():
                return CompoundBorder(o, i)
        return b

    def _restore_components(self):
        for c in ArrayList(self.saved.keySet()):
            st = self.saved.get(c)
            if st is None:
                continue
            try:
                if "bg" in st:
                    c.setBackground(st["bg"][1] if st["bg"][0] else None)
                if "fg" in st:
                    c.setForeground(st["fg"][1] if st["fg"][0] else None)
                for setter, old in st["extra"]:
                    getattr(c, setter)(old)
                for b, tc, inner in st["tb"]:
                    b.setBorder(inner)
                if st["border"] is not None:
                    c.setBorder(st["border"][0])
            except Exception:
                pass
        for b, tc in self.titles:
            b.setTitleColor(tc)
        self.titles = []
        self.saved.clear()


class _HierarchyListener(AWTEventListener):
    """Themes components added after dark mode was switched on (dialogs, popups, new tabs)."""

    def __init__(self, ctl):
        self.ctl = ctl

    def eventDispatched(self, e):
        try:
            if not self.ctl.enabled or not isinstance(e, HierarchyEvent):
                return
            if e.getID() != HierarchyEvent.HIERARCHY_CHANGED:
                return
            if e.getChangeFlags() & (HierarchyEvent.PARENT_CHANGED | HierarchyEvent.SHOWING_CHANGED):
                c = e.getChanged()
                if not self.ctl.saved.containsKey(c):
                    self.ctl.theme_tree(c)
        except Exception:
            pass


class _HotKey(KeyEventDispatcher):
    def __init__(self, ctl):
        self.ctl = ctl

    def dispatchKeyEvent(self, e):
        if (e.getID() == KeyEvent.KEY_PRESSED and e.getKeyCode() == KeyEvent.VK_D
                and e.isControlDown() and e.isShiftDown()):
            self.ctl.toggle()
            return True
        return False


# ----------------------------------------------------------------------------
# Burp entry point
# ----------------------------------------------------------------------------
class BurpExtender(IBurpExtender, IExtensionStateListener, ITab):
    def registerExtenderCallbacks(self, callbacks):
        self.callbacks = callbacks
        callbacks.setExtensionName(EXT_NAME)
        callbacks.registerExtensionStateListener(self)

        self.ctl = DarkModeController(on_change=self._changed)
        self.menu_items = []
        self.hotkey = _HotKey(self.ctl)
        KeyboardFocusManager.getCurrentKeyboardFocusManager().addKeyEventDispatcher(self.hotkey)

        self.button = JButton("Switch to dark mode", actionPerformed=lambda e: self.ctl.toggle())
        self.panel = JPanel(BorderLayout())
        top = JPanel(FlowLayout(FlowLayout.LEFT))
        top.add(self.button)
        top.add(JLabel("Shortcut: Ctrl+Shift+D"))
        self.panel.add(top, BorderLayout.NORTH)
        callbacks.addSuiteTab(self)

        SwingUtilities.invokeLater(_Run(self._install_menu))
        if callbacks.loadExtensionSetting(SETTING_KEY) == "1":
            # Delay a little so Burp's main frame is fully built before we restyle it.
            t = Timer(800, _Action(lambda: self.ctl.set_enabled(True)))
            t.setRepeats(False)
            t.start()

    def _install_menu(self):
        from java.awt import Frame
        for f in Frame.getFrames():
            title = f.getTitle() or ""
            bar = f.getJMenuBar() if hasattr(f, "getJMenuBar") else None
            if title.startswith("Burp Suite") and bar is not None:
                menu = JMenu("Theme")
                item = JCheckBoxMenuItem("Dark mode  (Ctrl+Shift+D)")
                item.addActionListener(_Action(lambda i=item: self.ctl.set_enabled(i.isSelected())))
                menu.add(item)
                bar.add(menu)
                bar.revalidate()
                self.menu_items.append((bar, menu, item))
                return

    def _changed(self, enabled):
        self.callbacks.saveExtensionSetting(SETTING_KEY, "1" if enabled else "0")
        self.button.setText("Switch to light mode" if enabled else "Switch to dark mode")
        for bar, menu, item in self.menu_items:
            item.setSelected(enabled)

    def extensionUnloaded(self):
        def undo():
            KeyboardFocusManager.getCurrentKeyboardFocusManager().removeKeyEventDispatcher(self.hotkey)
            for bar, menu, item in self.menu_items:
                bar.remove(menu)
                bar.revalidate()
                bar.repaint()
            if self.ctl.enabled:
                self.ctl.on_change = None       # keep the saved preference for next load
                self.ctl._switch(False)
        if SwingUtilities.isEventDispatchThread():
            undo()
        else:
            SwingUtilities.invokeLater(_Run(undo))

    # ITab
    def getTabCaption(self):
        return "Dark Mode"

    def getUiComponent(self):
        return self.panel
