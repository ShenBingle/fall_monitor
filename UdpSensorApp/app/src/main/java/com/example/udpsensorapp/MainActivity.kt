package com.example.udpsensorapp

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.Locale

class MainActivity : AppCompatActivity(), SensorEventListener, LocationListener {

    // UI 控件
    private lateinit var etIp: EditText
    private lateinit var etPort: EditText
    private lateinit var tvAcc: TextView
    private lateinit var tvGyro: TextView
    private lateinit var tvLight: TextView
    private lateinit var tvBattery: TextView
    private lateinit var tvGps: TextView
    private lateinit var tvStatus: TextView // 右上角的状态标签
    private lateinit var btnToggle: Button
    private lateinit var btnSos: Button

    // 系统服务
    private lateinit var sensorManager: SensorManager
    private lateinit var locationManager: LocationManager
    private lateinit var vibrator: Vibrator
    private lateinit var prefs: SharedPreferences

    // 传感器对象
    private var accelerometer: Sensor? = null
    private var gyroscope: Sensor? = null
    private var lightSensor: Sensor? = null

    // 数据缓存
    private var valAcc = FloatArray(3)
    private var valGyro = FloatArray(3)
    private var valLight = 0f
    private var lat = 0.0
    private var lon = 0.0
    private var sosState = 0

    // 网络与线程控制
    private var isSending = false
    private var socket: DatagramSocket? = null
    private var recvSocket: DatagramSocket? = null
    private var lastSendTime = 0L
    private val SEND_INTERVAL = 20L // 发送间隔 (ms)

    private var alertDialog: AlertDialog? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        try {
            setContentView(R.layout.activity_main)

            // 1. 绑定控件 (ID 必须与 XML 一致)
            etIp = findViewById(R.id.et_ip)
            etPort = findViewById(R.id.et_port)
            tvAcc = findViewById(R.id.tv_acc)
            tvGyro = findViewById(R.id.tv_gyro)
            tvLight = findViewById(R.id.tv_light)
            tvBattery = findViewById(R.id.tv_battery)
            tvGps = findViewById(R.id.tv_gps)
            tvStatus = findViewById(R.id.tv_status)
            btnToggle = findViewById(R.id.btn_toggle)
            btnSos = findViewById(R.id.btn_sos)

            // 2. 初始化服务
            sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
            locationManager = getSystemService(Context.LOCATION_SERVICE) as LocationManager
            vibrator = getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            prefs = getSharedPreferences("AppConfig", Context.MODE_PRIVATE)

            accelerometer = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
            gyroscope = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
            lightSensor = sensorManager.getDefaultSensor(Sensor.TYPE_LIGHT)

            // 3. 读取上次保存的 IP 配置
            etIp.setText(prefs.getString("ip", "192.168."))
            etPort.setText(prefs.getString("port", "5555"))

            // 4. 设置监听器
            btnToggle.setOnClickListener {
                if (isSending) stopSystem() else checkPermissionsAndStart()
            }

            btnSos.setOnClickListener { triggerSOS() }

            // 5. 保持屏幕常亮
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

            // 初始化 UI 状态
            stopSystem()

        } catch (e: Exception) {
            e.printStackTrace()
            Toast.makeText(this, "启动错误: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun checkPermissionsAndStart() {
        val hasFine = ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val hasCoarse = ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

        if (!hasFine || !hasCoarse) {
            ActivityCompat.requestPermissions(this, arrayOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
            ), 1)
        } else {
            startSystem()
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1 && grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startSystem()
        } else {
            Toast.makeText(this, "需要定位权限才能获取GPS信息", Toast.LENGTH_SHORT).show()
        }
    }

    @SuppressLint("MissingPermission")
    private fun startSystem() {
        if (!locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER) &&
            !locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
            Toast.makeText(this, "请在设置中打开手机定位服务(GPS)", Toast.LENGTH_LONG).show()
            return
        }

        val ipStr = etIp.text.toString()
        prefs.edit().putString("ip", ipStr).apply()
        prefs.edit().putString("port", etPort.text.toString()).apply()

        try {
            socket = DatagramSocket()
            recvSocket = DatagramSocket(5556)
            isSending = true

            // 注册传感器
            accelerometer?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME) }
            gyroscope?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME) }
            lightSensor?.let { sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }

            // 请求位置更新
            if (locationManager.isProviderEnabled(LocationManager.NETWORK_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 2000L, 5f, this)
            }
            if (locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
                locationManager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 2000L, 5f, this)
            }

            // 获取一次最后已知位置，防止初始数据为空
            val lastNetLoc = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER)
            val lastGpsLoc = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER)
            val bestLoc = lastGpsLoc ?: lastNetLoc

            if (bestLoc != null) {
                lat = bestLoc.latitude
                lon = bestLoc.longitude
                tvGps.text = String.format(Locale.US, "GPS: %.4f, %.4f (缓存)", lat, lon)
            } else {
                tvGps.text = "GPS: 搜索信号中..."
            }

            // --- UI 状态更新 ---
            etIp.isEnabled = false
            etPort.isEnabled = false

            btnToggle.text = "停止监测服务"
            // 设置为红色背景，表示停止操作
            btnToggle.backgroundTintList = ColorStateList.valueOf(Color.parseColor("#D32F2F"))

            tvStatus.text = "🟢 运行中"
            // 设置为绿色背景
            tvStatus.setBackgroundColor(Color.parseColor("#43A047"))

            startReceivingThread()

        } catch (e: Exception) {
            Toast.makeText(this, "启动失败: ${e.message}", Toast.LENGTH_SHORT).show()
            stopSystem()
        }
    }

    private fun stopSystem() {
        isSending = false
        try {
            sensorManager.unregisterListener(this)
            locationManager.removeUpdates(this)
            socket?.close()
            recvSocket?.close()
        } catch (e: Exception) { e.printStackTrace() }

        dismissAlert()

        // --- UI 状态更新 ---
        etIp.isEnabled = true
        etPort.isEnabled = true

        btnToggle.text = "启动监测服务"
        // 设置为 Teal 色背景 (默认主题色)
        btnToggle.backgroundTintList = ColorStateList.valueOf(Color.parseColor("#009688"))

        tvStatus.text = "⚪ 已停止"
        // 设置为灰色背景
        tvStatus.setBackgroundColor(Color.parseColor("#B0BEC5"))
    }

    private fun startReceivingThread() {
        CoroutineScope(Dispatchers.IO).launch {
            val buffer = ByteArray(1024)
            val packet = DatagramPacket(buffer, buffer.size)
            while (isSending) {
                try {
                    recvSocket?.receive(packet)
                    val msg = String(packet.data, 0, packet.length).trim()

                    if (msg == "ALERT" && sosState != 1) {
                        runOnUiThread { showFallDialog() }
                    }
                    else if (msg == "SAFE") {
                        runOnUiThread { dismissAlert() }
                    }
                } catch (e: Exception) { }
            }
        }
    }

    private fun showFallDialog() {
        if (alertDialog != null && alertDialog!!.isShowing) {
            return
        }

        // 震动提醒
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(800, VibrationEffect.DEFAULT_AMPLITUDE))
        } else {
            vibrator.vibrate(800)
        }

        val builder = AlertDialog.Builder(this)
            .setTitle("⚠️ 严重跌倒警报")
            .setMessage("系统检测到您可能跌倒了。\n\n如果您安然无恙，请点击“误报”以解除警报。")
            .setCancelable(false)
            .setPositiveButton("发生误判 (解除)") { _, _ ->
                sendFalseAlarm()
            }
            .setNegativeButton("呼叫求救 (SOS)") { _, _ ->
                triggerSOS()
            }

        alertDialog = builder.create()
        alertDialog?.show()
    }

    private fun dismissAlert() {
        if (alertDialog != null && alertDialog!!.isShowing) {
            alertDialog?.dismiss()
            alertDialog = null
            Toast.makeText(this, "监护人已确认安全", Toast.LENGTH_SHORT).show()
        }
    }

    private fun sendFalseAlarm() {
        sosState = 2 // 状态2表示误报
        Toast.makeText(this, "已反馈误判，正在解除...", Toast.LENGTH_SHORT).show()
        // 发送几次数据包让服务器更新状态，然后重置
        Handler(Looper.getMainLooper()).postDelayed({ sosState = 0 }, 2000)
    }

    private fun triggerSOS() {
        sosState = 1 // 状态1表示SOS
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(500, VibrationEffect.DEFAULT_AMPLITUDE))
        }
        Toast.makeText(this, "🆘 SOS 信号已发出！", Toast.LENGTH_LONG).show()
        // SOS状态保持一段时间
        Handler(Looper.getMainLooper()).postDelayed({ sosState = 0 }, 5000)
    }

    override fun onSensorChanged(event: SensorEvent?) {
        if (event == null) return

        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> {
                System.arraycopy(event.values, 0, valAcc, 0, 3)
            }
            Sensor.TYPE_GYROSCOPE -> {
                System.arraycopy(event.values, 0, valGyro, 0, 3)
            }
            Sensor.TYPE_LIGHT -> {
                valLight = event.values[0]
            }
        }

        val now = System.currentTimeMillis()
        if (isSending && (now - lastSendTime) >= SEND_INTERVAL) {
            lastSendTime = now
            sendDataPacket()
            updateUI()
        }
    }

    override fun onLocationChanged(location: Location) {
        lat = location.latitude
        lon = location.longitude
        val provider = if (location.provider == LocationManager.GPS_PROVIDER) "GPS" else "Net"
        tvGps.text = String.format(Locale.US, "%s: %.5f, %.5f", provider, lat, lon)
    }

    private fun sendDataPacket() {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val battery = getBatteryLevel()
                // 格式: accX, accY, accZ, gyroX, gyroY, gyroZ, light, battery, sosState, lat, lon
                val msg = String.format(Locale.US,
                    "%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.1f,%d,%d,%.6f,%.6f",
                    valAcc[0], valAcc[1], valAcc[2],
                    valGyro[0], valGyro[1], valGyro[2],
                    valLight, battery, sosState, lat, lon
                )

                val targetIpStr = etIp.text.toString()
                if (targetIpStr.isNotEmpty()) {
                    val ip = InetAddress.getByName(targetIpStr)
                    val port = etPort.text.toString().toIntOrNull() ?: 5555
                    val data = msg.toByteArray()
                    socket?.send(DatagramPacket(data, data.size, ip, port))
                }
            } catch (e: Exception) {
                // 网络错误暂不弹窗，避免刷屏
            }
        }
    }

    private fun updateUI() {
        tvAcc.text = String.format(Locale.US, "%.2f, %.2f, %.2f", valAcc[0], valAcc[1], valAcc[2])
        tvGyro.text = String.format(Locale.US, "%.2f, %.2f, %.2f", valGyro[0], valGyro[1], valGyro[2])
        tvLight.text = String.format(Locale.US, "%.1f Lx", valLight)
        tvBattery.text = "${getBatteryLevel()}%"
    }

    private fun getBatteryLevel(): Int {
        val intent = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return 0
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        return if (level >= 0 && scale > 0) (level * 100 / scale) else 0
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}
    override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
}