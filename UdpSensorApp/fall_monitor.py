import sys
import socket
import threading
import time
import csv
import winsound
import smtplib
import pyttsx3
import os

try:
    import pythoncom
except ImportError:
    pythoncom = None

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView

    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False

from collections import deque
from datetime import datetime
import numpy as np

# 🔴 修复：添加了 QTabWidget 到导入列表
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QGroupBox, QGridLayout, QTextEdit, QDialog, QFrame,
                             QTabWidget)
from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QLinearGradient, QPalette, QBrush

import pyqtgraph as pg
import pyqtgraph.opengl as gl

# --- 常量配置 ---
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465
PHONE_LISTEN_PORT = 5556

# --- 地图模板 ---
MAP_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.7.1/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.7.1/dist/leaflet.js"></script>
    <style>body,html,#map{width:100%;height:100%;margin:0;padding:0;}</style>
</head>
<body>
    <div id="map"></div>
    <script>
        var map = L.map('map').setView([LAT, LON], 16);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution: '&copy; OpenStreetMap'}).addTo(map);
        var sosIcon = L.icon({
            iconUrl: 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-2x-red.png',
            shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/0.7.7/images/marker-shadow.png',
            iconSize: [25, 41], iconAnchor: [12, 41], popupAnchor: [1, -34], shadowSize: [41, 41]
        });
        L.marker([LAT, LON], {icon: sosIcon}).addTo(map).bindPopup('<b>⚠️ 紧急位置</b><br>Lat: LAT<br>Lon: LON').openPopup();
    </script>
</body>
</html>
"""


# --- 算法：卡尔曼滤波 ---
class KalmanFilter:
    def __init__(self, process_noise=1e-4, measurement_noise=5e-2, estimated_error=1.0):
        self.q = process_noise
        self.r = measurement_noise
        self.p = estimated_error
        self.x = 0.0

    def update(self, measurement):
        self.p = self.p + self.q
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (measurement - self.x)
        self.p = (1 - k) * self.p
        return self.x


# --- 语音线程 ---
class VoiceAssistant(threading.Thread):
    def __init__(self):
        super().__init__()
        self.queue = deque()
        self.running = True
        self.daemon = True

    def speak(self, text):
        if len(self.queue) < 3: self.queue.append(text)

    def clear_queue(self):
        self.queue.clear()

    def run(self):
        if pythoncom: pythoncom.CoInitialize()
        while self.running:
            if len(self.queue) > 0:
                text = self.queue.popleft()
                try:
                    engine = pyttsx3.init()
                    engine.setProperty('rate', 170)
                    engine.say(text)
                    engine.runAndWait()
                    del engine
                except:
                    pass
            else:
                time.sleep(0.1)
        if pythoncom: pythoncom.CoUninitialize()


# --- 地图弹窗 ---
class SOSMapWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📍 紧急救援定位系统")
        self.resize(900, 600)
        self.setStyleSheet("background-color: #121212;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if HAS_WEBENGINE:
            self.browser = QWebEngineView()
            layout.addWidget(self.browser)
        else:
            lbl = QLabel("地图模块未安装 (PyQtWebEngine)")
            lbl.setStyleSheet("color: white; font-size: 18px;")
            lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(lbl)

    def load_map(self, lat, lon):
        if HAS_WEBENGINE:
            self.browser.setHtml(MAP_HTML_TEMPLATE.replace("LAT", str(lat)).replace("LON", str(lon)))


# --- 主程序窗口 ---
class FallMonitorApp(QMainWindow):
    data_received = pyqtSignal(float, float, float, float, float, float, float, int, int, float, float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AIoT 智能体感监护系统 V9.0 Ultra")
        self.resize(1600, 1000)

        # 核心初始化
        self.voice = VoiceAssistant()
        self.voice.start()
        self.map_window = SOSMapWindow(self)
        self.kf_x = KalmanFilter()
        self.kf_y = KalmanFilter()
        self.kf_z = KalmanFilter()

        # 数据结构
        self.buf_size = 300
        self.data_x = deque([0.0] * self.buf_size, maxlen=self.buf_size)
        self.data_y = deque([0.0] * self.buf_size, maxlen=self.buf_size)
        self.data_z = deque([0.0] * self.buf_size, maxlen=self.buf_size)
        self.data_svm = deque([0.0] * self.buf_size, maxlen=self.buf_size)
        self.poincare_x = deque([0.0] * 100, maxlen=100)
        self.poincare_y = deque([0.0] * 100, maxlen=100)

        # 状态变量
        self.fall_state = "NORMAL"
        self.last_state_change = time.time()
        self.last_alarm_time = 0
        self.activity_status = "初始化"
        self.last_act_change = time.time()
        self.step_count = 0
        self.last_step_time = 0
        self.curr_gps = (0.0, 0.0)
        self.client_addr = None
        self.is_recording = False
        self.csv_file = None
        self.csv_writer = None

        # 网络
        self.udp_socket = None
        self.is_running = False
        self.data_received.connect(self.process_data)

        # UI 构建
        self.setup_ui()

        # 定时器
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(30)

        self.log("系统核心服务已启动...")
        self.voice.speak("系统就绪")

    def setup_ui(self):
        # 全局样式表 (高端黑金/青色风格)
        self.setStyleSheet("""
            QMainWindow { background-color: #0F1115; }
            QWidget { font-family: 'Segoe UI', sans-serif; color: #E0E0E0; }

            QGroupBox { 
                background-color: #161920; 
                border: 1px solid #2A2F3A; 
                border-radius: 8px; 
                margin-top: 24px; 
                font-weight: bold;
                font-size: 13px;
                color: #00E5FF;
            }
            QGroupBox::title { 
                subcontrol-origin: margin; 
                left: 12px; 
                padding: 0 5px; 
                background-color: #161920;
            }

            QLabel#ValueLabel { font-family: 'Consolas'; font-size: 20px; font-weight: bold; }
            QLabel#InfoLabel { color: #90A4AE; font-size: 12px; }

            QPushButton {
                background-color: #1F232C;
                border: 1px solid #3E4552;
                border-radius: 6px;
                color: #FFF;
                padding: 8px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #00E5FF; color: #000; border: 1px solid #00E5FF; }
            QPushButton:pressed { background-color: #00B8D4; }

            QPushButton#SOSBtn { background-color: #D32F2F; border: none; }
            QPushButton#SOSBtn:hover { background-color: #F44336; }
            QPushButton#SOSBtn:disabled { background-color: #262626; color: #555; }

            QLineEdit { background: #0F1115; border: 1px solid #333; padding: 5px; border-radius: 4px; color: #FFF; }
            QTextEdit { background: #0F1115; border: 1px solid #333; border-radius: 4px; color: #00E5FF; font-family: 'Consolas'; }

            QTabWidget::pane { border: 1px solid #333; background: #161920; }
            QTabBar::tab { background: #1F232C; color: #888; padding: 10px 25px; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px;}
            QTabBar::tab:selected { background: #2A2F3A; color: #00E5FF; border-bottom: 2px solid #00E5FF; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # === 顶部 Header ===
        header = QHBoxLayout()
        title_lbl = QLabel("AIoT SMART GUARDIAN SYSTEM")
        title_lbl.setStyleSheet("font-size: 24px; font-weight: 900; color: #FFF; letter-spacing: 2px;")
        header.addWidget(title_lbl)
        header.addStretch()

        self.status_badge = QLabel(" ● DISCONNECTED ")
        self.status_badge.setStyleSheet(
            "background: #263238; color: #546E7A; padding: 6px 12px; border-radius: 15px; font-weight: bold;")
        header.addWidget(self.status_badge)
        main_layout.addLayout(header)

        # === 内容区 ===
        content_layout = QHBoxLayout()

        # --- 左侧列 (监控仪表盘) ---
        left_col = QVBoxLayout()
        left_col.setSpacing(15)

        # 1. 核心状态卡片
        status_box = QGroupBox("MONITOR STATUS")
        status_layout = QVBoxLayout()

        self.lbl_alarm = QLabel("SYSTEM SECURE")
        self.lbl_alarm.setAlignment(Qt.AlignCenter)
        self.lbl_alarm.setFixedHeight(60)
        self.lbl_alarm.setStyleSheet(
            "background: #1B5E20; color: #FFF; border-radius: 6px; font-size: 22px; font-weight: 900; letter-spacing: 1px;")
        status_layout.addWidget(self.lbl_alarm)

        self.btn_reset = QPushButton("解除警报 (SAFE)")
        self.btn_reset.setObjectName("SOSBtn")
        self.btn_reset.setFixedHeight(40)
        self.btn_reset.setEnabled(False)
        self.btn_reset.clicked.connect(lambda: self.manual_reset())
        status_layout.addWidget(self.btn_reset)
        status_box.setLayout(status_layout)
        left_col.addWidget(status_box)

        # 2. 传感器数据
        sensor_box = QGroupBox("SENSOR TELEMETRY")
        grid = QGridLayout()
        self.val_x = self.mk_val_label("0.00", "#FF5252")
        self.val_y = self.mk_val_label("0.00", "#69F0AE")
        self.val_z = self.mk_val_label("0.00", "#448AFF")
        self.val_svm = self.mk_val_label("0.00", "#FFD740")

        grid.addWidget(QLabel("ACC X"), 0, 0);
        grid.addWidget(self.val_x, 0, 1)
        grid.addWidget(QLabel("ACC Y"), 1, 0);
        grid.addWidget(self.val_y, 1, 1)
        grid.addWidget(QLabel("ACC Z"), 2, 0);
        grid.addWidget(self.val_z, 2, 1)
        grid.addWidget(QLabel("SVM (G)"), 3, 0);
        grid.addWidget(self.val_svm, 3, 1)
        sensor_box.setLayout(grid)
        left_col.addWidget(sensor_box)

        # 3. 健康与环境
        health_box = QGroupBox("HEALTH & ENV")
        h_layout = QGridLayout()

        self.val_steps = self.mk_val_label("0", "#00E5FF")
        self.val_act = QLabel("静止")
        self.val_act.setStyleSheet("color: #FFF; font-size: 16px; font-weight: bold;")
        self.val_gps = QLabel("Waiting...")
        self.val_gps.setStyleSheet("color: #78909C; font-family: Consolas; font-size: 11px;")
        self.val_gps.setWordWrap(True)

        h_layout.addWidget(QLabel("步数:"), 0, 0);
        h_layout.addWidget(self.val_steps, 0, 1)
        h_layout.addWidget(QLabel("状态:"), 1, 0);
        h_layout.addWidget(self.val_act, 1, 1)
        h_layout.addWidget(QLabel("GPS:"), 2, 0);
        h_layout.addWidget(self.val_gps, 2, 1)

        # 目标步数控制
        ctrl_layout = QHBoxLayout()
        self.in_target = QLineEdit("100");
        self.in_target.setFixedWidth(50);
        self.in_target.setAlignment(Qt.AlignCenter)
        btn_set = QPushButton("重置");
        btn_set.setFixedSize(50, 26);
        btn_set.clicked.connect(self.reset_steps)
        ctrl_layout.addWidget(QLabel("目标:"))
        ctrl_layout.addWidget(self.in_target)
        ctrl_layout.addWidget(btn_set)
        h_layout.addLayout(ctrl_layout, 3, 0, 1, 2)

        health_box.setLayout(h_layout)
        left_col.addWidget(health_box)

        # 4. 3D 视图
        gl_box = QGroupBox("ATTITUDE VISUALIZER")
        gl_l = QVBoxLayout()
        self.gl_view = gl.GLViewWidget()
        self.gl_view.setBackgroundColor('#161920')
        self.gl_view.setCameraPosition(distance=18, elevation=20)
        g = gl.GLGridItem();
        g.scale(2, 2, 1);
        self.gl_view.addItem(g)
        self.phone_model = gl.GLBoxItem(size=None, color=(0, 229, 255, 180))
        self.phone_model.setSize(x=2.5, y=5.0, z=0.3)
        self.phone_model.translate(-1.25, -2.5, 0)
        self.gl_view.addItem(self.phone_model)
        gl_l.addWidget(self.gl_view)
        gl_box.setLayout(gl_l)
        left_col.addWidget(gl_box)

        content_layout.addLayout(left_col, 35)  # 左列占比 35%

        # --- 右侧列 (分析图表) ---
        right_col = QVBoxLayout()

        # Tab Widget
        tabs = QTabWidget()

        # Tab 1: 时域图
        pg.setConfigOption('background', '#161920')
        pg.setConfigOption('foreground', '#B0BEC5')
        p1 = pg.PlotWidget()
        p1.showGrid(x=True, y=True, alpha=0.1)
        p1.addLegend(offset=(10, 10))
        self.curve_x = p1.plot(pen=pg.mkPen('#FF5252', width=2), name="X")
        self.curve_y = p1.plot(pen=pg.mkPen('#69F0AE', width=2), name="Y")
        self.curve_z = p1.plot(pen=pg.mkPen('#448AFF', width=2), name="Z")
        self.curve_svm = p1.plot(pen=pg.mkPen('#FFD740', width=2, style=Qt.DashLine), name="SVM")
        tabs.addTab(p1, "TIME DOMAIN")

        # Tab 2: 庞加莱图
        p2 = pg.PlotWidget()
        p2.setLabel('left', 'SVM[n+1]');
        p2.setLabel('bottom', 'SVM[n]')
        self.scat = pg.ScatterPlotItem(size=8, pen=pg.mkPen(None), brush=pg.mkBrush(0, 229, 255, 150))
        p2.addItem(self.scat)
        tabs.addTab(p2, "PHASE SPACE")

        # Tab 3: FFT
        p3 = pg.PlotWidget()
        self.curve_fft = p3.plot(pen=pg.mkPen('#E040FB', width=2), fillLevel=0, brush=(224, 64, 251, 50))
        tabs.addTab(p3, "FREQUENCY (FFT)")

        right_col.addWidget(tabs, 70)

        # 日志区
        log_box = QGroupBox("SYSTEM LOGS")
        log_l = QVBoxLayout()
        self.log_txt = QTextEdit()
        self.log_txt.setReadOnly(True)
        log_l.addWidget(self.log_txt)
        log_box.setLayout(log_l)
        right_col.addWidget(log_box, 30)

        # 底部连接控制
        conn_layout = QHBoxLayout()
        self.in_port = QLineEdit("5555");
        self.in_port.setFixedWidth(60)
        self.btn_listen = QPushButton("启动服务");
        self.btn_listen.clicked.connect(self.toggle_server)
        self.btn_rec = QPushButton("录制数据");
        self.btn_rec.clicked.connect(self.toggle_rec)

        conn_layout.addWidget(QLabel("端口:"))
        conn_layout.addWidget(self.in_port)
        conn_layout.addWidget(self.btn_listen)
        conn_layout.addWidget(self.btn_rec)
        right_col.addLayout(conn_layout)

        content_layout.addLayout(right_col, 65)  # 右列占比 65%
        main_layout.addLayout(content_layout)

    def mk_val_label(self, txt, color):
        l = QLabel(txt)
        l.setObjectName("ValueLabel")
        l.setStyleSheet(f"color: {color};")
        l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return l

    def log(self, msg):
        t = datetime.now().strftime("%H:%M:%S")
        self.log_txt.append(f"<font color='#546E7A'>[{t}]</font> {msg}")

    # --- 逻辑功能 ---
    def toggle_server(self):
        if not self.is_running:
            try:
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_socket.bind(('0.0.0.0', int(self.in_port.text())))
                self.udp_socket.settimeout(0.5)
                self.is_running = True
                self.listen_thread = threading.Thread(target=self.udp_worker)
                self.listen_thread.daemon = True
                self.listen_thread.start()
                self.btn_listen.setText("停止服务");
                self.btn_listen.setStyleSheet("background-color: #C62828;")
                self.status_badge.setText(" ● LISTENING ");
                self.status_badge.setStyleSheet(
                    "background: #00C853; color: #FFF; padding: 6px 12px; border-radius: 15px; font-weight: bold;")
                self.log("服务启动，正在监听...")
            except Exception as e:
                self.log(f"启动失败: {e}")
        else:
            self.is_running = False;
            self.udp_socket.close()
            self.btn_listen.setText("启动服务");
            self.btn_listen.setStyleSheet("")
            self.status_badge.setText(" ● DISCONNECTED ");
            self.status_badge.setStyleSheet(
                "background: #263238; color: #546E7A; padding: 6px 12px; border-radius: 15px; font-weight: bold;")
            self.log("服务已停止")

    def udp_worker(self):
        while self.is_running:
            try:
                data, addr = self.udp_socket.recvfrom(1024)
                if self.client_addr != addr: self.client_addr = addr; self.log(f"新设备接入: {addr[0]}")
                parts = data.decode('utf-8').strip().split(',')
                if len(parts) >= 11:
                    self.data_received.emit(*[float(p) for p in parts[:7]], int(parts[7]), int(parts[8]),
                                            float(parts[9]), float(parts[10]))
            except:
                pass

    def process_data(self, ax, ay, az, gx, gy, gz, light, batt, sos, lat, lon):
        # 滤波 & 计算
        cx = self.kf_x.update(ax);
        cy = self.kf_y.update(ay);
        cz = self.kf_z.update(az)
        svm = np.sqrt(cx ** 2 + cy ** 2 + cz ** 2)
        self.curr_gps = (lat, lon)

        # 缓存
        self.data_x.append(cx);
        self.data_y.append(cy);
        self.data_z.append(cz);
        self.data_svm.append(svm)
        if len(self.data_svm) > 2: self.poincare_x.append(self.data_svm[-2]); self.poincare_y.append(self.data_svm[-1])

        # 逻辑
        if sos == 2 and self.fall_state != "NORMAL":
            self.log("收到[误报反馈]，解除警报");
            self.manual_reset(auto=True)
        elif sos == 1:
            self.trigger_alarm(is_sos=True)
        else:
            self.detect_fall(svm)

        self.check_act(svm);
        self.count_steps(svm)

        # 录制
        if self.is_recording: self.csv_writer.writerow(
            [datetime.now(), cx, cy, cz, svm, gx, gy, gz, light, batt, sos, lat, lon])

    def detect_fall(self, svm):
        if self.fall_state == "SOS": return
        now = time.time()
        if self.fall_state == "NORMAL":
            if svm > 25.0 and (now - self.last_state_change > 2.0):
                self.fall_state = "IMPACT";
                self.last_state_change = now;
                self.trigger_alarm(is_fall=True)
        elif self.fall_state == "IMPACT" and (now - self.last_alarm_time > 3.0):
            self.trigger_alarm(is_fall=True);
            self.last_alarm_time = now

    def trigger_alarm(self, is_fall=False, is_sos=False):
        self.send_cmd("ALERT")
        self.btn_reset.setEnabled(True)

        if is_sos:
            if self.fall_state != "SOS":
                self.fall_state = "SOS"
                self.lbl_alarm.setText("🆘 SOS SIGNAL 🆘")
                self.lbl_alarm.setStyleSheet(
                    "background: #B71C1C; color: #FFF; border: 3px solid #FFD740; font-size: 22px; font-weight: 900; animation: blink 0.5s infinite;")
                self.voice.clear_queue();
                self.voice.speak("收到紧急求救信号")
                if abs(self.curr_gps[0]) > 0.1 and not self.map_window.isVisible(): self.map_window.load_map(
                    *self.curr_gps); self.map_window.show()
        elif is_fall:
            self.lbl_alarm.setText("⚠️ FALL DETECTED")
            self.lbl_alarm.setStyleSheet("background: #E65100; color: #FFF; font-size: 22px; font-weight: 900;")
            self.voice.speak("检测到跌倒")
            try:
                winsound.Beep(1000, 500)
            except:
                pass
            if abs(self.curr_gps[0]) > 0.1 and not self.map_window.isVisible(): self.map_window.load_map(
                *self.curr_gps); self.map_window.show()

    def manual_reset(self, auto=False):
        self.fall_state = "NORMAL"
        self.btn_reset.setEnabled(False)
        self.lbl_alarm.setText("SYSTEM SECURE")
        self.lbl_alarm.setStyleSheet(
            "background: #1B5E20; color: #FFF; border-radius: 6px; font-size: 22px; font-weight: 900;")
        self.send_cmd("SAFE")
        self.voice.clear_queue();
        self.voice.speak("警报已解除" if not auto else "误报已确认")
        self.map_window.hide()

    def send_cmd(self, cmd):
        if self.udp_socket and self.client_addr:
            try:
                self.udp_socket.sendto(cmd.encode(), (self.client_addr[0], PHONE_LISTEN_PORT))
            except:
                pass

    def check_act(self, svm):
        prev = self.activity_status
        self.activity_status = "静止" if abs(svm - 9.8) < 0.8 else ("剧烈" if abs(svm - 9.8) > 5.0 else "活动")
        if prev != self.activity_status: self.last_act_change = time.time()
        # 久坐逻辑
        if self.fall_state == "NORMAL" and self.activity_status == "静止" and (time.time() - self.last_act_change) > 30:
            if int(time.time()) % 10 == 0: self.voice.speak("请起身活动")

    def count_steps(self, svm):
        th = 10.6 if self.activity_status == "活动" else 11.2
        if svm > th and (time.time() - self.last_step_time) > 0.3:
            self.step_count += 1;
            self.last_step_time = time.time()
            try:
                if self.step_count == int(self.in_target.text()): self.voice.speak("目标达成")
            except:
                pass

    def reset_steps(self):
        self.step_count = 0; self.log("步数已重置")

    def toggle_rec(self):
        if not self.is_recording:
            fn, _ = QFileDialog.getSaveFileName(self, "Save", "", "CSV(*.csv)")
            if fn:
                self.csv_file = open(fn, 'w', newline='');
                self.csv_writer = csv.writer(self.csv_file)
                self.is_recording = True;
                self.btn_rec.setText("停止录制");
                self.btn_rec.setStyleSheet("background-color: #C62828;")
        else:
            self.is_recording = False;
            self.csv_file.close();
            self.btn_rec.setText("录制数据");
            self.btn_rec.setStyleSheet("")

    def update_ui(self):
        if not self.is_running: return
        self.val_x.setText(f"{self.data_x[-1]:.2f}");
        self.val_y.setText(f"{self.data_y[-1]:.2f}")
        self.val_z.setText(f"{self.data_z[-1]:.2f}");
        self.val_svm.setText(f"{self.data_svm[-1]:.2f}")
        self.val_steps.setText(str(self.step_count));
        self.val_act.setText(self.activity_status)
        if abs(self.curr_gps[0]) > 0.1: self.val_gps.setText(f"{self.curr_gps[0]:.5f}\n{self.curr_gps[1]:.5f}")

        self.curve_x.setData(self.data_x);
        self.curve_y.setData(self.data_y)
        self.curve_z.setData(self.data_z);
        self.curve_svm.setData(self.data_svm)
        self.scat.setData(list(self.poincare_x), list(self.poincare_y))

        if len(self.data_svm) >= 128:
            s = np.array(list(self.data_svm)[-128:]) - np.mean(list(self.data_svm)[-128:])
            self.curve_fft.setData(np.fft.rfftfreq(128, 1 / 30)[1:], (np.abs(np.fft.rfft(s)) / 128 * 2)[1:])

        try:
            self.phone_model.resetTransform()
            self.phone_model.translate(-1.25, -2.5, 0)
            self.phone_model.rotate(np.degrees(np.arctan2(-self.data_x[-1], self.data_z[-1])), 0, 1, 0)
            self.phone_model.rotate(
                np.degrees(np.arctan2(self.data_y[-1], np.sqrt(self.data_x[-1] ** 2 + self.data_z[-1] ** 2))), 1, 0, 0)
        except:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FallMonitorApp()
    window.show()
    sys.exit(app.exec_())