# -*- coding: utf-8 -*-
# Dark mode for Burp Suite 1.7.x (Jython 2.7 extension).
#
# Toggle at runtime, no restart: Ctrl+Shift+D, the "Theme" menu in Burp's menu
# bar, or the "Dark Mode" suite tab. The choice is saved with the extension and
# re-applied on the next start. Unloading the extension restores the light UI.
#
# How it works (and why switching back is exact):
#  * Nothing global is re-initialised. Burp's own UI delegates are never replaced.
#  * Before anything changes, every component's colours/border/UI are snapshotted.
#  * The L&F colour defaults are swapped for dark ones (originals remembered) and
#    only components that use a stock Swing UI are refreshed with updateUI().
#  * Explicit colours on the rest are remapped (neutrals inverted, saturated
#    colours such as Burp's orange kept and toned down).
#  * Tables and text panes (message editors, advisories) are painted through an
#    invert + hue-rotate pixel filter, so hard-coded colours (black text in styled
#    documents, red/blue syntax tokens, custom renderers) stay readable.
#  * Switching back puts every default, colour, border and UI delegate back.

try:
    from burp import IBurpExtender, IExtensionStateListener, ITab
except ImportError:  # lets tests/ import this file outside Burp
    class IBurpExtender(object): pass
    class IExtensionStateListener(object): pass
    class ITab(object): pass

import jarray
import os
import traceback

from java.awt import (Color, Container, KeyboardFocusManager, KeyEventDispatcher, Toolkit,
                      AWTEvent, Window, BorderLayout, FlowLayout, Rectangle, Graphics, Graphics2D, Frame)
from java.awt.event import AWTEventListener, ActionListener, HierarchyEvent, KeyEvent
from java.awt.image import (BufferedImage, BandCombineOp, DataBuffer, LookupOp, Raster,
                            ShortLookupTable)
from java.lang import Runnable, Float, Class
from java.lang.reflect import Array
from java.util import ArrayList, WeakHashMap
from javax.swing import (JButton, JCheckBoxMenuItem, JColorChooser, JComponent, JLabel, JList,
                         JMenu, JPanel, JTable, JTree, SwingUtilities, Timer, UIManager)
from javax.swing.border import CompoundBorder, LineBorder, TitledBorder
from javax.swing.plaf import ColorUIResource, UIResource
from javax.swing.plaf.basic import BasicTextUI, BasicTableUI
from javax.swing.plaf.metal import DefaultMetalTheme, MetalLookAndFeel, OceanTheme
from javax.swing.plaf.nimbus import NimbusLookAndFeel
from javax.swing.text import JTextComponent, StyleConstants, StyleContext
from javax.swing import JTextPane
from javax.swing import JTextField
from javax.swing.tree import DefaultTreeCellRenderer

EXT_NAME = "Dark Mode"
SETTING_KEY = "dark_mode"

# Palette (Metal theme; also the target of the pixel filter).
BG = Color(0x2b, 0x2b, 0x2b)
BG_TEXT = Color(0x1c, 0x1c, 0x1c)
FG = Color(0xdc, 0xdc, 0xdc)
FG_DISABLED = Color(0x80, 0x80, 0x80)
SEL_BG = Color(0x36, 0x58, 0x80)
BORDER = Color(0x55, 0x55, 0x55)


# ----------------------------------------------------------------------------
# Colour mapping (used for explicit colours on components we don't pixel-filter)
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
    if s < 0.25:                                  # neutral
        if b < 0.3 or (_is_res(c) and b < 0.5):
            return c                              # already dark
        return _make(h, s, 0.11 + (1.0 - b) * 0.25, c)
    if b > 0.6:                                   # light tinted: darken, keep hue
        return _make(h, s, b * 0.55, c)
    return c


def xf_fg(c):
    h, s, b = _hsb(c)
    if s < 0.25:
        if b > 0.88 or (_is_res(c) and b > 0.5):
            return c                              # already light
        return _make(h, s, 0.9 - b * 0.4, c)
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


# Nimbus derives most colours from these keys (Burp 1.7 runs on Nimbus).
NIMBUS_DARK = {
    "control": BG, "info": BG, "menu": BG,
    "nimbusBase": Color(0x1b, 0x1d, 0x1f), "nimbusBlueGrey": Color(0x3a, 0x3d, 0x40),
    "nimbusLightBackground": BG_TEXT, "text": FG, "textForeground": FG,
    "nimbusSelectionBackground": SEL_BG, "nimbusSelection": SEL_BG,
    "textBackground": SEL_BG, "textHighlight": SEL_BG,
    "nimbusFocus": Color(0x4b, 0x6e, 0xaf), "nimbusDisabledText": FG_DISABLED,
    "nimbusBorder": BORDER, "nimbusInfoBlue": Color(0x4b, 0x6e, 0xaf),
}

_XF = {"bg": xf_bg, "fg": xf_fg, "border": xf_border, "sel": xf_sel}

# (component class, [(property, kind, defaults-key suffix)]) for colours beyond bg/fg.
_EXTRA = [
    (JTextComponent, [("CaretColor", "fg", ".caretForeground"), ("SelectionColor", "sel", ".selectionBackground"),
                      ("SelectedTextColor", "fg", ".selectionForeground"),
                      ("DisabledTextColor", "fg", ".inactiveForeground")]),
    (JTable, [("GridColor", "border", ".gridColor"), ("SelectionBackground", "sel", ".selectionBackground"),
              ("SelectionForeground", "fg", ".selectionForeground")]),
    (JList, [("SelectionBackground", "sel", ".selectionBackground"),
             ("SelectionForeground", "fg", ".selectionForeground")]),
    (DefaultTreeCellRenderer, [("BackgroundNonSelectionColor", "bg", None),
                               ("BackgroundSelectionColor", "sel", None),
                               ("TextNonSelectionColor", "fg", None), ("TextSelectionColor", "fg", None),
                               ("BorderSelectionColor", "border", None)]),
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
# Pixel filter: paint a component normally, then invert + rotate hue by 180 degrees
# (light page -> dark page, red/blue/green tokens stay recognisable). Applied to
# the paint of tables and text components, so it reaches colours that are
# hard-coded in renderers, styled documents and custom painters.
# ----------------------------------------------------------------------------
_OP = []
_OFFSET = 1024.0


def _ops():
    """(matrix op on 16-bit rasters, clamping lookup). BandCombineOp wraps instead of clamping on
    overflow, so the matrix runs on 16-bit samples with an offset and a table clamps to 0..255."""
    if not _OP:
        H = ((-0.574, 1.430, 0.144), (0.426, 0.430, 0.144), (0.426, 1.430, -0.856))
        lo, hi = float(BG_TEXT.getRed()), float(FG.getRed())      # white -> lo, black -> hi
        k = (hi - lo) / 255.0
        rows = [jarray.array([-k * H[i][0], -k * H[i][1], -k * H[i][2], hi + _OFFSET], 'f')
                for i in range(3)]
        op = BandCombineOp(jarray.array(rows, Class.forName("[F")), None)
        table = jarray.array([max(0, min(255, v - int(_OFFSET))) for v in range(4096)], 'h')
        _OP.extend([op, LookupOp(ShortLookupTable(0, table), None)])
    return _OP


def _filter_raster(src, dst):
    op, lookup = _ops()
    w, h = src.getWidth(), src.getHeight()
    wide = Raster.createInterleavedRaster(DataBuffer.TYPE_USHORT, w, h, 3, None)
    wide.setRect(src)
    wide2 = Raster.createInterleavedRaster(DataBuffer.TYPE_USHORT, w, h, 3, None)
    op.filter(wide, wide2)
    lookup.filter(wide2, dst)


def _filtered_paint(c, g, painter):
    w, h = c.getWidth(), c.getHeight()
    clip = g.getClipBounds()
    clip = Rectangle(0, 0, w, h) if clip is None else clip.intersection(Rectangle(0, 0, w, h))
    if clip.width <= 0 or clip.height <= 0:
        return
    try:
        src = BufferedImage(clip.width, clip.height, BufferedImage.TYPE_INT_RGB)
        g2 = src.createGraphics()
        try:
            if isinstance(g, Graphics2D):
                g2.setRenderingHints(g.getRenderingHints())
            g2.setColor(c.getBackground())
            g2.fillRect(0, 0, clip.width, clip.height)
            g2.translate(-clip.x, -clip.y)
            g2.setClip(clip.x, clip.y, clip.width, clip.height)
            painter(g2)
        finally:
            g2.dispose()
        dst = BufferedImage(clip.width, clip.height, BufferedImage.TYPE_INT_RGB)
        _filter_raster(src.getRaster(), dst.getRaster())
        g.drawImage(dst, clip.x, clip.y, None)
    except Exception:
        painter(g)


_FILTER_CLS = {}


def _filter_class(base):
    """Subclass of a stock text/table UI that paints through the filter (or None)."""
    if base in _FILTER_CLS:
        return _FILTER_CLS[base]
    cls = None
    try:
        if issubclass(base, BasicTextUI):
            class FilteredText(base):
                _is_dark_filter = True

                def installUI(self, c):
                    self._comp = c                    # getComponent() is protected: keep our own ref
                    self.super__installUI(c)

                def paintSafely(self, g):
                    _filtered_paint(self._comp, g, lambda g2: self.super__paintSafely(g2))
            cls = FilteredText
        elif issubclass(base, BasicTableUI):
            class FilteredTable(base):
                _is_dark_filter = True

                def paint(self, a, b):
                    if isinstance(a, Graphics):       # Synth also has paint(SynthContext, Graphics)
                        _filtered_paint(b, a, lambda g2: self.super__paint(g2, b))
                    else:
                        self.super__paint(a, b)
            cls = FilteredTable
    except Exception:
        cls = None
    _FILTER_CLS[base] = cls
    return cls


# ----------------------------------------------------------------------------
# Dark Metal theme
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
# Component helpers
# ----------------------------------------------------------------------------
def _stock_ui(c):
    """True if c uses a stock Swing UI delegate (safe to updateUI()/replace)."""
    getui = getattr(c, "getUI", None)
    if getui is None or not isinstance(c, JComponent):
        return False
    try:
        ui = getui()
    except Exception:
        return False
    if ui is None:
        return False
    m = ui.__class__.__module__ or ""
    return m.startswith("javax.swing.plaf") or m.startswith("com.sun.java.swing.plaf")


def _filter_candidate(c):
    if isinstance(c, JTable):
        return True
    return isinstance(c, JTextComponent) and not isinstance(c, JTextField)


def _children(c):
    kids = []
    if isinstance(c, JMenu):
        kids.append(c.getPopupMenu())
    if isinstance(c, JTree):
        r = c.getCellRenderer()
        if isinstance(r, DefaultTreeCellRenderer):
            kids.append(r)
    if isinstance(c, Container):
        kids.extend(c.getComponents())
    return kids


def _collect(root, out):
    if isinstance(root, JColorChooser):
        return
    out.append(root)
    for k in _children(root):
        _collect(k, out)


# ----------------------------------------------------------------------------
# Controller
# ----------------------------------------------------------------------------
_ABSENT = object()


class DarkModeController(object):
    def __init__(self, on_change=None):
        self.enabled = False
        self.on_change = on_change
        self.saved = WeakHashMap()        # component -> snapshot (weak keys: no leaks)
        self.orig_defaults = {}           # L&F defaults key -> original value
        self.old_theme = None
        self.nimbus_old = None            # user-table values we replaced (Nimbus)
        self.light_colors = {}            # light defaults captured before switching
        self.listener = None
        self.errors = []

    # -- public --------------------------------------------------------------
    def set_enabled(self, on):
        if SwingUtilities.isEventDispatchThread():
            self._switch(on)
        else:
            SwingUtilities.invokeLater(_Run(lambda: self._switch(on)))

    def toggle(self):
        self.set_enabled(not self.enabled)

    def _err(self, where):
        if len(self.errors) < 30:
            self.errors.append("%s: %s" % (where, traceback.format_exc()))

    # -- switching -----------------------------------------------------------
    def _switch(self, on):
        if on == self.enabled:
            return
        try:
            if on:
                self._enable()
            else:
                self._disable()
        except Exception:
            self._err("switch")
            raise
        finally:
            for w in Window.getWindows():
                w.invalidate()
                w.validate()
                w.repaint()
        if self.on_change:
            self.on_change(self.enabled)

    def _enable(self):
        comps = []
        for w in Window.getWindows():
            _collect(w, comps)
        for c in comps:                                 # 1. snapshot everything first
            self._snap(c)
        self._capture_light()
        self._defaults_dark()                           # 2. dark colour defaults
        for c in comps:                                 # 3. refresh stock UIs / pixel filter
            self._stage(c, late=False)
        for c in comps:                                 # 4. remap remaining explicit colours
            self._apply(c)
        self.enabled = True
        self.listener = _HierarchyListener(self)
        Toolkit.getDefaultToolkit().addAWTEventListener(self.listener, AWTEvent.HIERARCHY_EVENT_MASK)

    def _disable(self):
        if self.listener is not None:
            Toolkit.getDefaultToolkit().removeAWTEventListener(self.listener)
            self.listener = None
        self.enabled = False
        self._defaults_restore()
        comps = ArrayList(self.saved.keySet())
        for c in comps:
            st = self.saved.get(c)
            if st is not None:
                self._restore_props(c, st)
        for c in comps:
            st = self.saved.get(c)
            if st is None:
                continue
            try:
                if st.get("filtered"):
                    c.updateUI()                        # fresh stock UI, colours from the light defaults
                elif st.get("ui") is not None and not st.get("renderer") and _stock_ui(c):
                    c.updateUI()
            except Exception:
                self._err("restore-ui " + c.__class__.__name__)
        for c in comps:
            st = self.saved.get(c)
            if st is not None and "style_fg" in st:
                ds, fg, had = st["style_fg"]
                try:
                    if had:
                        StyleConstants.setForeground(ds, fg)
                    else:
                        ds.removeAttribute(StyleConstants.Foreground)
                except Exception:
                    self._err("style-fg")
        self.saved.clear()
        leaked = []
        for w in Window.getWindows():                   # safety net: never leave a filter UI behind
            _collect(w, leaked)
        for c in leaked:
            try:
                if getattr(c.getUI(), "_is_dark_filter", False):
                    c.updateUI()
            except Exception:
                pass

    # -- L&F defaults --------------------------------------------------------
    def _defaults_dark(self):
        if UIManager.getLookAndFeel().getName() == "Nimbus":
            self.nimbus_old = {}
            for k, v in NIMBUS_DARK.items():
                self.nimbus_old[k] = UIManager.put(k, ColorUIResource(v))   # previous user-table value
            UIManager.setLookAndFeel(NimbusLookAndFeel())                   # re-derive from the new keys
            return
        d = UIManager.getLookAndFeelDefaults()
        new = {}
        if UIManager.getLookAndFeel().getName() == "Metal":
            self.old_theme = MetalLookAndFeel.getCurrentTheme()
            MetalLookAndFeel.setCurrentTheme(DarkMetalTheme())
            try:
                dd = MetalLookAndFeel().getDefaults()
                for k in list(dd.keySet()):
                    v = dd.get(k)
                    if isinstance(v, Color):
                        new[k] = v
            except Exception:
                self._err("metal-defaults")
        drop = []
        for k in list(d.keySet()):                      # keys the dark Metal table doesn't cover
            if k in new:
                continue
            v = d.get(k)
            if isinstance(v, Color):
                n = str(k).lower()
                if n.endswith("selectionbackground"):
                    kind = "sel"
                elif n.endswith("foreground") or n.endswith("text") or n.endswith("textforeground"):
                    kind = "fg"
                else:
                    kind = "bg"
                new[k] = ColorUIResource(_XF[kind](v))
            elif str(k).endswith(".gradient"):          # Ocean gradients are light: paint flat instead
                drop.append(k)
        for k, v in new.items():
            cur = d.get(k)
            self.orig_defaults[k] = _ABSENT if cur is None else cur
            d.put(k, v)
        for k in drop:
            self.orig_defaults[k] = d.get(k)
            d.put(k, None)

    def _defaults_restore(self):
        if self.nimbus_old is not None:
            for k, old in self.nimbus_old.items():
                UIManager.put(k, old)                   # None removes our key again
            self.nimbus_old = None
            UIManager.setLookAndFeel(NimbusLookAndFeel())
            UIManager.setLookAndFeel(NimbusLookAndFeel())   # second pass: derived colours settle on the light parents
            return
        d = UIManager.getLookAndFeelDefaults()
        for k, v in self.orig_defaults.items():
            d.put(k, None if v is _ABSENT else v)
        self.orig_defaults = {}
        if self.old_theme is not None:
            MetalLookAndFeel.setCurrentTheme(self.old_theme)
            self.old_theme = None
        elif UIManager.getLookAndFeel().getName() == "Metal":
            MetalLookAndFeel.setCurrentTheme(OceanTheme())

    def _capture_light(self):
        self.light_colors = {}
        for prefix in ("TextArea", "TextPane", "EditorPane", "Table"):
            for suffix in (".background", ".foreground", ".selectionBackground", ".selectionForeground",
                           ".caretForeground", ".inactiveForeground", ".gridColor"):
                c = UIManager.getColor(prefix + suffix)
                if c is not None:
                    self.light_colors[prefix + suffix] = self._plain(c)

    def _light_default(self, key):
        return self.light_colors.get(key)

    # -- snapshot / restore --------------------------------------------------
    def _snap(self, c):
        if self.saved.containsKey(c):
            return self.saved.get(c)
        st = {"extra": [], "tb": [], "frozen": {}}   # frozen: plain RGB copies (DerivedColor mutates)
        self.saved.put(c, st)
        try:
            if isinstance(c, DefaultTreeCellRenderer):
                st["renderer"] = True                   # its bg/fg are set per cell
            else:
                bg, fg = c.getBackground(), c.getForeground()
                if bg is not None:
                    st["bg"] = (c.isBackgroundSet(), bg)
                    st["frozen"]["bg"] = self._plain(bg)
                if fg is not None:
                    st["fg"] = (c.isForegroundSet(), fg)
                    st["frozen"]["fg"] = self._plain(fg)
            for cls, props in _EXTRA:
                if isinstance(c, cls):
                    for prop, kind, suffix in props:
                        old = getattr(c, "get" + prop)()
                        if old is not None:
                            st["extra"].append(("set" + prop, old, prop, suffix))
                            st["frozen"][prop] = self._plain(old)
            if isinstance(c, JTextPane):
                # BasicTextPaneUI copies the component foreground into the document's default style
                ds = c.getStyledDocument().getStyle(StyleContext.DEFAULT_STYLE)
                if ds is not None:
                    cur = ds.getAttribute(StyleConstants.Foreground)
                    st["style_fg"] = (ds, None if cur is None else self._plain(cur), cur is not None)
            if isinstance(c, JComponent):
                st["ui"] = c.getUI() if hasattr(c, "getUI") else None
                b = c.getBorder()
                st["border"] = b
                self._snap_titles(b, st)
        except Exception:
            self._err("snap " + c.__class__.__name__)
        return st

    def _snap_titles(self, b, st):
        if isinstance(b, TitledBorder):
            tc = b.getTitleColor()
            st["tb"].append((b, tc, b.getBorder(), None if tc is None else self._plain(tc)))
            self._snap_titles(b.getBorder(), st)
        elif isinstance(b, CompoundBorder):
            self._snap_titles(b.getOutsideBorder(), st)
            self._snap_titles(b.getInsideBorder(), st)

    @staticmethod
    def _orig_color(orig, frozen):
        """Theme colours (UIResource/DerivedColor) are mutated in place by the L&F, so restore their
        frozen light RGB as a UIResource (updateUI() then re-installs the real one)."""
        if isinstance(orig, UIResource) and frozen is not None:
            return ColorUIResource(frozen)
        return orig

    def _restore_props(self, c, st):
        try:
            fz = st["frozen"]
            if "bg" in st:
                c.setBackground(self._orig_color(st["bg"][1], fz.get("bg")) if st["bg"][0] else None)
            if "fg" in st:
                c.setForeground(self._orig_color(st["fg"][1], fz.get("fg")) if st["fg"][0] else None)
            for setter, old, prop, suffix in st["extra"]:
                getattr(c, setter)(self._orig_color(old, fz.get(prop)))
            for b, tc, inner, frozen in st["tb"]:
                b.setTitleColor(None if tc is None else (self._plain(frozen) if frozen is not None else tc))
                b.setBorder(inner)
            if st.get("border_changed"):
                c.setBorder(st["border"])
        except Exception:
            self._err("restore " + c.__class__.__name__)

    # -- staging: stock UI refresh or pixel filter ----------------------------
    def _stage(self, c, late):
        st = self.saved.get(c)
        if st is None:
            return
        try:
            if _filter_candidate(c) and _stock_ui(c):
                cls = _filter_class(c.getUI().__class__)
                if cls is not None:
                    c.setUI(cls())
                    st["filtered"] = True
                    self._relight(c, st, late)
                    return
            if not late and not st.get("renderer") and _stock_ui(c):
                c.updateUI()
        except Exception:
            self._err("stage " + c.__class__.__name__)

    @staticmethod
    def _plain(col):
        """Explicit (non-UIResource) copy: Synth/Nimbus never overwrites those with style colours."""
        return Color(col.getRed(), col.getGreen(), col.getBlue(), col.getAlpha())

    def _relight(self, c, st, late):
        """A filtered component must paint with its original *light* colours (the filter darkens
        them), and they must be explicit so the dark L&F style can't reset them."""
        prefix = c.getUIClassID()[:-2]
        for key, getter, setter, suffix in (("bg", "getBackground", "setBackground", ".background"),
                                            ("fg", "getForeground", "setForeground", ".foreground")):
            if not late and key in st["frozen"]:
                light = st["frozen"][key]
            else:
                light = self._light_default(prefix + suffix) or getattr(c, getter)()
            if light is not None:
                getattr(c, setter)(self._plain(light))
        for setter, old, prop, suffix in st["extra"]:
            light = st["frozen"].get(prop) if not late else (self._light_default(prefix + suffix) if suffix else None) or old
            if light is not None:
                getattr(c, setter)(self._plain(light))

    # -- colour remapping ----------------------------------------------------
    def _apply(self, c):
        st = self.saved.get(c)
        if st is None or st.get("filtered"):
            return
        try:
            if "bg" in st:
                bg = c.getBackground()
                if bg is not None:
                    nb = xf_bg(bg)
                    if nb is not bg:
                        c.setBackground(nb)
            if "fg" in st:
                fg = c.getForeground()
                if fg is not None:
                    nf = xf_fg(fg)
                    if nf is not fg:
                        c.setForeground(nf)
            for setter, old, prop, kind_suffix in st["extra"]:
                kind = [k for p, k, s in self._props_for(c) if p == prop]
                cur = getattr(c, "get" + prop)()
                if kind and cur is not None:
                    new = _XF[kind[0]](cur)
                    if new is not cur:
                        getattr(c, setter)(new)
            if isinstance(c, JComponent) and "border" in st:
                nb2 = self._border(c.getBorder(), st)
                if nb2 is not c.getBorder():
                    c.setBorder(nb2)
                    st["border_changed"] = True
        except Exception:
            self._err("apply " + c.__class__.__name__)

    @staticmethod
    def _props_for(c):
        out = []
        for cls, props in _EXTRA:
            if isinstance(c, cls):
                out.extend(props)
        return out

    def _border(self, b, st):
        if isinstance(b, TitledBorder):
            tc = b.getTitleColor()
            if tc is not None:
                b.setTitleColor(xf_fg(tc))
            ni = self._border(b.getBorder(), st)
            if ni is not b.getBorder():
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

    # -- late components (dialogs, popups, new tabs) -------------------------
    def theme_late(self, c):
        if self.saved.containsKey(c) or isinstance(c, JColorChooser):
            return
        for k in [c] + _children(c) if isinstance(c, JMenu) else [c]:
            self._snap(k)
            self._stage(k, late=True)
            self._apply(k)

    # -- diagnostics ---------------------------------------------------------
    def dump(self, path):
        lines = []

        def rec(c, depth):
            if len(lines) > 8000:
                return
            try:
                ui = c.getUI().__class__.__name__ if hasattr(c, "getUI") and c.getUI() is not None else "-"
            except Exception:
                ui = "?"
            st = self.saved.get(c)
            lines.append("%s%s ui=%s bg=%s fg=%s opaque=%s %s" % (
                "  " * depth, c.__class__.__module__ + "." + c.__class__.__name__, ui,
                c.getBackground(), c.getForeground(), getattr(c, "isOpaque", lambda: "-")(),
                "[filtered]" if st and st.get("filtered") else ""))
            if isinstance(c, Container):
                for k in c.getComponents():
                    rec(k, depth + 1)
        for w in Window.getWindows():
            if w.isShowing():
                rec(w, 0)
        lines.append("--- errors ---")
        lines.extend(self.errors)
        f = open(path, "w")
        try:
            f.write("\n".join(unicode(x).encode("utf-8") for x in lines))
        finally:
            f.close()
        return len(lines)


class _HierarchyListener(AWTEventListener):
    """Themes components that appear after dark mode was switched on (dialogs, popups, new tabs)."""

    def __init__(self, ctl):
        self.ctl = ctl

    def eventDispatched(self, e):
        try:
            if not self.ctl.enabled or not isinstance(e, HierarchyEvent):
                return
            if e.getChangeFlags() & (HierarchyEvent.PARENT_CHANGED | HierarchyEvent.SHOWING_CHANGED
                                     | HierarchyEvent.DISPLAYABILITY_CHANGED):
                c = e.getComponent()          # the receiver: every descendant gets its own event
                if not self.ctl.saved.containsKey(c):
                    self.ctl.theme_late(c)
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
        dump_btn = JButton("Dump UI tree (diagnostics)", actionPerformed=lambda e: self._dump())
        self.status = JLabel(" ")
        self.panel = JPanel(BorderLayout())
        top = JPanel(FlowLayout(FlowLayout.LEFT))
        top.add(self.button)
        top.add(dump_btn)
        top.add(JLabel("Shortcut: Ctrl+Shift+D"))
        top.add(self.status)
        self.panel.add(top, BorderLayout.NORTH)
        callbacks.addSuiteTab(self)

        SwingUtilities.invokeLater(_Run(self._install_menu))
        if callbacks.loadExtensionSetting(SETTING_KEY) == "1":
            # Delay a little so Burp's main frame is fully built before we restyle it.
            t = Timer(800, _Action(lambda: self.ctl.set_enabled(True)))
            t.setRepeats(False)
            t.start()

    def _dump(self):
        path = os.path.join(os.path.expanduser("~"), "burp_dark_mode_dump.txt")
        n = self.ctl.dump(path)
        self.status.setText("wrote %d lines to %s" % (n, path))

    def _install_menu(self):
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
            if self.ctl.enabled:
                self.ctl.on_change = None       # keep the saved preference for next load
                self.ctl._switch(False)
            for bar, menu, item in self.menu_items:
                bar.remove(menu)
                bar.revalidate()
                bar.repaint()
        if SwingUtilities.isEventDispatchThread():
            undo()
        else:
            SwingUtilities.invokeLater(_Run(undo))

    # ITab
    def getTabCaption(self):
        return "Dark Mode"

    def getUiComponent(self):
        return self.panel
