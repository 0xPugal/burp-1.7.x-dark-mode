# -*- coding: utf-8 -*-
# Run: java -jar jython-standalone-2.7.4.jar tests/harness.py [metal|nimbus|system] OUTDIR
# Builds a Burp-like window, toggles dark mode on/off and saves screenshots.
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import burp_dark_mode as bdm
from java.awt import BorderLayout, Color, Robot, Rectangle
from java.lang import Runnable
from javax.swing import (JFrame, JMenuBar, JMenu, JMenuItem, JTabbedPane, JPanel, JTextArea,
                         JScrollPane, JTable, JButton, JSplitPane, JTree, JDialog, JLabel,
                         JOptionPane, SwingUtilities, UIManager)
from javax.swing.border import TitledBorder, LineBorder
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

state = {}
def build():
    f = JFrame("Burp Suite Professional v1.7.37 - test")
    mb = JMenuBar(); m = JMenu("Burp"); m.add(JMenuItem("Save state")); mb.add(m); f.setJMenuBar(mb)
    tabs = JTabbedPane()
    p = JPanel(BorderLayout())
    p.setBorder(TitledBorder(LineBorder(Color.GRAY), "Request"))
    ta = JTextArea("GET / HTTP/1.1\nHost: example.com\n", 6, 40)
    p.add(JScrollPane(ta), BorderLayout.CENTER)
    tbl = JTable([["1", "GET", "/"], ["2", "POST", "/login"]], ["#", "Method", "URL"])
    tbl.setRowSelectionInterval(1, 1)
    orange = JButton("Go"); orange.setBackground(Color(0xe8, 0x6a, 0x10))
    p.add(orange, BorderLayout.SOUTH)
    tabs.addTab("Proxy", JSplitPane(JSplitPane.VERTICAL_SPLIT, JScrollPane(tbl), p))
    tabs.addTab("Tree", JScrollPane(JTree()))
    tabs.addTab("Misc", JPanel())
    f.add(tabs); f.setSize(700, 450); f.setLocation(100, 100); f.setVisible(True)
    state["f"] = f
on_edt(build)

def shot(name):
    time.sleep(1.0)
    def paint():
        from java.awt.image import BufferedImage
        for key in ("f", "d"):
            w = state.get(key)
            if w is None or not w.isShowing():
                continue
            rp = w.getRootPane()
            img = BufferedImage(rp.getWidth(), rp.getHeight(), BufferedImage.TYPE_INT_RGB)
            g = img.createGraphics(); rp.paint(g); g.dispose()
            ImageIO.write(img, "png", File(os.path.join(out, "%s_%s_%s.png" % (laf, name, key))))
    on_edt(paint)

ctl = bdm.DarkModeController()
shot("1_light")
ctl.set_enabled(True); time.sleep(0.5); shot("2_dark")
def dlg():
    JOptionPane.showMessageDialog  # dialog theming path exercised via a frame below
    d = JDialog(state["f"], "New dialog"); d.add(JLabel("Hello dialog")); d.add(JButton("OK"), BorderLayout.SOUTH)
    d.setSize(250, 120); d.setLocation(200, 200); d.setVisible(True); state["d"] = d
on_edt(dlg); shot("3_dark_dialog")
on_edt(lambda: state["d"].dispose())
ctl.set_enabled(False); time.sleep(0.5); shot("4_restored")
print("saved entries after restore:", ctl.saved.size(), "put keys:", len(ctl.put_keys), "enabled:", ctl.enabled)
System = __import__("java.lang", fromlist=["System"]).System
System.exit(0)
