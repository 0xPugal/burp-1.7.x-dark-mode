# -*- coding: utf-8 -*-
"""
Offline harness for burp_dark_mode.py (Jython 2.7, Java 8).

It creates a Burp-like Swing window (Nimbus L&F, hard-coded cell renderers and
a styled document), applies the dark theme, opens a new dialog, switches back
to light and verifies that the window is restored pixel-for-pixel.

Run (from the repository root):

    java -cp jython-standalone-2.7.4.jar org.python.util.jython tests/harness.py tests/out

Exit code 0 = all checks passed.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import burp_dark_mode as bdm

from java.awt import BorderLayout, Color, Dimension
from java.awt.image import BufferedImage
from java.io import File
from java.lang import Object, Runnable, System
from javax.imageio import ImageIO
from javax.swing import (JButton, JDialog, JFrame, JLabel, JPanel, JScrollPane, JTabbedPane,
                         JTable, JTextArea, JTextPane, JTree, JSplitPane, SwingUtilities,
                         UIManager)
from javax.swing.table import DefaultTableCellRenderer
from javax.swing.text import SimpleAttributeSet, StyleConstants

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")


class _Run(Runnable):
    def __init__(self, fn):
        self.fn = fn

    def run(self):
        self.fn()


def on_edt(fn):
    SwingUtilities.invokeAndWait(_Run(fn))


def wait(seconds=0.8):
    time.sleep(seconds)


class WhiteRenderer(DefaultTableCellRenderer):
    """Simulates Burp's renderers, which paint fixed colours."""

    def getTableCellRendererComponent(self, table, value, selected, focus, row, column):
        component = DefaultTableCellRenderer.getTableCellRendererComponent(
            self, table, value, selected, focus, row, column)
        component.setBackground(Color(255, 200, 150) if selected else Color(255, 255, 255))
        component.setForeground(Color(0, 0, 0))
        return component


state = {}


def build_window():
    frame = JFrame("Burp Suite Professional v1.7.37 - harness")
    tabs = JTabbedPane()

    panel = JPanel(BorderLayout())
    table = JTable([["1", "GET", "/"], ["2", "POST", "/login"]], ["#", "Method", "URL"])
    table.setDefaultRenderer(Object, WhiteRenderer())
    table.setRowSelectionInterval(1, 1)

    pane = JTextPane()
    document = pane.getStyledDocument()
    blue = SimpleAttributeSet()
    StyleConstants.setForeground(blue, Color(0, 0, 192))
    red = SimpleAttributeSet()
    StyleConstants.setForeground(red, Color(160, 16, 16))
    document.insertString(0, "POST /login HTTP/1.1\nHost: example.com\nCookie: ", None)
    document.insertString(document.getLength(), "session=abc", blue)
    document.insertString(document.getLength(), "\n\nuser=", None)
    document.insertString(document.getLength(), "admin", red)

    split = JSplitPane(JSplitPane.VERTICAL_SPLIT, JScrollPane(table), JScrollPane(pane))
    split.setDividerLocation(120)
    panel.add(split, BorderLayout.CENTER)
    panel.add(JButton("Send"), BorderLayout.SOUTH)

    tabs.addTab("Proxy", panel)
    tabs.addTab("Target", JScrollPane(JTree()))
    tabs.addTab("Misc", JTextArea("plain text area"))
    frame.add(tabs)
    frame.setSize(720, 500)
    frame.setLocation(80, 80)
    frame.setVisible(True)
    state["frame"] = frame


def find_tabs(component):
    if isinstance(component, JTabbedPane):
        return component
    if hasattr(component, "getComponents"):
        for child in component.getComponents():
            found = find_tabs(child)
            if found is not None:
                return found
    return None


def focus_window():
    frame = state["frame"]
    frame.toFront()
    frame.requestFocus()
    tabs = find_tabs(frame)
    if tabs is not None:
        tabs.requestFocusInWindow()


def capture(label):
    frame = state["frame"]
    root = frame.getRootPane()
    image = BufferedImage(root.getWidth(), root.getHeight(), BufferedImage.TYPE_INT_RGB)
    graphics = image.createGraphics()
    root.paint(graphics)
    graphics.dispose()
    ImageIO.write(image, "png", File(os.path.join(OUT, "%s.png" % label)))
    return image


def open_dialog():
    dialog = JDialog(state["frame"], "Harness dialog", False)
    dialog.setLayout(BorderLayout(0, 6))
    table = JTable([["a", "b"]], ["One", "Two"])
    scroll = JScrollPane(table)
    scroll.setPreferredSize(Dimension(320, 90))
    dialog.add(scroll, BorderLayout.NORTH)
    dialog.add(JScrollPane(JTextArea("Dialog opened while dark.")), BorderLayout.CENTER)
    dialog.add(JLabel("  bottom"), BorderLayout.SOUTH)
    dialog.setSize(340, 220)
    dialog.setLocationRelativeTo(state["frame"])
    dialog.setVisible(True)
    state["dialog"] = dialog


def capture_dialog(label):
    dialog = state["dialog"]
    root = dialog.getRootPane()
    image = BufferedImage(root.getWidth(), root.getHeight(), BufferedImage.TYPE_INT_RGB)
    graphics = image.createGraphics()
    root.paint(graphics)
    graphics.dispose()
    ImageIO.write(image, "png", File(os.path.join(OUT, "%s.png" % label)))


def diff(a, b):
    if a.getWidth() != b.getWidth() or a.getHeight() != b.getHeight():
        return -1
    count = 0
    for x in range(a.getWidth()):
        for y in range(a.getHeight()):
            if a.getRGB(x, y) != b.getRGB(x, y):
                count += 1
    return count


def main():
    if not os.path.isdir(OUT):
        os.makedirs(OUT)

    UIManager.setLookAndFeel("javax.swing.plaf.nimbus.NimbusLookAndFeel")
    on_edt(build_window)
    wait(1.0)

    engine = bdm.ThemeEngine()
    on_edt(focus_window)
    wait(0.4)
    light = capture("1_light")
    on_edt(lambda: engine.set_enabled(True))
    wait(1.2)
    dark = capture("2_dark")
    if engine.errors:
        print("errors while enabling: %s" % engine.errors)

    on_edt(open_dialog)
    wait(2.0)
    capture_dialog("3_dark_dialog")
    on_edt(lambda: state["dialog"].dispose())
    wait(0.5)

    on_edt(lambda: engine.set_enabled(False))
    wait(1.2)
    on_edt(focus_window)
    wait(0.4)
    restored = capture("4_restored")
    if engine.errors:
        print("errors while disabling: %s" % engine.errors)

    def info():
        table = None
        for component in state["frame"].getContentPane().getComponents():
            pass
        def walk(c):
            if isinstance(c, JTable):
                return c
            if hasattr(c, "getComponents"):
                for child in c.getComponents():
                    found = walk(child)
                    if found is not None:
                        return found
            return None
        table = walk(state["frame"])
        if table is not None:
            print("table bg=%s sel=%s grid=%s op=%s renderer=%s" % (
                table.getBackground(), table.getSelectionBackground(), table.getGridColor(),
                table.isOpaque(), table.getDefaultRenderer(Object).__class__.__name__))
        pane = walk_text(state["frame"])
        if pane is not None:
            print("text bg=%s fg=%s caret=%s" % (pane.getBackground(), pane.getForeground(),
                                                 pane.getCaretColor()))
        print("UIManager control=%s TextPane.background=%s Table.background=%s" % (
            UIManager.get("control"), UIManager.get("TextPane.background"),
            UIManager.get("Table.background")))
    def walk_text(c):
        if isinstance(c, JTextPane):
            return c
        if hasattr(c, "getComponents"):
            for child in c.getComponents():
                found = walk_text(child)
                if found is not None:
                    return found
        return None
    on_edt(info)

    differing = diff(light, restored)
    print("restored vs original: %d differing pixels" % differing)
    print("active after revert: %s" % engine.active)

    success = differing == 0 and not engine.active
    print("RESULT: %s" % ("PASS" if success else "FAIL"))
    System.exit(0 if success else 1)


main()



