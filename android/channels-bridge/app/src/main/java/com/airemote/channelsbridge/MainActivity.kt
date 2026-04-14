package com.airemote.channelsbridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.Switch
import android.widget.TextView
import android.view.View
import androidx.appcompat.app.AppCompatActivity

/**
 * Configuration screen with live connection status.
 * Status bar at top shows colored dot + state + detail text.
 * Form fields scroll under the keyboard thanks to adjustResize.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var tokenEdit: EditText
    private lateinit var nameEdit: EditText
    private lateinit var enableSwitch: Switch
    private lateinit var statusText: TextView
    private lateinit var statusDetail: TextView
    private lateinit var statusDot: View
    private lateinit var statusCard: View

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val state = intent?.getStringExtra(BridgeService.EXTRA_STATE) ?: return
            val detail = intent.getStringExtra(BridgeService.EXTRA_DETAIL) ?: ""
            runOnUiThread { showStatus(state, detail) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        hostEdit = findViewById(R.id.editHost)
        portEdit = findViewById(R.id.editPort)
        tokenEdit = findViewById(R.id.editToken)
        nameEdit = findViewById(R.id.editDeviceName)
        enableSwitch = findViewById(R.id.switchEnable)
        statusText = findViewById(R.id.textStatus)
        statusDetail = findViewById(R.id.textStatusDetail)
        statusDot = findViewById(R.id.statusDot)
        statusCard = findViewById(R.id.statusCard)

        // Load saved config
        hostEdit.setText(BridgeConfig.getServerHost(this))
        portEdit.setText(BridgeConfig.getServerPort(this).toString())
        tokenEdit.setText(BridgeConfig.getAuthToken(this))
        nameEdit.setText(BridgeConfig.getDeviceName(this))
        enableSwitch.isChecked = BridgeConfig.isEnabled(this)

        updateLocalStatus()

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            saveAndApply()
        }

        enableSwitch.setOnCheckedChangeListener { _, isChecked ->
            BridgeConfig.setEnabled(this, isChecked)
            if (isChecked) {
                saveConfig()
                startBridgeService()
            } else {
                stopBridgeService()
                showStatus("disconnected", "Bridge disabled")
            }
        }
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(BridgeService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(statusReceiver, filter)
        }
    }

    override fun onPause() {
        super.onPause()
        try { unregisterReceiver(statusReceiver) } catch (_: Exception) {}
    }

    private fun saveConfig() {
        BridgeConfig.setServerHost(this, hostEdit.text.toString().trim())
        BridgeConfig.setServerPort(this, portEdit.text.toString().trim().toIntOrNull() ?: 8771)
        BridgeConfig.setAuthToken(this, tokenEdit.text.toString().trim())
        BridgeConfig.setDeviceName(this, nameEdit.text.toString().trim().ifBlank { Build.MODEL })
    }

    private fun saveAndApply() {
        saveConfig()
        if (enableSwitch.isChecked) {
            stopBridgeService()
            startBridgeService()
            showStatus("connecting", "Restarting with new configuration...")
        } else {
            showStatus("disconnected", "Configuration saved — enable the switch to connect")
        }
    }

    private fun startBridgeService() {
        val intent = Intent(this, BridgeService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun stopBridgeService() {
        stopService(Intent(this, BridgeService::class.java))
    }

    /** Set status from local state (no broadcast yet received). */
    private fun updateLocalStatus() {
        val host = BridgeConfig.getServerHost(this)
        val enabled = BridgeConfig.isEnabled(this)
        when {
            host.isBlank() -> showStatus("error", "Enter your MCP server address to get started")
            enabled -> showStatus("connecting", "Service should be running — waiting for status...")
            else -> showStatus("disconnected", "Bridge disabled")
        }
    }

    /** Update the status bar UI. */
    private fun showStatus(state: String, detail: String) {
        val (label, dotColor, cardColor) = when (state) {
            "connected" -> Triple("Connected", Color.parseColor("#4CAF50"), Color.parseColor("#E8F5E9"))
            "connecting" -> Triple("Connecting…", Color.parseColor("#FF9800"), Color.parseColor("#FFF3E0"))
            "error" -> Triple("Error", Color.parseColor("#F44336"), Color.parseColor("#FFEBEE"))
            else -> Triple("Disconnected", Color.parseColor("#9E9E9E"), Color.parseColor("#F5F5F5"))
        }
        statusText.text = label
        statusDetail.text = detail

        // Round dot
        val dot = GradientDrawable()
        dot.shape = GradientDrawable.OVAL
        dot.setColor(dotColor)
        statusDot.background = dot

        statusCard.setBackgroundColor(cardColor)
    }
}
