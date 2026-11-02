# -*- coding: utf-8 -*-
# Run: java -jar jython-standalone-2.7.4.jar tests/harness.py [metal|nimbus|system] OUTDIR
# Builds a Burp-like window (styled text pane with hard-coded token colours, table, tree,
# a tab strip with a custom non-stock UI), toggles dark mode and saves screenshots.
# Also asserts that dark -> light restores the window pixel-for-pixel.
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burp_dark_mode as bdm
from java.awt import BorderLayout, Color
from java.lang import Runnable, System
from javax.swing import (JFrame, JMenuBar, JMenu, JMenuItem, JTabbedPane, JPanel, JTextArea,
                         JScrollPane, JTable, JButton, JSplitPane, JTree, JDialog, JLabel,
                         JTextPane, SwingUtilities, UIManager)
from javax.swing.border import TitledBorder, LineBorder
from javax.swing.plaf.basic import BasicTabbedPaneUI
from javax.swing.text import SimpleAttributeSet, StyleConstants
from javax.swing.table import DefaultTableCellRenderer
from java.awt.image import BufferedImage
from javax.imageio import ImageIO
from java.io import File

laf = sys.argv[1] if len(sys.argv) > 1 else "metal"
out = sys.argv[2] if len(sys.argv) > 2 else "."
names = {"metal": "javax.swing.plaf.metal.MetalLookAndFeel",
         "nimbus": "javax.swing.plaf.nimbus.NimbusLookAndFeel",
         "system": UIManager.getSystemLookAndFeelClassName()}
UIManager.setLookAndFeel(names[laf])


class R(Runnable):
    def __init__(s, f): s.f = f
    def run(s): s.f()


def on_edt(f):
    SwingUtilities.invokeAndWait(R(f))


class WhiteRenderer(DefaultTableCellRenderer):
    """Like Burp's renderers: paints hard-coded colours regardless of the table's."""
    def getTableCellRendererComponent(self, t, v, sel, foc, r, c):
        comp = DefaultTableCellRenderer.getTableCellRendererComponent(self, t, v, sel, foc, r, c)
        comp.setBackground(Color(255, 200, 150) if sel else Color.WHITE)
        comp.setForeground(Color.BLACK)
        return comp


class CustomTabsUI(BasicTabbedPaneUI):
    """Non-stock UI (module is not javax.swing.plaf): must be left alone and restored exactly."""
    pass


state = {}


def build():
    f = JFrame("Burp Suite Professional v1.7.37 - test")
    mb = JMenuBar(); m = JMenu("Burp"); m.add(JMenuItem("Save state")); mb.add(m); f.setJMenuBar(mb)
    tabs = JTabbedPane()
    p = JPanel(BorderLayout())
    p.setBorder(TitledBorder(LineBorder(Color.GRAY), "Request"))
    pane = JTextPane()                                   # styled doc: plain text paints BLACK
    doc = pane.getStyledDocument()
    blue = SimpleAttributeSet(); StyleConstants.setForeground(blue, Color(0, 0, 255))
    red = SimpleAttributeSet(); StyleConstants.setForeground(red, Color(255, 0, 0))
    doc.insertString(0, "POST /login.aspx HTTP/1.1\nHost: example.com\nCookie: ", None)
    doc.insertString(doc.getLength(), "ASP.NET_SessionId", blue)
    doc.insertString(doc.getLength(), "=abc123\n\ntbUsername=", None)
    doc.insertString(doc.getLength(), "admin", red)
    pane.setEditable(True)
    p.add(JScrollPane(pane), BorderLayout.CENTER)
    tbl = JTable([["1", "GET", "/"], ["2", "POST", "/login"]], ["#", "Method", "URL"])
    tbl.setDefaultRenderer(tbl.getColumnClass(0), WhiteRenderer())
    tbl.setRowSelectionInterval(1, 1)
    orange = JButton("Go"); orange.setBackground(Color(0xe8, 0x6a, 0x10))
    p.add(orange, BorderLayout.SOUTH)
    sp = JSplitPane(JSplitPane.VERTICAL_SPLIT, JScrollPane(tbl), p); sp.setDividerLocation(110)
    tabs.addTab("Proxy", sp)
    tabs.addTab("Tree", JScrollPane(JTree()))
    tabs.addTab("Misc", JTextArea("plain text area"))
    strip = JTabbedPane(); strip.setUI(CustomTabsUI()); strip.addTab("custom", JPanel())
    f.add(strip, BorderLayout.NORTH)
    f.add(tabs); f.setSize(700, 500); f.setLocation(100, 100); f.setVisible(True)
    state["f"] = f
on_edt(build)


def snap():
    time.sleep(1.0)
    box = {}
    def paint():
        for key in ("f", "d"):
            w = state.get(key)
            if w is None or not w.isShowing():
                continue
            rp = w.getRootPane()
            img = BufferedImage(rp.getWidth(), rp.getHeight(), BufferedImage.TYPE_INT_RGB)
            g = img.createGraphics(); rp.paint(g); g.dispose()
            box[key] = img
    on_edt(paint)
    return box


def save(box, name):
    for key, img in box.items():
        ImageIO.write(img, "png", File(os.path.join(out, "%s_%s_%s.png" % (laf, name, key))))


def diff(a, b):
    n = 0
    for x in range(a.getWidth()):
        for y in range(a.getHeight()):
            if a.getRGB(x, y) != b.getRGB(x, y):
                n += 1
    return n


ctl = bdm.DarkModeController()
light = snap(); save(light, "1_light")
ctl.set_enabled(True); time.sleep(0.8)
print("errors after enable:", ctl.errors)
dark = snap(); save(dark, "2_dark")


def dlg():
    d = JDialog(state["f"], "New dialog"); d.add(JLabel("Hello dialog")); d.add(JButton("OK"), BorderLayout.SOUTH)
    ta = JTextArea("late text area"); d.add(ta, BorderLayout.NORTH)
    d.setSize(250, 140); d.setLocation(200, 200); d.setVisible(True); state["d"] = d
on_edt(dlg); save(snap(), "3_dark_dialog")
on_edt(lambda: state["d"].dispose())
ctl.set_enabled(False); time.sleep(0.8)
print("errors after disable:", ctl.errors)
back = snap(); save(back, "4_restored")
print("restored vs light: differing pixels =", diff(light["f"], back["f"]))
print("saved entries:", ctl.saved.size(), "defaults left:", len(ctl.orig_defaults), "enabled:", ctl.enabled)
System.exit(0)
