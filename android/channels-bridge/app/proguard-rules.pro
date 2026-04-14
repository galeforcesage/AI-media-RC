# AI Remote Bridge — ProGuard rules for release builds

# Keep WebSocket client (reflection-based)
-keep class org.java_websocket.** { *; }

# Keep Gson serialization models
-keep class com.google.gson.** { *; }
-keepattributes Signature
-keepattributes *Annotation*

# Keep our bridge config and service classes
-keep class com.airemote.channelsbridge.** { *; }
