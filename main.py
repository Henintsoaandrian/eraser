#!/usr/bin/env python3

import sys
import os
import threading
import time
import json
import ssl
import certifi
import urllib.request
import urllib.parse
from pathlib import Path

# ===== Gestion libusb-package (SANS écraser usb.core.find) =====
try:
    import libusb_package
    HAS_LIBUSB = True
    print("✅ libusb-package chargé")
except ImportError:
    HAS_LIBUSB = False
    print("ℹ️ libusb-package non disponible, utilisation de pyusb standard")

import usb.core
import usb.util

from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtCore import QByteArray, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QPainter, QPixmap, QPainterPath
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget, QDialog,
)
from pymobiledevice3.irecv import IRecv

def resource_path(name):
    base = getattr(sys, '_MEIPASS', os.path.abspath('.'))
    return os.path.join(base, name)

def load_retina_pixmap(path, display_size):
    screen = QApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen else 1
    target_size = int(display_size * ratio)
    pix = QPixmap(path)
    if not pix.isNull():
        pix = pix.scaled(target_size, target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pix.setDevicePixelRatio(ratio)
    return pix

API_URL = "https://api.mobidocserver.com/eraser_passcode/validate_ecid.php"
TELEGRAM_REPORT_URL = "https://api.mobidocserver.com/eraser_passcode/telegramreport_ecid.php"

APPLE_VENDOR_ID = 0x05AC
DFU_PRODUCT_ID = 0x1227
RECOVERY_PRODUCT_ID = 0x1281
DFU_DNLOAD = 1
DFU_ABORT = 4
CUSTOM_BOOT = 8
DFU_REQUEST_TYPE = 0x21
DFU_TRANSFER_SIZE = 0x800

SUPPORTED_CPIDS = {"0x8020", "0x8030"}

DEVICES = {
    ("0x8020", 0x0A): ("iPhone XS Max", "d331"),
    ("0x8020", 0x0C): ("iPhone XR", "n841"),
    ("0x8020", 0x0E): ("iPhone XS", "d321"),
    ("0x8020", 0x1A): ("iPhone XS Max", "d331p"),
    ("0x8020", 0x14): ("iPad mini 5", "j210"),
    ("0x8020", 0x16): ("iPad mini 5", "j210"),
    ("0x8020", 0x1C): ("iPad Air 3", "j217"),
    ("0x8020", 0x1E): ("iPad Air 3", "j217"),
    ("0x8020", 0x24): ("iPad (8th gen)", "ipad11b"),
    ("0x8020", 0x26): ("iPad (8th gen)", "ipad11b"),
    ("0x8030", 0x02): ("iPhone 11 Pro Max", "d431"),
    ("0x8030", 0x04): ("iPhone 11", "n104"),
    ("0x8030", 0x06): ("iPhone 11 Pro", "d421"),
    ("0x8030", 0x10): ("iPhone SE (2nd gen)", "d79")
}

IBEC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "boot"

BG_PAGE      = "#141F2E"
BG_CARD      = "#16212F"
BG_SUBCARD   = "#111B27"
BORDER       = "#26445A"
TEXT_PRIMARY = "#E5F6FB"
TEXT_SECOND  = "#9BC2D4"
TEXT_MUTED   = "#5F84A3"
ACCENT       = "#22D3EE"
ACCENT_HOVER = "#67E8F9"
GREEN        = "#5DCAA5"
GREEN_BG     = "#173c33"
RED          = "#F09595"
RED_BG       = "#3c1a1a"
MONO_FONT    = "Menlo, Consolas, monospace"

STEPS = [
    "Uploading",
    "Booting iBEC",
    "Waiting for recovery mode",
    "Sending obliteration commands",
]

_ICON_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)

SVG_ICONS = {
    "phone": _ICON_TEMPLATE.format(color="{color}", body=(
        '<rect x="7" y="3" width="10" height="18" rx="2"/>'
        '<line x1="10.5" y1="17.5" x2="13.5" y2="17.5"/>'
    )),
    "lock-closed": _ICON_TEMPLATE.format(color="{color}", body=(
        '<rect x="5.5" y="11" width="13" height="9" rx="2"/>'
        '<path d="M8.5 11V7.5a3.5 3.5 0 0 1 7 0V11"/>'
        '<circle cx="12" cy="15.3" r="1"/>'
    )),
    "lock-open": _ICON_TEMPLATE.format(color="{color}", body=(
        '<rect x="5.5" y="11" width="13" height="9" rx="2"/>'
        '<path d="M8.5 11V7.5a3.5 3.5 0 0 1 6.6-1.6"/>'
        '<circle cx="12" cy="15.3" r="1"/>'
    )),
    "send": _ICON_TEMPLATE.format(color="{color}", body=(
        '<line x1="21" y1="3" x2="10.5" y2="13.5"/>'
        '<polygon points="21 3 14.5 21 10.5 13.5 3 9.5 21 3"/>'
    )),
}

TELEGRAM_HANDLE = "mobidocserver_news"
TELEGRAM_URL = f"https://t.me/{TELEGRAM_HANDLE}"

def render_icon(name, color, size=20):
    svg = SVG_ICONS[name].format(color=color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap

# ======================================================================
# DFU utilities
# ======================================================================
def _dfu_serial(dispose=False):
    try:
        if HAS_LIBUSB:
            dev = libusb_package.find(
                idVendor=APPLE_VENDOR_ID,
                idProduct=DFU_PRODUCT_ID
            )
        else:
            dev = usb.core.find(
                idVendor=APPLE_VENDOR_ID,
                idProduct=DFU_PRODUCT_ID
            )

        if dev is None:
            return None, None

    except Exception as e:
        print(f"Erreur de détection DFU: {e}")
        return None, None

    try:
        serial = dev.serial_number or ""
        if isinstance(serial, bytes):
            serial = serial.decode("utf-8", errors="ignore")
    except Exception:
        serial = ""

    # DEBUG : affiche la valeur brute du serial
    print(f"🔍 SERIAL RAW = {repr(serial)}")

    if dispose:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass

    return dev, serial

def _recovery_find():
    """Recherche le périphérique en mode recovery."""
    try:
        if HAS_LIBUSB:
            return libusb_package.find(
                idVendor=APPLE_VENDOR_ID,
                idProduct=RECOVERY_PRODUCT_ID
            )
        else:
            return usb.core.find(
                idVendor=APPLE_VENDOR_ID,
                idProduct=RECOVERY_PRODUCT_ID
            )
    except Exception:
        return None

def _serial_field(serial, key):
    if not serial:
        return None
    for part in serial.split():
        if part.startswith(f"{key}:"):
            return part.split(":", 1)[1]
    return None

def identify(cpid_key, bdid_raw):
    try:
        return DEVICES.get((cpid_key, int(bdid_raw, 16)))
    except (ValueError, TypeError):
        return None

def ibec_path_for(codename):
    return IBEC_DIR / f"iBEC.{codename}.RELEASE.patched"

def dfu_upload(dev, buf):
    for off in range(0, len(buf), DFU_TRANSFER_SIZE):
        dev.ctrl_transfer(DFU_REQUEST_TYPE, DFU_DNLOAD, 0, 0,
                          buf[off:off + DFU_TRANSFER_SIZE], 2000)
    dev.ctrl_transfer(DFU_REQUEST_TYPE, DFU_DNLOAD, 0, 0, None, 100)

def dfu_boot(dev):
    dev.ctrl_transfer(DFU_REQUEST_TYPE, CUSTOM_BOOT, 0, 0, None, 100)
    try:
        dev.ctrl_transfer(DFU_REQUEST_TYPE, DFU_ABORT, 0, 0, None, 100)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)

# ===== API & Telegram =====
def check_ecid_online(ecid):
    if not ecid:
        return False
    try:
        url = f"{API_URL}?ecid={urllib.parse.quote(ecid)}"
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(url, timeout=5, context=context) as response:
            data = json.loads(response.read().decode())
            return data.get("valid", False)
    except Exception:
        return False

def get_location():
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.urlopen("http://ip-api.com/json/", timeout=3, context=context)
        data = json.loads(req.read().decode())
        return f"{data.get('city', '')}, {data.get('country', '')}"
    except Exception:
        return "Unknown"

def send_telegram_report(status, ecid, model):
    try:
        location = get_location()
        data = {
            "status": status,
            "ecid": ecid,
            "model": model,
            "os": sys.platform,
            "location": location,
        }
        post_data = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(TELEGRAM_REPORT_URL, data=post_data, method="POST")
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=5, context=context) as response:
            return response.read().decode() == "ok"
    except Exception:
        return False

# ===== Dialogues (Success, Register) =====
class SuccessDialog(QDialog):
    def __init__(self, parent=None, device_model=None):
        super().__init__(parent)
        self.setWindowTitle("Mobi Doc Eraser Passcode V1.0")
        self.setFixedSize(400, 160)
        self.setStyleSheet("""
            QDialog {
                background-color: #16212F;
                border-radius: 12px;
                border: 1px solid #26445A;
            }
            QLabel {
                color: #E5F6FB;
                border: none;
                background: transparent;
            }
            QPushButton {
                background-color: #22D3EE;
                color: #04141A;
                border: none;
                border-radius: 5px;
                padding: 8px 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #67E8F9;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(72, 72)
        icon_lbl.setStyleSheet('border: none; background: transparent;')
        logo_path = resource_path('logo.png')
        if os.path.exists(logo_path):
            pix = load_retina_pixmap(logo_path, 72)
            rounded = QPixmap(72 * (pix.devicePixelRatio() if pix.devicePixelRatio() else 1),
                               72 * (pix.devicePixelRatio() if pix.devicePixelRatio() else 1))
            rounded.fill(Qt.transparent)
            p = QPainter(rounded)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, rounded.width(), rounded.height(), 14, 14)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            rounded.setDevicePixelRatio(pix.devicePixelRatio() if pix.devicePixelRatio() else 1)
            icon_lbl.setPixmap(rounded)
        else:
            icon_lbl.setText("🔒")
            icon_lbl.setStyleSheet("font-size: 40px;")
        layout.addWidget(icon_lbl)

        right = QVBoxLayout()
        right.setSpacing(6)
        title = QLabel('✅ Device Erased successfully! 🎉')
        title.setStyleSheet('font-size: 16px; font-weight: bold; color: #22D3EE;')
        title.setWordWrap(True)
        model = device_model or ''
        msg = QLabel(f'Your {model} has been erased.\nIt will reboot soon.')
        msg.setStyleSheet('font-size: 12px; color: #9BC2D4;')
        msg.setWordWrap(True)

        btn_ok = QPushButton('OK')
        btn_ok.setFixedWidth(80)
        btn_ok.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)

        right.addWidget(title)
        right.addWidget(msg)
        right.addLayout(btn_row)
        layout.addLayout(right)

class RegisterDialog(QDialog):
    def __init__(self, parent=None, ecid=None):
        super().__init__(parent)
        self.setWindowTitle("ECID not registered")
        self.setFixedSize(400, 160)
        self.setStyleSheet("""
            QDialog {
                background-color: #16212F;
                border-radius: 12px;
                border: 1px solid #26445A;
            }
            QLabel {
                color: #E5F6FB;
                border: none;
                background: transparent;
            }
            QPushButton {
                background-color: #22D3EE;
                color: #04141A;
                border: none;
                border-radius: 5px;
                padding: 8px 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #67E8F9;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 25)
        layout.setSpacing(10)

        title = QLabel('🔒 ECID not registered!')
        title.setStyleSheet('font-size: 15px; font-weight: bold; color: #F09595;')
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        ecid_label = QLabel(f'ECID: <b>{ecid}</b>')
        ecid_label.setStyleSheet('font-size: 13px;')
        ecid_label.setAlignment(Qt.AlignCenter)
        ecid_label.setTextFormat(Qt.RichText)
        layout.addWidget(ecid_label)

        msg = QLabel('Please register this ECID to use the tool.')
        msg.setStyleSheet('font-size: 12px; color: #9BC2D4;')
        msg.setAlignment(Qt.AlignCenter)
        layout.addWidget(msg)

        btn_ok = QPushButton('OK')
        btn_ok.setFixedWidth(80)
        btn_ok.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        layout.addLayout(btn_row)

# ===== Detector =====
class Detector(QObject):
    detected = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._stop = False
        self._paused = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            if not self._paused:
                try:
                    dev, serial = _dfu_serial(dispose=True)
                    if dev is None:
                        info = None
                    else:
                        cpid = _serial_field(serial, "CPID")
                        bdid = _serial_field(serial, "BDID")
                        ecid = _serial_field(serial, "ECID")
                        pwnd = "PWND:[" in serial
                        info = (cpid, bdid, ecid, pwnd)
                except Exception:
                    info = None
                try:
                    self.detected.emit(info)
                except RuntimeError:
                    return
            end = time.time() + 1.2
            while not self._stop and time.time() < end:
                time.sleep(0.1)

# ===== Worker (avec logs détaillés) =====
class Obliter8Worker(QObject):
    step = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            print("🔍 Récupération du périphérique DFU...")
            dev, serial = _dfu_serial()
            if dev is None:
                raise Exception("DFU device not found")

            print(f"📋 Serial: {serial}")
            cpid = _serial_field(serial, "CPID")
            if cpid is None:
                raise Exception(f"CPID not found in serial: {serial}")
            print(f"📱 CPID: {cpid}")

            entry = identify(f"0x{cpid.lower()}", _serial_field(serial, "BDID"))
            if entry is None:
                raise Exception(f"Device not supported (CPID: {cpid})")

            name, codename = entry
            print(f"✅ Device identified: {name} ({codename})")

            ecid_hex = _serial_field(serial, "ECID")
            ecid = int(ecid_hex, 16) if ecid_hex else None
            print(f"🔑 ECID: {ecid}")

            ibec_path = ibec_path_for(codename)
            print(f"📂 iBEC path: {ibec_path}")
            if not ibec_path.exists():
                raise Exception(f"iBEC file not found: {ibec_path}")

            print("📤 Uploading iBEC...")
            self.step.emit(f"Uploading {ibec_path.name}...")
            dfu_upload(dev, ibec_path.read_bytes())

            print("🚀 Booting iBEC...")
            self.step.emit("Booting iBEC...")
            dfu_boot(dev)

            # ---- Attente du mode recovery ----
            print("⏳ Waiting for recovery mode...")
            self.step.emit("Waiting for recovery mode...")
            recovery_found = False
            for i in range(30):
                time.sleep(1)
                rec_dev = _recovery_find()
                if rec_dev is not None:
                    recovery_found = True
                    print(f"✅ Recovery device found after {i+1} seconds")
                    break
                print(f"⏳ Recovery not found, waiting... ({i+1}/30)")

            if not recovery_found:
                raise Exception("Recovery device not found after iBEC boot")

            print("🔗 Connecting to recovery...")
            self.step.emit("Connecting to recovery...")
            irecv = IRecv(ecid=ecid, is_recovery=True, timeout=120)

            print("📝 Sending obliteration commands...")
            self.step.emit("Sending obliteration commands...")
            for cmd in ("setenv oblit-inprogress 5",
                        "setenv auto-boot true",
                        "saveenv"):
                print(f"  ➜ Sending: {cmd}")
                irecv.send_command(cmd)

            try:
                print("🔄 Rebooting...")
                irecv.send_command("reboot")
            except Exception:
                pass

            print("✅ Process finished successfully!")
            self.finished_ok.emit()

        except Exception as e:
            print(f"❌ ERROR: {e}")
            self.failed.emit(str(e))

# ===== Custom widgets =====
class CopyableLabel(QLabel):
    def __init__(self):
        super().__init__("\u2013")
        self.setObjectName("monoLbl")
        self.setAlignment(Qt.AlignRight)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Cliquer pour copier")
        self._value = ""

    def set_value(self, text):
        self._value = text or ""
        self.setText(text or "\u2013")

    def mousePressEvent(self, event):
        if self._value:
            QApplication.clipboard().setText(self._value)
            original = self.text()
            self.setText("Copié !")
            QTimer.singleShot(800, lambda: self.setText(original))
        super().mousePressEvent(event)

class Badge(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("badge")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(6)
        self._icon_lbl = QLabel()
        self._text_lbl = QLabel()
        f = self._text_lbl.font()
        f.setPointSize(10)
        f.setBold(True)
        self._text_lbl.setFont(f)
        layout.addWidget(self._icon_lbl)
        layout.addWidget(self._text_lbl)

    def set_state(self, text, fg, bg, icon=None):
        self._text_lbl.setText(text)
        self._text_lbl.setStyleSheet(f"color:{fg};")
        self.setStyleSheet(f"#badge {{ background:{bg}; border-radius:12px; }}")
        if icon:
            self._icon_lbl.setPixmap(render_icon(icon, fg, 14))
            self._icon_lbl.show()
        else:
            self._icon_lbl.clear()
            self._icon_lbl.hide()

# ===== MainWindow =====
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mobi Doc Eraser Passcode V1.0")

        logo_path = resource_path('logo.png')
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))
        else:
            for ext in ['.ico', '.icns']:
                alt_path = resource_path(f'logo{ext}')
                if os.path.exists(alt_path):
                    self.setWindowIcon(QIcon(alt_path))
                    break

        self.resize(420, 380)
        self._busy = False
        self._step_index = -1
        self._ecid_valid = None
        self._current_ecid = None
        self._current_model = None
        self._pwnd = False
        self._ibec_ok = False
        self._device_supported = False
        self._build_ui()
        self._apply_theme()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)
        self._detector = Detector()
        self._detector.detected.connect(self._on_detected)
        self._show_device("Waiting for device...", None, False)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        card = QFrame()
        card.setObjectName("card")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(20, 18, 20, 20)
        card_l.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(10)

        logo_path = resource_path('logo.png')
        if os.path.exists(logo_path):
            icon = QLabel()
            pix = load_retina_pixmap(logo_path, 40)
            icon.setPixmap(pix)
            icon.setFixedSize(44, 44)
            icon.setAlignment(Qt.AlignCenter)
            icon.setObjectName("icon")
            header.addWidget(icon)
        else:
            icon = QLabel()
            icon.setPixmap(render_icon("phone", ACCENT, 18))
            icon.setFixedSize(36, 36)
            icon.setAlignment(Qt.AlignCenter)
            icon.setObjectName("icon")
            header.addWidget(icon)

        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        self.name_lbl = QLabel("Waiting for device...")
        self.name_lbl.setObjectName("nameLbl")
        f = self.name_lbl.font()
        f.setPointSize(13)
        f.setBold(True)
        self.name_lbl.setFont(f)
        self.sub_lbl = QLabel(" ")
        self.sub_lbl.setObjectName("subLbl")
        name_col.addWidget(self.name_lbl)
        name_col.addWidget(self.sub_lbl)
        header.addLayout(name_col, 1)

        self.pwnd_badge = Badge()
        header.addWidget(self.pwnd_badge, alignment=Qt.AlignTop)
        card_l.addLayout(header)

        info = QFrame()
        info.setObjectName("subcard")
        grid = QGridLayout(info)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        ecid_label = QLabel("ECID")
        ecid_label.setObjectName("mutedLbl")
        self.ecid_val = CopyableLabel()
        grid.addWidget(ecid_label, 0, 0)
        grid.addWidget(self.ecid_val, 0, 1)

        self.target_val = self._info_row(grid, 1, "iBoot cible")
        self.mode_val = self._info_row(grid, 2, "Mode")
        self.mode_val.setText("DFU")
        card_l.addWidget(info)

        prog_head = QHBoxLayout()
        self.step_lbl = QLabel("Ready")
        self.step_lbl.setObjectName("subLbl")
        prog_head.addWidget(self.step_lbl)
        prog_head.addStretch(1)
        self.step_count_lbl = QLabel(" ")
        self.step_count_lbl.setObjectName("mutedLbl")
        prog_head.addWidget(self.step_count_lbl)
        card_l.addLayout(prog_head)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setRange(0, len(STEPS))
        self.progress.setValue(0)
        card_l.addWidget(self.progress)

        self.run_btn = QPushButton("Eraser")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.setFixedHeight(44)
        self.run_btn.setDefault(True)
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._start)
        card_l.addWidget(self.run_btn)

        outer.addWidget(card)
        outer.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        tg_icon = QLabel()
        tg_icon.setPixmap(render_icon("send", TEXT_MUTED, 13))
        footer.addWidget(tg_icon)
        tg_link = QLabel(
            f'<a href="{TELEGRAM_URL}" style="color:{TEXT_SECOND}; '
            f'text-decoration:none;">@{TELEGRAM_HANDLE}</a>'
        )
        tg_link.setObjectName("footerLink")
        tg_link.setOpenExternalLinks(True)
        tg_link.setCursor(Qt.PointingHandCursor)
        footer.addWidget(tg_link)
        footer.addStretch(1)
        outer.addLayout(footer)
        outer.addSpacing(4)

    @staticmethod
    def _info_row(grid, row, label_text):
        label = QLabel(label_text)
        label.setObjectName("mutedLbl")
        value = QLabel("\u2013")
        value.setObjectName("monoLbl")
        value.setAlignment(Qt.AlignRight)
        grid.addWidget(label, row, 0)
        grid.addWidget(value, row, 1)
        return value

    def _apply_theme(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background: {BG_PAGE}; }}
            QWidget {{ color: {TEXT_PRIMARY}; font-family: -apple-system, Segoe UI, sans-serif; }}
            #card {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
            #subcard {{ background: {BG_SUBCARD}; border-radius: 8px; }}
            #icon {{ background: {BG_SUBCARD}; border-radius: 8px; }}
            #nameLbl {{ color: {TEXT_PRIMARY}; }}
            #subLbl {{ color: {TEXT_SECOND}; font-size: 12px; }}
            #mutedLbl {{ color: {TEXT_MUTED}; font-size: 11px; }}
            #monoLbl {{
                color: {ACCENT}; font-family: {MONO_FONT}; font-size: 12px;
                padding: 2px 4px; border-radius: 4px;
            }}
            #monoLbl:hover {{ background: {BG_CARD}; }}
            #footerLink {{ font-size: 12px; }}
            #footerLink a:hover {{ color: {ACCENT}; }}
            #runBtn {{
                background-color: {ACCENT};
                color: #04141A;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }}
            #runBtn:hover {{
                background-color: {ACCENT_HOVER};
            }}
            #runBtn:disabled {{
                background: {BG_SUBCARD};
                color: {TEXT_MUTED};
            }}
            QProgressBar {{ background: {BG_SUBCARD}; border: none; border-radius: 3px; }}
            QProgressBar::chunk {{
                background: {ACCENT};
                border-radius: 3px;
            }}
        """)

    def _show_device(self, name, sub, pwnd, pwnd_known=True, can_run=False, error=False):
        self.name_lbl.setText(name)
        self.name_lbl.setStyleSheet(f"color:{RED};" if error else "")
        self.sub_lbl.setText(sub or " ")
        if pwnd_known:
            if pwnd:
                self.pwnd_badge.set_state("PWNED", GREEN, GREEN_BG, "lock-open")
            else:
                self.pwnd_badge.set_state("NOT PWNED", RED, RED_BG, "lock-closed")
        else:
            self.pwnd_badge.set_state(" ", TEXT_MUTED, BG_SUBCARD)
        self.run_btn.setEnabled(can_run and not self._busy)

    def _on_detected(self, info):
        if info is None:
            self._show_device("Waiting for device...", "Connect a device in DFU mode",
                               False, pwnd_known=False)
            self.ecid_val.set_value(None)
            self.target_val.setText("\u2013")
            self._current_ecid = None
            self._current_model = None
            self._ecid_valid = None
            self._pwnd = False
            self._ibec_ok = False
            self._device_supported = False
            self.run_btn.setEnabled(False)
            return

        cpid, bdid, ecid, pwnd = info
        cpid_key = f"0x{cpid.lower()}" if cpid else None
        entry = identify(cpid_key, bdid)
        self._current_ecid = ecid
        self._pwnd = pwnd
        self.ecid_val.set_value(ecid)

        if entry is None:
            label = (f"Unsupported (CPID:{cpid})" if cpid_key not in SUPPORTED_CPIDS
                      else f"Unknown board (CPID:{cpid} BDID:{bdid})")
            self._show_device(label, f"CPID:{cpid} BDID:{bdid}", pwnd, can_run=False, error=True)
            self.target_val.setText("\u2013")
            self._current_model = None
            self._ecid_valid = None
            self._ibec_ok = False
            self._device_supported = False
            self.run_btn.setEnabled(False)
            return

        name, codename = entry
        self._current_model = name
        self._ibec_ok = ibec_path_for(codename).is_file()
        self._device_supported = True

        if ecid:
            self._check_ecid(ecid)
        else:
            self._ecid_valid = False

        can_run = pwnd and self._ibec_ok
        self._show_device(name, f"CPID:{cpid} BDID:{bdid}", pwnd, can_run=can_run)
        self.target_val.setText(codename)

    def _check_ecid(self, ecid):
        def task():
            valid = check_ecid_online(ecid)
            QTimer.singleShot(0, lambda: self._update_ecid_status(valid))
        threading.Thread(target=task, daemon=True).start()

    def _update_ecid_status(self, valid):
        self._ecid_valid = valid
        if valid:
            self.step_lbl.setText("✅ ECID valid")
        else:
            self.step_lbl.setText("❌ ECID not registered")

    def _start(self):
        if self._ecid_valid is None:
            self.step_lbl.setText("⏳ Checking ECID...")
            valid = check_ecid_online(self._current_ecid)
            self._ecid_valid = valid
            if valid:
                self.step_lbl.setText("✅ ECID valid")
            else:
                self.step_lbl.setText("❌ ECID not registered")

        if not self._ecid_valid:
            dlg = RegisterDialog(self, ecid=self._current_ecid)
            dlg.exec_()
            return

        self._detector._paused = True
        self._busy = True
        self._step_index = -1
        self.run_btn.setEnabled(False)
        self.progress.setValue(0)
        self.step_lbl.setText("Starting...")
        self.step_count_lbl.setText(f"Étape 0 / {len(STEPS)}")

        self._worker = Obliter8Worker()
        self._worker.step.connect(self._on_step)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_step(self, text):
        self.step_lbl.setText(text)
        self._step_index = min(self._step_index + 1, len(STEPS) - 1)
        self.progress.setValue(self._step_index + 1)
        self.step_count_lbl.setText(f"Étape {self._step_index + 1} / {len(STEPS)}")

    def _on_finished_ok(self):
        ecid = self._current_ecid or "N/A"
        model = self._current_model or "N/A"
        send_telegram_report("✅ Device Erased successfully!", ecid, model)

        dlg = SuccessDialog(self, device_model=model)
        dlg.exec_()

        self._finish("Terminé ✅", ok=True)

    def _on_failed(self, msg):
        QMessageBox.critical(self, "Mobi Doc Eraser Passcode V1.0", f"Erase failed:\n{msg}")
        send_telegram_report(f"❌ Erase failed: {msg}", self._current_ecid or "N/A", self._current_model or "N/A")
        self._finish("Échec ❌", ok=False, msg=msg)

    def _finish(self, status, ok, msg=None):
        self._busy = False
        self._detector._paused = False
        self.step_lbl.setText(status)
        if ok:
            self.progress.setValue(len(STEPS))
        else:
            QTimer.singleShot(3000, lambda: self.step_lbl.setText("Ready"))
        if msg and not ok:
            QMessageBox.critical(self, "Mobi Doc Eraser Passcode", msg)

    def closeEvent(self, event):
        self._detector._stop = True
        event.accept()

# ===== Entry point =====
def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
